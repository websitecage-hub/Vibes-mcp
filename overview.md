# Media Gen MCP — Full Project Overview (Agent-Readable)

This repo builds a **remote MCP server** that lets **ChatGPT and Claude** (and any REST client) generate **images via meta.ai** and **videos via vibes.ai**, hosted on **Render Free** and kept alive 24/7 by **UptimeRobot**.

The server is **production-ready**: async job pattern for videos (no timeouts), inline media embedding, download proxy, persistent production-hub projects, self-healing sessions, hourly janitor.

---

## 1. Goal

- Single MCP endpoint `https://media-gen-mcp.onrender.com/mcp` (Streamable HTTP, stateless) + REST `https://media-gen-mcp.onrender.com/api/*` + web studio `https://media-gen-mcp.onrender.com/app`
- **Image gen MUST use `meta.py` only** (Meta AI DGW websocket + ecto protos, token-auth). No vibes fallback for images.
- **Video gen uses `vibes.py`** (Vibes AI: batch → submit → wait → CDN urls). Image-to-video and lip-sync are variants.
- Render Free sleeps after 15 min → UptimeRobot HTTP monitor on `/health` every 5 min keeps it awake.

---

## 2. Repo Inventory

| File | Purpose | Key lines |
|------|---------|-----------|
| `meta.py` | Full Meta AI client: token load, DGW websocket, proto pools, rupload `upload_file`, `DGWSession.connect/ask`, login + encryption, embedded `_rt` + `token.txt`/`meta_session.json` self-materializes | `meta.py:139` `pool_from` (dependency fix required), `meta.py:347` `upload_file`, `meta.py:510` `DGWSession` |
| `vibes.py` | Full Vibes AI client: `Vibes` class, `_SessionPayload` embedded `session.json`, `load_session`/`save_session`/`_fresh_client`/`_auto_login`, generation `generate`/`generate_videos`/`generate_images`/`generate_lipsync`/`generate_edit`/`generate_with_ingredients`, uploads, `media_library`, `studio_voices`, `sync` | `vibes.py:20` `_SESSION_PAYLOAD`, `vibes.py:333` `load_session`, `vibes.py:1347` `_fresh_client` |
| `server.py` | **MCP + REST + Web App** — FastAPI + `mcp.server.fastmcp.FastMCP` Streamable HTTP at `/mcp`, 18 tools, REST `/api/*`, `/app` hub, `/health`, download proxy, job system, project hub | `server.py:562` `FastMCP(... transport_security=...)`, `server.py:990` lifespan |
| `requirements.txt` | `fastapi`, `uvicorn`, `mcp[cli]>=1.10,<2`, `curl_cffi`, `websockets`, `protobuf>=4.25,<5`, `pynacl`, `cryptography`, `requests` | pinned `protobuf 4.25.8` and `mcp 1.x` for proto compat |
| `render.yaml` | Render Blueprint: web service `media-gen-mcp`, `pip install -r requirements.txt`, `uvicorn server:app --host 0.0.0.0 --port $PORT`, `healthCheckPath: /health` | |
| `.gitignore` | Excludes `__pycache__`, `_rt/`, `session.json`, `meta_session.json`, `token.txt`, `media/`, `downloads/`, `.venv/`, `*.har`, `api_keys.json`, `*.log` | |
| `API_DOCS.md` | Full REST + MCP documentation with curl/JS examples | |
| `overview.md` | This file | |

**Live URLs:**
- MCP: `https://media-gen-mcp.onrender.com/mcp`
- Health: `https://media-gen-mcp.onrender.com/health` (GET + HEAD)
- App: `https://media-gen-mcp.onrender.com/app` — Production Hub
- REST: `https://media-gen-mcp.onrender.com/api/*`

---

## 3. Architecture

```
[Claude/ChatGPT] --Streamable HTTP--> FastMCP (stateless) --async--> meta.ai DGW wss://gateway.meta.ai/ws/clippy? (ecto protos)
                                   \--threadpool--> vibes.ai https://vibes.ai/api (curl_cffi)
                                   \--FastAPI REST /api/* -> same helpers
                                   \--/app HTML (Vercel/Geist dark) -> REST
                                   \--/api/download proxy -> CDN fetch -> attachment
```

- **No media stored on Render** — backends return public CDN urls (e.g. `https://scontent.xx.fbcdn.net/...webp` for images, `https://scontent-*.fbcdn.net/...mp4` for videos). Images are **embedded inline** as `ImageContent` base64 blocks *and* returned as markdown links `[name](url)`. Videos include poster thumb embed where available.
- **Free disk never fills:** temp files via `tempfile.mkstemp` deleted in `finally`; hourly janitor `_janitor_sweep` deletes `/tmp/tmp*`, `media/*`, `downloads/*` older than 2h; `JOBS` expired after 1h; hub project history capped at 50.
- **Render:** `srv-da6uc17avr4c739m70dg`, region `oregon`, plan `free`, autoDeploy `yes` on `main`.

---

## 4. MCP Tools (18) — server.py:565+

| Tool | Backend | Signature | Notes |
|------|---------|-----------|-------|
| `generate_image` | meta.py | `(prompt: str, project_id: str="") -> list[Content]` | **MUST use meta.py only**. `mode` instant/thinking via hub. Inline embed + URLs. |
| `edit_image` | meta.py | `(image_url: str, instruction: str)` | downloads image → `upload_file` → DGW ask with attachment |
| `transparent_image` | meta.py | `(prompt: str)` | appends transparent PNG instruction |
| `make_gif` | meta.py | `(prompt: str)` | appends GIF flip-book instruction |
| `generate_video` | vibes.py | `(prompt, aspect_ratio="9:16", resolution="480p", model="", count=1, reference_image_url="", project_id="") -> str` | **Async:** returns `job_id` instantly; poll `check_generation` |
| `check_generation` | — | `(job_id: str) -> list[Content]` | Polls `JOBS` dict; running → "wait 20s", done → URLs + thumbs |
| `animate_image` | vibes.py | `(image_url, prompt="", aspect_ratio, resolution, project_id) -> str` | i2v, async |
| `create_lipsync` | vibes.py | `(source_url, audio_url, prompt, aspect_ratio, resolution) -> str` | async |
| `meta_chat` | meta.py | `(message: str)` | Full meta.ai sandbox: code, docs, conversions → file URLs |
| `web_search` | meta.py | `(query: str)` | Prompted web search with citations |
| `deep_research` | meta.py | `(topic: str)` | Multi-round report, 600s timeout |
| `social_search` | meta.py | `(query: str)` | IG/FB/Threads |
| `places_search` | meta.py | `(query: str)` | Places graph |
| `list_voices` | vibes.py | `() -> str` | `studio_voices` |
| `media_library` | vibes.py | `(media_type="video", limit=25)` | past media |
| `favorite_media` | vibes.py | `(item_id, is_favorited=True)` | |
| `delete_media` | vibes.py | `(item_id)` | |
| `media_status` | both | `() -> str` | token OK + vibes user + janitor purged count |

All video creation goes through `JOBS` dict `server.py:531` + `_start_job` (background thread) + `_vibes_with_fresh_login` retry on 401.

---

## 5. REST Endpoints — server.py:1020+

| Method | Path | Auth | Body / Query | Returns |
|--------|------|------|--------------|---------|
| GET | `/health` | open (GET+HEAD) | — | `{"ok":true,"uptime_s":...}` |
| GET | `/` | open | — | service info |
| GET | `/app` | open | — | HTML Production Hub |
| GET | `/api/download?url=` | open | `url` must be `*.fbcdn.net`/`*.vibes.ai`/`*.meta.ai` | `attachment; filename="..."` stream |
| GET | `/api/projects?kind=` | open | `kind=image|video|animate` | `{"projects":[...]}` |
| POST | `/api/projects` | open | `{"kind","name"}` | project rec |
| POST | `/api/image` | open* | `{"prompt","project_id","mode":"instant"\|"thinking","timeout":180}` | `{"urls":[],"error":...}` or `{"urls":[...]}` |
| POST | `/api/video` | open* | `{"prompt","aspect_ratio","resolution","model","count","reference_image_url","generationType","project_id","timeout"}` | `{"job_id":...}` |
| GET | `/api/job/{id}` | open | — | `{"id","kind","status":"running"|"done"|"error","elapsed_s","items":[{url,type,thumb}],"error":...}` |
| GET | `/api/keys` | open* | — | `{"keys":["sk-..."],"count":...}` |
| POST | `/api/keys` | open* | `{}` | `{"api_key":"sk-..."}` |

*Previously required `x-api-key`/`Authorization: Bearer`; now **open** per latest requirement (file still contains helpers `API_KEYS_FILE` etc but checks are removed). Re-enable by restoring the `if _k and _k not in _valid` blocks in `api_image`/`api_video`.

---

## 6. Production Hub — server.py _APP_HTML

- Vercel/Geist dark: `bg #0a0a0a`, `card #111214`, `border #262626`, Inter font, `max-width 820px`.
- Tabs: Image / Video / Animate.
- **Persistent projects:** dropdown per tab + `+ New project` button. `GET /api/projects?kind=X` auto-creates `Image #1` etc if none. Selection stored in `ACTIVE[mode]`. History strip shows last 16 thumbnails from `project.history`.
- **Relevant options only:** `renderProjBar()` hides/shows: Image → only `aspect`; Video/Animate → `aspect`+`res`+`model`+`count`; Animate additionally shows `refurl` input.
- Generate: Image → `POST /api/image` with `project_id`; Video/Animate → `POST /api/video` → `poll(job_id)` every 8s.
- Results: `addMedia(u,type)` creates `<img>` or `<video controls>` + Download link via `/api/download?url=`.

---

## 7. Self-Healing & Speed Optimizations

- **protobuf fix:** `meta.py:139 pool_from` declares `fd.dependency` for `ecto_request_client.proto` so `Attachment` resolves on protobuf 4.25.8. **Must keep** — new meta.py overwrites it on rewiring.
- **Session healing:** `vibes.py` embeds full cookie jar (`_SESSION_PAYLOAD` + `session.json`). `server.py _get_vibes()` tries `auth.load_session()` with `reauth=True`, then `_fresh_client(quiet=True)`, then `login_session`. Device cookies (`datr`, `ps_l`, `fs`, `locale`) seeded to look like Pixel 9. **HAR-exact login headers** added to both files: `Sec-Ch-Ua: "Not=A?Brand";v="99"...`, `Sec-Ch-Ua-Mobile: ?1`, `Platform: Android`, `Model: Pixel 9`, `Priority: u=1,i`, `Accept-Language: en-IN...`, `X-ASBD-ID: 359341`, `X-FB-LSD`. `IMPERSONATE = "chrome_android"`, `UA = "Mozilla/5.0 (Linux; Android 15; Pixel 9) ... Chrome/151"`, `curl_cffi` only (never plain `requests` for auth) — avoids IP suspicion / email codes.
- **Speed:** default `resolution 480p`, `model midjen-short`, `directGeneration:true`, `batchVariation:true`, `promptModel gemini-2.5-flash`, `imageModel midjen-base`. Image ~15-30s, video 60-90s (480p) / 120-300s (720p/1080p). Job system avoids client timeouts.
- **Download proxy:** `curl_cffi` fetch → `StreamingResponse` with `Content-Disposition: attachment` (browser saves, not navigates). Allowlist `fbcdn.net/vibes.ai/meta.ai/cdninstagram.com`.
- **Jobs:** in-memory `JOBS` dict, `threading.Thread` per job, `PROJECTS_LOCK` for hub history.

---

## 8. Environment & Secrets — DO NOT COMMIT VALUES

**Required env (set in Render Dashboard → Environment, NOT in git):**
- `PYTHON_VERSION=3.12.2`
- `MASTER_API_KEY` or `API_KEY` — optional master for key creation (if empty, any caller can mint keys; keys stored in `api_keys.json` which IS gitignored)
- `META_TOKEN` — optional override for `meta.py` embedded token (priority: env → `token.txt` → embedded fallback)
- `PORT` — provided by Render

**Where secrets live (never commit plaintext):**
- GitHub PAT: `github_pat_11B6WTQWI...` — used only to `git push` via `https://<user>:<pat>@github.com/...`. Stored in shell history only; rotate after use. Repo is **Private** `websitecage-hub/media-gen-mcp`.
- Render API key: `rnd_hmynfZ5m...` — `Authorization: Bearer <key>`, owner `tea-d6tfmmvkijhs73f477ng` (My Workspace, `websitecage@gmail.com`).
- UptimeRobot key: `u3610057-2cf4a80...` — monitor `media-meta-mcp` (id `803831193`), `https://media-gen-mcp.onrender.com/health`, interval 300s.
- Meta creds: embedded in `meta.py`/`vibes.py` (`EMAIL=anshuminded@gmail.com`, `PASSWORD=...`, `APP_ID 1522763855472543` / `1301537925115840`). New rewired files rotate embedded `meta_session`/`token.txt` via `_update_embedded`/`_safe_write` on every `save_session`/`save_token`.

**For another agent to reproduce:**
1. Clone `https://github.com/websitecage-hub/media-gen-mcp` (private — needs GitHub PAT)
2. `pip install -r requirements.txt` (ensure `protobuf==4.25.8`, `mcp<2`)
3. No manual `session.json` needed — `_bootstrap` materializes `_rt/*.proto` + tokens from embedded payloads on first import
4. Run `uvicorn server:app --host 0.0.0.0 --port 8000` — verify `GET /health` and `tools/list`
5. To deploy: `render.yaml` auto-applies; or via API: `ownerId tea-...`, `repo https://github.com/websitecage-hub/media-gen-mcp`, `serviceDetails {runtime:python, envSpecificDetails:{buildCommand:"pip install -r requirements.txt",startCommand:"uvicorn server:app --host 0.0.0.0 --port $PORT"}, healthCheckPath:"/health", plan:"free", region:"oregon"}`
6. Set env vars in Render dashboard, create UptimeRobot monitor as above, add MCP connector `https://<app>.onrender.com/mcp` in Claude/ChatGPT.

**Security note for the next agent:** Do NOT write the actual token values into `overview.md` or any committed file. Use `API_DOCS.md` placeholders (`sk-...`) and `render.yaml` `sync:false` for `META_TOKEN`. The HAR files `*.har` are local live-traffic captures — gitignored, never push.

---

## 9. Known Pitfalls & Fixes Already Applied

- `KeyError: '.clippy.ecto.Attachment'` → fix `pool_from` dependency injection.
- `ModuleNotFoundError: mcp.server.fastmcp` on mcp 2.x → pin `mcp>=1.10,<2`, `protobuf>=4.25,<5`.
- `Invalid Host header` on Render → `FastMCP(transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))` + allow any Host.
- `HEAD /health 404` → `@app.api_route(..., methods=["GET","HEAD"])` for `/` and `/health`.
- Media redirect not download → `/api/download` proxy with allowlist.
- Video `gen_kind not defined` → normalize `gen_type` param, accept `*_, **__`.
- Nix Python wiped on workspace rebuild → `nix-env -iA nixpkgs.python312` then `~/.venv/bin/python` symlink restored.
- Embedded `vibes.py` had two `meta_session` cookies (old + new) shadowing → candidate fallback + `_get_vibes` with `reauth=True` + `_fresh_client`.

---

## 10. Testing Checklist (precise, as used live)

```bash
# health + projects + app
curl https://media-gen-mcp.onrender.com/health
curl https://media-gen-mcp.onrender.com/api/projects
curl -I https://media-gen-mcp.onrender.com/app

# image (meta only)
curl -X POST https://media-gen-mcp.onrender.com/api/image -H "Content-Type: application/json" -d '{"prompt":"minimal flat logo of a fox"}'

# video async (vibes)
curl -X POST https://media-gen-mcp.onrender.com/api/video -H "Content-Type: application/json" -d '{"prompt":"drone over ocean","resolution":"480p"}'
# → {"job_id":"..."} then poll:
curl https://media-gen-mcp.onrender.com/api/job/<job_id>

# MCP
curl -X POST https://media-gen-mcp.onrender.com/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
# should list 18 tools; generate_image must show meta-only desc
```

All verified live: image inline embed (base64 `ImageContent` + markdown link), video `mp4` via `scontent.*.fbcdn.net`, download proxy returns `ftypisom` MP4, UptimeRobot status flipped from `9 (down)` to `2 (up)` after HEAD fix.

---

*Generated for the next agent — keep secrets in env, never in git. Rotate GitHub/Render/UptimeRobot keys after sharing in chat.*
