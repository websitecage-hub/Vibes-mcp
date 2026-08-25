#!/usr/bin/env python3
"""Media generation MCP server (images via meta.ai, videos via vibes.ai).

Exposes MCP tools over Streamable HTTP at /mcp so Claude/ChatGPT can call:
  images/videos/lipsync/library  -> vibes.ai & meta.ai backends
  meta.ai full suite             -> chat sandbox files, image editing,
                                    transparent assets, GIFs, web/social/
                                    places search, deep research

Media delivery: generated media is embedded directly in the MCP response as
image content blocks (renders inline in Claude) AND returned as public CDN
URLs (download links). Nothing is stored on this server.

HTTP endpoints:
  GET /         -> info page
  GET /health   -> keep-alive probe for UptimeRobot (GET and HEAD)
  POST /mcp     -> MCP Streamable HTTP transport

Storage hygiene: a background janitor deletes stray temp/media files hourly,
so Render's free disk can never fill up.
"""
import asyncio
import base64
import glob
import os
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
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent, ImageContent

START_TIME = time.time()

MAX_EMBED_BYTES = 4_500_000   # stay under MCP message limits

# ── shared helpers ────────────────────────────────────────────────────────


def _download_temp(url, suffix=".bin"):
    """Fetch a remote media file to a temp path."""
    from curl_cffi import requests as _rq
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


def _embed_media_block(url):
    """Fetch a public CDN URL and build an ImageContent block (inline render).
    Returns None when the file is too big or not an embeddable type."""
    try:
        from curl_cffi import requests as _rq
        r = _rq.get(url, timeout=90, impersonate="chrome")
        if r.status_code != 200 or len(r.content) > MAX_EMBED_BYTES:
            return None
        mime = (r.headers.get("content-type") or "").split(";")[0].strip()
        if not mime.startswith("image/") or "svg" in mime:
            return None
        return ImageContent(type="image",
                            data=base64.b64encode(r.content).decode(),
                            mimeType=mime)
    except Exception:  # noqa: BLE001
        return None


def _media_response(header, urls, note=""):
    """Text with clean download links + inline-embedded image blocks."""
    lines = [header]
    for u in urls:
        name = os.path.basename(u.split("?")[0]) or "media"
        lines.append(f"- [{name}]({u})")
    if note:
        lines.append(f"\n{note}")
    blocks = [TextContent(type="text", text="\n".join(lines))]
    for u in urls[:3]:
        blk = _embed_media_block(u)
        if blk is not None:
            blocks.append(blk)
    return blocks

# ── meta.ai helpers ───────────────────────────────────────────────────────


async def meta_ask(message, mode="instant", timeout=240, attachments=None,
                   attempts=2):
    """Full-capability meta.ai passthrough. Returns dict(text, media[])."""
    token = meta_client.load_token()
    if not token:
        raise RuntimeError("no META_TOKEN available")
    conv = str(uuid.uuid4())
    last_err = None
    for attempt in range(attempts):
        sess = meta_client.DGWSession(token, conv, download_media=False)
        try:
            await sess.connect(timeout=20)
            text, media = await sess.ask(message, mode=mode, timeout=timeout,
                                         attachments=attachments)
            urls = [{"url": m.get("url"), "mime_type": m.get("mime_type", ""),
                     "filename": m.get("filename", "")}
                    for m in media if m.get("url")]
            if text.strip() or urls:
                return {"text": meta_client.clean_chunk(text.strip()),
                        "media": urls}
            last_err = RuntimeError("empty response from meta.ai")
        except Exception as e:  # noqa: BLE001
            last_err = e
        finally:
            await sess.close()
        await asyncio.sleep(1.5)
    raise RuntimeError(f"meta.ai request failed: {last_err}")


def _meta_attachment_from_url(url):
    """Download a remote file and upload it to meta.ai rupload -> attachment."""
    import mimetypes
    tmp = _download_temp(url, _suffix_for_url(url))
    try:
        token = meta_client.load_token()
        media_id = meta_client.upload_file(tmp, token)
        fname = os.path.basename(url.split("?")[0]) or "upload"
        return {"media_id": media_id,
                "mime_type": mimetypes.guess_type(tmp)[0]
                or "application/octet-stream",
                "filename": fname}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

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
        thumb = it.get("thumbnailUrl") or ""
        err = it.get("error")
        if url:
            out.append({"url": url, "thumb": thumb,
                        "id": str(it.get("id", ""))[:40],
                        "type": "video" if it.get("videoUrl") else "image"})
        elif err:
            out.append({"error": str(err)[:160]})
    return out


def _upload_ref(v, project, image_path):
    """Upload a local image and register it as project content -> oref dict."""
    media = v.upload_media(image_path)
    citems = v.project_upload(project["id"], media)
    oref = {"mediaEntId": media["mediaEntId"],
            "cdnUrl": media.get("cdnUrl"),
            "_id": citems[0]["id"]}
    return oref


def _run_vibe_gen(prompt, aspect, resolution, model, n, max_sec,
                  reference_image_url=None, gen_kind="auto"):
    v = _get_vibes()
    tmp = None
    try:
        project = _ensure_project(v)
        oref = None
        gen_type = gen_kind if gen_kind in ("t2v", "i2v") else "t2v"
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
            raise RuntimeError(f"vibes generation failed: {e}") from e
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

# ── storage janitor ───────────────────────────────────────────────────────


def _janitor_sweep():
    """Delete stray temp/media files older than 2h; keeps disk forever clean."""
    now = time.time()
    removed = 0
    patterns = ["/tmp/tmp*", "/tmp/*.jpg", "/tmp/*.png", "/tmp/*.mp4",
                "/tmp/*.mp3"]
    root = os.path.dirname(os.path.abspath(__file__))
    for sub in ("media", "downloads"):
        d = os.path.join(root, sub)
        if os.path.isdir(d):
            patterns += [os.path.join(d, "*")]
    for pat in patterns:
        for f in glob.glob(pat):
            try:
                if os.path.isfile(f) and now - os.path.getmtime(f) > 7200:
                    os.remove(f)
                    removed += 1
            except OSError:
                pass
    return removed


async def _janitor_loop():
    while True:
        try:
            await run_in_threadpool(_janitor_sweep)
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(3600)

# ── MCP server + tools ────────────────────────────────────────────────────

mcp = FastMCP(
    "media-gen", stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False))

# ── core generation tools ────────────────────────────────────────────────


@mcp.tool()
async def generate_image(prompt: str) -> list:
    """Generate image(s) from a text prompt using Meta AI.
    Returns the image rendered INLINE plus public download URLs.

    Args:
        prompt: Description of the image to create. Be detailed for best results.
    """
    try:
        res = await meta_ask("/imagine " + prompt, timeout=180)
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [m["url"] for m in res["media"] if m.get("url")]
    if not urls:
        return [TextContent(type="text",
                            text=f"No image returned.\n{res['text'][:400]}")]
    return _media_response(f"Generated {len(urls)} image(s):", urls,
                           res["text"][:300])


@mcp.tool()
async def edit_image(image_url: str, instruction: str) -> list:
    """Edit an existing image with Meta AI: change style/background/clothes,
    combine images, restore, colorize, remove objects, etc.

    Args:
        image_url: Public URL of the image to edit.
        instruction: What to change, e.g. 'make the background snowy at night'.
    """
    try:
        att = await run_in_threadpool(_meta_attachment_from_url, image_url)
        res = await meta_ask(instruction, timeout=240, attachments=[att])
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [m["url"] for m in res["media"] if m.get("url")]
    if not urls:
        return [TextContent(type="text",
                            text=f"No edited image returned.\n"
                                 f"{res['text'][:400]}")]
    return _media_response("Edited image:", urls)


@mcp.tool()
async def transparent_image(prompt: str) -> list:
    """Generate a logo/sticker/icon/badge/sprite with a TRANSPARENT background.
    Delivered as PNG with alpha channel.

    Args:
        prompt: What to draw, e.g. 'cute rocket mascot logo'.
    """
    msg = ("/imagine " + prompt +
           " - isolated asset on fully transparent background, PNG with "
           "alpha channel, no backdrop, sticker-style cutout, crisp edges")
    try:
        res = await meta_ask(msg, timeout=240)
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [m["url"] for m in res["media"] if m.get("url")]
    if not urls:
        return [TextContent(type="text",
                            text=f"No asset returned.\n{res['text'][:400]}")]
    return _media_response("Transparent asset(s):", urls)


@mcp.tool()
async def make_gif(prompt: str) -> list:
    """Create an animated GIF / flip-book animation (frame-by-frame).

    Args:
        prompt: What should animate, e.g. 'a cat chasing a laser pointer'.
    """
    msg = ("/imagine " + prompt +
           " - create this as a short animated GIF flip-book of frames")
    try:
        res = await meta_ask(msg, timeout=300)
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [m["url"] for m in res["media"] if m.get("url")]
    if not urls:
        return [TextContent(type="text",
                            text=f"No GIF returned.\n{res['text'][:400]}")]
    return _media_response("Animated GIF:", urls)


@mcp.tool()
async def generate_video(prompt: str, aspect_ratio: str = "9:16",
                         resolution: str = "480p", model: str = "",
                         count: int = 1, reference_image_url: str = "") -> list:
    """Generate video(s) from a text prompt using Vibes AI.
    Returns video download URLs (+ poster preview where available).
    Takes ~30s-5min per clip.

    Args:
        prompt: Description of the video to create.
        aspect_ratio: One of 9:16, 16:9, 1:1, 4:5, 3:4, 4:3.
        resolution: 480p (fast), 720p, or 1080p (slowest).
        model: Optional: midjen-short (fast), midjen-long, midjen, sora, veo.
        count: How many variations (1-4).
        reference_image_url: Optional public image URL. When given, the video
            animates FROM that image (image-to-video).
    """
    try:
        res = await run_in_threadpool(
            _run_vibe_gen, prompt, aspect_ratio, resolution,
            model or None, count, 360, reference_image_url or None, "t2v")
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    ok = [i for i in res["items"] if "url" in i]
    bad = [i for i in res["items"] if "error" in i]
    if bad and not ok:
        return [TextContent(type="text",
                            text="All items failed:\n"
                                 + "\n".join(f"- {b['error']}" for b in bad)
                                 + "\nTip: retry or rephrase the prompt.")]
    header = f"Generated {len(ok)} video(s):"
    lines = [header]
    thumbs = []
    for i in ok:
        name = os.path.basename(i["url"].split("?")[0]) or "video.mp4"
        lines.append(f"- [{name}]({i['url']})")
        if i.get("thumb"):
            thumbs.append(i["thumb"])
    tip = ("\nTip: open the link to watch/download the mp4."
           if ok else "")
    blocks = [TextContent(type="text", text="\n".join(lines) + tip)]
    for t in thumbs[:2]:
        blk = _embed_media_block(t)
        if blk is not None:
            blocks.append(blk)
    return blocks


@mcp.tool()
async def animate_image(image_url: str, prompt: str = "",
                        aspect_ratio: str = "9:16",
                        resolution: str = "480p") -> list:
    """Turn an existing image into a short video (image-to-video).

    Args:
        image_url: Public URL of the source image.
        prompt: How the scene should move (optional).
        aspect_ratio: Output ratio, defaults to source shape (9:16).
        resolution: 480p, 720p, or 1080p.
    """
    try:
        res = await run_in_threadpool(
            _run_vibe_gen, prompt or "animate this image", aspect_ratio,
            resolution, None, 1, 420, image_url, "i2v")
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    ok = [i for i in res["items"] if "url" in i]
    lines = ["Animated image -> video:"]
    thumbs = []
    for i in ok:
        name = os.path.basename(i["url"].split("?")[0]) or "video.mp4"
        lines.append(f"- [{name}]({i['url']})")
        if i.get("thumb"):
            thumbs.append(i["thumb"])
    blocks = [TextContent(type="text", text="\n".join(lines))]
    for t in thumbs[:2]:
        blk = _embed_media_block(t)
        if blk is not None:
            blocks.append(blk)
    return blocks


@mcp.tool()
async def create_lipsync(source_url: str, audio_url: str,
                         prompt: str = "", aspect_ratio: str = "9:16",
                         resolution: str = "480p") -> list:
    """Lip-sync a source face/image/video to an audio track.

    Args:
        source_url: Public URL of the face/source image or video.
        audio_url: Public URL of the audio (mp3/wav/m4a).
        prompt: Optional hint text.
        aspect_ratio: Output ratio (9:16 default).
        resolution: 480p, 720p, or 1080p.
    """
    try:
        res = await run_in_threadpool(
            vibes_lipsync, source_url, audio_url, prompt, aspect_ratio,
            resolution)
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    ok = [i for i in res["items"] if "url" in i]
    lines = ["Lipsync complete:"]
    for i in ok:
        lines.append(f"- {i['url']}")
    return [TextContent(type="text", text="\n".join(lines))]

# ── meta.ai capability suite ─────────────────────────────────────────────


@mcp.tool()
async def meta_chat(message: str) -> list:
    """Talk to Meta AI with its FULL capability set. Use for anything the
    other tools don't cover: running Python analysis on data you describe,
    building documents/slides/PDFs (returned as downloadable file links),
    converting media formats, answering questions, math, code, etc.

    Args:
        message: What you want Meta AI to do. Any files it produces come
            back as public download URLs.
    """
    try:
        res = await meta_ask(message, timeout=300)
    except Exception as e:  # noqa: BLE001
        return [TextContent(type="text", text=f"ERROR: {e}")]
    urls = [f"{m.get('filename') or 'file'}: {m['url']}"
            for m in res["media"] if m.get("url")]
    text = res["text"]
    if urls:
        text += "\n\nFiles produced:\n- " + "\n- ".join(urls)
    blocks = [TextContent(type="text", text=text or "(empty response)")]
    img_urls = [m["url"] for m in res["media"]
                if m.get("url") and m.get("mime_type", "").startswith("image/")]
    for u in img_urls[:2]:
        blk = _embed_media_block(u)
        if blk is not None:
            blocks.append(blk)
    return blocks


@mcp.tool()
async def web_search(query: str) -> str:
    """Search the live web via Meta AI with citations. Good for current facts,
    news, prices, schedules, scores, docs, product specs.

    Args:
        query: What to search for.
    """
    try:
        res = await meta_ask(
            f"Search the web and answer with sources cited:\n{query}",
            timeout=240)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return res["text"][:8000]


@mcp.tool()
async def deep_research(topic: str) -> str:
    """Deep-research a topic via Meta AI: multiple search rounds, synthesis,
    formal structured report. Slower (~2-5 min) but thorough.

    Args:
        topic: The subject to research in depth.
    """
    try:
        res = await meta_ask(
            f"Do deep research on this topic: use multiple searches, compare "
            f"sources, then write a formal structured report with sections "
            f"and citations.\nTopic: {topic}", timeout=600)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return res["text"][:20000]


@mcp.tool()
async def social_search(query: str) -> str:
    """Semantic-search public social posts (Instagram/Facebook/Threads) via
    Meta AI. Great for trends, real people's recommendations, aesthetics.

    Args:
        query: What to look for, e.g. 'best budget espresso machines people
        recommend' or '@username recent posts'.
    """
    try:
        res = await meta_ask(
            f"Search public social posts (Instagram, Facebook, Threads) and "
            f"summarize what people are saying, citing accounts/posts:"
            f"\n{query}", timeout=240)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return res["text"][:8000]


@mcp.tool()
async def places_search(query: str) -> str:
    """Search real places (restaurants, cafes, gyms, shops, parks...) via
    Meta AI's Places graph: names, addresses, hours, price levels.

    Args:
        query: What and where, e.g. 'best ramen near Shibuya Tokyo'.
    """
    try:
        res = await meta_ask(
            f"Find real places matching this and give name/address/hours/"
            f"price level for each:\n{query}", timeout=240)
    except Exception as e:  # noqa: BLE001
        return f"ERROR: {e}"
    return res["text"][:8000]

# ── library management tools ─────────────────────────────────────────────


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
    removed = _janitor_sweep()
    if removed:
        parts.append(f"janitor: purged {removed} stale file(s)")
    return "\n".join(parts)

# ── HTTP app ──────────────────────────────────────────────────────────────

mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    async with mcp.session_manager.run():
        task = asyncio.create_task(_janitor_loop())
        yield
        task.cancel()


app = FastAPI(title="media-gen-mcp", lifespan=lifespan)


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return JSONResponse({
        "service": "media-gen-mcp",
        "status": "running",
        "uptime_s": int(time.time() - START_TIME),
        "mcp_endpoint": "/mcp",
        "tools": [
            "generate_image", "edit_image", "transparent_image", "make_gif",
            "generate_video", "animate_image", "create_lipsync",
            "meta_chat", "web_search", "deep_research", "social_search",
            "places_search", "list_voices", "media_library",
            "favorite_media", "delete_media", "media_status"],
    })


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    return JSONResponse({"ok": True, "uptime_s": int(time.time() - START_TIME)})


# MCP transport last, so / and /health always win
app.mount("/", mcp_app)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
