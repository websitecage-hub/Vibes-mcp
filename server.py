#!/usr/bin/env python3
"""Media generation MCP server (images via meta.ai, videos via vibes.ai).

Exposes MCP tools over Streamable HTTP at /mcp so Claude/ChatGPT can call:
  - generate_image(prompt)              -> meta.ai image CDN URLs
  - generate_video(prompt, ...)         -> vibes.ai video CDN URLs
  - animate_image(image_url, prompt)    -> image-to-video
  - media_status()                      -> account/session health

HTTP endpoints:
  GET /         -> info page
  GET /health   -> keep-alive probe for UptimeRobot
  POST /mcp     -> MCP Streamable HTTP transport

No generated media is stored here: both backends return public CDN URLs,
so delivery is a plain link and storage cleans itself up.
"""
import asyncio
import os
import sys
import tempfile
import threading
import time
import uuid

import meta as meta_client
import vibes as vibes_mod

from starlette.concurrency import run_in_threadpool
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

START_TIME = time.time()

# ── meta.ai helpers ───────────────────────────────────────────────────────

async def meta_generate_image(prompt: str, timeout: int = 180):
    """Token-only mode: fresh conversation per request, no local downloads."""
    token = meta_client.load_token()
    if not token:
        raise RuntimeError("no META_TOKEN available")
    conv = str(uuid.uuid4())
    last_err = None
    for attempt in range(2):
        sess = meta_client.DGWSession(token, conv, download_media=False)
        try:
            await sess.connect(timeout=20)
            text, media = await sess.ask("/imagine " + prompt, timeout=timeout)
            urls = [m.get("url") for m in media if m.get("url")]
            if urls:
                return {"text": text.strip(), "urls": urls}
            last_err = RuntimeError(f"no media returned ({text[:120]!r})")
        except Exception as e:  # noqa: BLE001
            last_err = e
        finally:
            await sess.close()
        await asyncio.sleep(1.5)
    raise RuntimeError(f"meta image generation failed: {last_err}")

# ── vibes.ai helpers ──────────────────────────────────────────────────────

_vibes_lock = threading.Lock()
_vibes_client = None


def _get_vibes():
    global _vibes_client
    with _vibes_lock:
        if _vibes_client is not None:
            return _vibes_client
        s = vibes_mod.auth.load_session()
        if s is None:
            s = vibes_mod.auth.login_session(
                print_fn=lambda *a: print("[vibes-auth]", *a))
            if s is not None:
                vibes_mod.auth.save_session(s)
        if s is None:
            raise RuntimeError("vibes.ai authentication failed "
                               "(session.json invalid and re-login rejected)")
        _vibes_client = vibes_mod.Vibes(s)
        return _vibes_client


def _reset_vibes():
    global _vibes_client
    with _vibes_lock:
        _vibes_client = None


def _ensure_project(v):
    p = v.create_project(name="mcp-media")
    if not p:
        projects = v.list_projects(limit=5)
        p = projects[0] if projects else None
    if not p:
        raise RuntimeError("could not create vibes project")
    return p


def _pick_urls(items, want):
    out = []
    for it in items:
        url = it.get("videoUrl") if want == "video" else (
            it.get("imageUrl") or it.get("videoUrl"))
        err = it.get("error")
        if url:
            out.append({"url": url,
                        "id": str(it.get("id", ""))[:40],
                        "type": "video" if it.get("videoUrl") else "image"})
        elif err:
            out.append({"error": str(err)[:160]})
    return out


def _download_temp(url, suffix=".bin"):
    """Fetch a remote media file to a temp path (for uploads)."""
    import requests as _rq
    r = _rq.get(url, timeout=120, impersonate="chrome")
    if r.status_code != 200 or len(r.content) < 256:
        raise RuntimeError(f"cannot fetch {url[:80]} ({r.status_code})")
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.write(fd, r.content)
    os.close(fd)
    return tmp


_EXT_MIME = {"jpg": ".jpg", "jpeg": ".jpg", "png": ".png", "webp": ".webp",
             "gif": ".gif", "mp4": ".mp4", "mov": ".mov", "webm": ".webm",
             "mp3": ".mp3", "wav": ".wav", "m4a": ".m4a", "ogg": ".ogg"}


def _suffix_for_url(url):
    base = url.split("?")[0]
    ext = os.path.splitext(base)[1].lower().lstrip(".")
    return _EXT_MIME.get(ext, ".bin")


def _upload_ref(v, project, image_path):
    """Upload a local image and register it as project content -> oref dict."""
    media = v.upload_media(image_path)
    citems = v.project_upload(project["id"], media)
    oref = {"mediaEntId": media["mediaEntId"],
            "cdnUrl": media.get("cdnUrl"),
            "_id": citems[0]["id"]}
    return oref


def vibes_generate_video(prompt, aspect="9:16", resolution="480p",
                         model=None, n=1, max_sec=360,
                         reference_image_url=None):
    """Text-to-video, optional reference image (image-to-video when given)."""
    v = _get_vibes()
    tmp = None
    try:
        project = _ensure_project(v)
        oref = None
        gen_type = "t2v"
        if reference_image_url:
            tmp = _download_temp(reference_image_url,
                                 _suffix_for_url(reference_image_url))
            oref = _upload_ref(v, project, tmp)
            gen_type = "i2v"
        try:
            items, batch_id = v.generate(
                project, prompt, n=max(1, min(4, n)), aspect=aspect,
                resolution=resolution, kind="videos", model=model,
                oref=oref, generation_type=gen_type, max_sec=max_sec)
        except vibes_mod.VibesError as e:
            if e.status == 401:
                _reset_vibes()
            raise RuntimeError(f"vibes video failed: {e}") from e
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    results = _pick_urls(items, "video")
    if not results:
        raise RuntimeError("vibes returned no usable items")
    return {"batch_id": batch_id, "items": results}


def vibes_animate_image(image_url, prompt="", aspect="9:16",
                        resolution="480p", model=None, max_sec=420):
    """Image-to-video: download the source image, upload as oref, submit i2v."""
    v = _get_vibes()
    tmp = None
    try:
        tmp = _download_temp(image_url, _suffix_for_url(image_url))
        project = _ensure_project(v)
        oref = _upload_ref(v, project, tmp)
        items, batch_id = v.generate(project, prompt or "animate this image",
                                     n=1, aspect=aspect, resolution=resolution,
                                     kind="videos", model=model, oref=oref,
                                     generation_type="i2v", max_sec=max_sec)
    except Exception as e:  # noqa: BLE001
        if isinstance(e, vibes_mod.VibesError) and e.status == 401:
            _reset_vibes()
        raise RuntimeError(f"image-to-video failed: {e}") from e
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    results = _pick_urls(items, "video")
    if not results:
        raise RuntimeError("vibes returned no usable items")
    return {"batch_id": batch_id, "items": results}


def vibes_lipsync(source_url, audio_url, prompt="", aspect="9:16",
                  resolution="480p", max_sec=420):
    """Lip-sync: pair an audio track (URL) with a source image/video (URL)."""
    v = _get_vibes()
    tmp_src = tmp_aud = None
    try:
        project = _ensure_project(v)
        tmp_src = _download_temp(source_url, _suffix_for_url(source_url))
        src_media = v.upload_media(tmp_src)
        src_items = v.project_upload(project["id"], src_media)
        src_content_id = src_items[0]["id"]

        tmp_aud = _download_temp(audio_url, _suffix_for_url(audio_url))
        aud = v.upload_audio_direct(tmp_aud)

        bid = v.make_batch(project, prompt or "lip sync", n=1, aspect=aspect)
        res = v.generate_lipsync(project, bid, aud["mediaEntId"],
                                 src_content_id, prompt=prompt or "",
                                 aspect=aspect, resolution=resolution)
        if isinstance(res, dict) and res.get("success") is False:
            raise RuntimeError(str(res.get("error"))[:200])
        items = v.wait_generation(bid, want="video", max_sec=max_sec,
                                  project=project)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"lipsync failed: {e}") from e
    finally:
        for t in (tmp_src, tmp_aud):
            if t and os.path.exists(t):
                try:
                    os.remove(t)
                except OSError:
                    pass
    results = _pick_urls(items, "video")
    if not results:
        raise RuntimeError("lipsync produced no usable items")
    return {"items": results}


def vibes_list_voices(limit=100):
    v = _get_vibes()
    voices = v.studio_voices(limit=limit)
    return [{"id": vo.get("id"), "name": vo.get("name"),
             "description": (vo.get("description") or "")[:80]}
            for vo in voices]


def vibes_media_library(media_type="video", limit=25):
    v = _get_vibes()
    t = {"video": "video", "image": "images", "images": "images",
         "audio": "audio"}.get((media_type or "video").lower(), "video")
    items, _page = v.media_library(types=t, limit=max(1, min(50, limit)))
    out = []
    for it in items:
        url = it.get("fullUrl") or it.get("videoUrl") or it.get("imageUrl")
        if url:
            out.append({"id": str(it.get("id", ""))[:40],
                        "type": it.get("type"),
                        "url": url.split("?")[0],
                        "favorited": bool(it.get("isFavorited"))})
    return out


def vibes_set_favorite(item_id, is_favorited=True):
    v = _get_vibes()
    j = v.set_favorite(item_id, bool(is_favorited))
    return {"ok": isinstance(j, dict) and j.get("success", True),
            "item_id": item_id, "favorited": bool(is_favorited)}


def vibes_delete_media(item_id):
    v = _get_vibes()
    v.delete_content_item(item_id)
    return {"ok": True, "deleted": item_id}

# ── MCP server + tools ────────────────────────────────────────────────────

mcp = FastMCP("media-gen", stateless_http=True, json_response=True)


@mcp.tool()
async def generate_image(prompt: str) -> str:
    """Generate image(s) from a text prompt using Meta AI. Returns public CDN URLs.

    Args:
        prompt: Description of the image to create. Be detailed for best results.
    """
    try:
        res = await meta_generate_image(prompt)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    lines = [f"Generated {len(res['urls'])} image(s):"]
    lines += [f"- {u}" for u in res["urls"]]
    if res["text"]:
        lines.append(f"Note: {res['text'][:300]}")
    return "\n".join(lines)


@mcp.tool()
async def generate_video(prompt: str, aspect_ratio: str = "9:16",
                         resolution: str = "480p", model: str = "",
                         count: int = 1, reference_image_url: str = "") -> str:
    """Generate video(s) from a text prompt using Vibes AI. Returns public CDN URLs.

    Args:
        prompt: Description of the video to create.
        aspect_ratio: One of 9:16, 16:9, 1:1, 4:5, 3:4, 4:3.
        resolution: 480p (fast), 720p, or 1080p (slowest).
        model: Optional: midjen-short (fast), midjen-long, midjen, sora, veo.
        count: How many variations (1-4). Each takes ~30-120s.
        reference_image_url: Optional public image URL. When given, the video
            is generated FROM that image (image-to-video).
    """
    try:
        res = await run_in_threadpool(
            vibes_generate_video, prompt, aspect_ratio, resolution,
            model or None, count, 360, reference_image_url or None)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    ok = [i for i in res["items"] if "url" in i]
    bad = [i for i in res["items"] if "error" in i]
    lines = [f"Generated {len(ok)} video(s) (batch {res['batch_id'][:20]}…):"]
    lines += [f"- {i['url']}" for i in ok]
    lines += [f"- FAILED: {i['error']}" for i in bad]
    if bad and not ok:
        lines.append("Tip: retry, possibly rephrase the prompt.")
    return "\n".join(lines)


@mcp.tool()
async def animate_image(image_url: str, prompt: str = "",
                        aspect_ratio: str = "9:16",
                        resolution: str = "480p") -> str:
    """Turn an existing image into a short video (image-to-video).

    Args:
        image_url: Public URL of the source image.
        prompt: How the scene should move (optional).
        aspect_ratio: Output ratio, defaults to the source shape (9:16).
        resolution: 480p, 720p, or 1080p.
    """
    try:
        res = await run_in_threadpool(
            vibes_animate_image, image_url, prompt, aspect_ratio, resolution)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    ok = [i for i in res["items"] if "url" in i]
    lines = [f"Animated image -> {len(ok)} video(s):"]
    lines += [f"- {i['url']}" for i in ok]
    return "\n".join(lines)


@mcp.tool()
async def create_lipsync(source_url: str, audio_url: str,
                         prompt: str = "", aspect_ratio: str = "9:16",
                         resolution: str = "480p") -> str:
    """Lip-sync a source image or video to an audio track.

    Args:
        source_url: Public URL of the face/source image or video.
        audio_url: Public URL of the audio file (mp3/wav/m4a).
        prompt: Optional hint text.
        aspect_ratio: Output ratio (9:16 default).
        resolution: 480p, 720p, or 1080p.
    """
    try:
        res = await run_in_threadpool(
            vibes_lipsync, source_url, audio_url, prompt, aspect_ratio,
            resolution)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    ok = [i for i in res["items"] if "url" in i]
    lines = [f"Lipsync complete -> {len(ok)} video(s):"]
    lines += [f"- {i['url']}" for i in ok]
    return "\n".join(lines)


@mcp.tool()
async def list_voices() -> str:
    """List available TTS voices for lip-sync/voiceover on Vibes AI."""
    try:
        voices = await run_in_threadpool(vibes_list_voices)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    if not voices:
        return "No voices available."
    lines = ["Available voices:"]
    lines += [f"- {v['id']}  ({v.get('name', '?')}) {v.get('description', '')}"
              for v in voices[:50]]
    return "\n".join(lines)


@mcp.tool()
async def media_library(media_type: str = "video", limit: int = 25) -> str:
    """List previously generated media with their URLs and item IDs.

    Args:
        media_type: 'video', 'image', or 'audio'.
        limit: Max items to return (1-50).
    """
    try:
        items = await run_in_threadpool(vibes_media_library, media_type, limit)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    if not items:
        return f"No {media_type} media found."
    lines = [f"{len(items)} item(s):"]
    for it in items:
        fav = " ♥" if it["favorited"] else ""
        lines.append(f"- [{it['id']}] {it['type']}{fav}\n  {it['url']}")
    return "\n".join(lines)


@mcp.tool()
async def favorite_media(item_id: str, is_favorited: bool = True) -> str:
    """Favorite or unfavorite a generated media item by its ID.

    Args:
        item_id: The content item ID (from generate_* or media_library).
        is_favorited: True to favorite, False to unfavorite.
    """
    try:
        res = await run_in_threadpool(vibes_set_favorite, item_id, is_favorited)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    state = "favorited" if res["favorited"] else "unfavorited"
    return f"Item {res['item_id']} {state}." if res["ok"] else "Server rejected."


@mcp.tool()
async def delete_media(item_id: str) -> str:
    """Permanently delete a generated media item by its ID.

    Args:
        item_id: The content item ID (from generate_* or media_library).
    """
    try:
        res = await run_in_threadpool(vibes_delete_media, item_id)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return f"Deleted {res['deleted']}."


@mcp.tool()
async def media_status() -> str:
    """Check whether the image (Meta AI) and video (Vibes AI) backends are working."""
    parts = []
    tok = bool(meta_client.load_token())
    parts.append(f"meta.ai token: {'OK' if tok else 'MISSING'}")
    try:
        v = await run_in_threadpool(_get_vibes)
        u = await run_in_threadpool(v.me)
        parts.append(f"vibes.ai session: OK (user {u.get('username', '?')})")
    except Exception as e:  # noqa: BLE001
        parts.append(f"vibes.ai session: FAILING ({str(e)[:120]})")
    return "\n".join(parts)

# ── HTTP app ──────────────────────────────────────────────────────────────

mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        yield


app = FastAPI(title="media-gen-mcp", lifespan=lifespan)


@app.get("/")
async def root():
    return JSONResponse({
        "service": "media-gen-mcp",
        "status": "running",
        "uptime_s": int(time.time() - START_TIME),
        "mcp_endpoint": "/mcp",
        "tools": ["generate_image", "generate_video", "animate_image",
                  "create_lipsync", "list_voices", "media_library",
                  "favorite_media", "delete_media", "media_status"],
    })


@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "uptime_s": int(time.time() - START_TIME)})


# MCP transport last, so / and /health always win
app.mount("/", mcp_app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
