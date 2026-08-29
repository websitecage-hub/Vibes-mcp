# Media Gen MCP — API Documentation

Base URL: `https://media-gen-mcp.onrender.com`

All endpoints are **open** (no API key required). For external integrations, you may still send `x-api-key: sk-...` or `Authorization: Bearer sk-...` — if provided, it is validated; if omitted, the request is allowed (browser `/app` compat).

---

## 1. Health & Info

### GET /health
Keep-alive probe (UptimeRobot). Supports `GET` and `HEAD`.

```bash
curl https://media-gen-mcp.onrender.com/health
# {"ok":true,"uptime_s":1234}
```

### GET /
Service info.

```bash
curl https://media-gen-mcp.onrender.com/
# {"service":"media-gen-mcp","status":"running","mcp_endpoint":"/mcp","tools":[...],"web_app":"/app"}
```

### GET /app
Production Hub UI — Image / Video / Animate with persistent projects. Open in browser.

---

## 2. Projects (Persistent Context)

Projects keep style/characters consistent across generations.

### GET /api/projects?kind=image|video|animate
List hub projects. Omit `kind` for all.

```bash
curl "https://media-gen-mcp.onrender.com/api/projects?kind=image"
# {"projects":[{"id":"abc123def4","kind":"image","name":"Image #1","conversation_id":"...","vibes_project_id":"...","history":[...],"created":1234567890}]}
```

### POST /api/projects
Create a new project.

**Body:**
```json
{"kind":"image","name":"My Comic Series"}
```
`kind`: `image` | `video` | `animate` (default `image`)

```bash
curl -X POST https://media-gen-mcp.onrender.com/api/projects \
  -H "Content-Type: application/json" \
  -d '{"kind":"video","name":"Ad Campaign"}'
# {"id":"xyz","kind":"video","name":"Ad Campaign",...}
```

Pass `project_id` in image/video calls to stay in that project's context. Omit for one-off.

---

## 3. Image Generation — Meta AI (meta.py only)

### POST /api/image
**Every feature exposed — leave nothing behind.**

**Body:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | yes | Full description: subject, style, lighting, mood |
| `project_id` | string | no | Hub project id for context (same style across calls) |
| `mode` | string | no | `instant` (default, fast) or `thinking` (higher quality, slower) |
| `timeout` | int | no | Seconds to wait for Meta, default 180 |

**Response 200:**
```json
{"urls":["https://scontent.xx.fbcdn.net/...webp"],"text":"Here is your image...","conversation_id":"..."}
```
**Response 200 with error (blocked prompt):**
```json
{"urls":[],"error":"I can't generate that image — it references protected characters..."}
```

**cURL:**
```bash
curl -X POST https://media-gen-mcp.onrender.com/api/image \
  -H "Content-Type: application/json" \
  -d '{"prompt":"minimal flat logo of a fox, orange and white","mode":"instant"}'
```

**JS:**
```js
const r = await fetch("https://media-gen-mcp.onrender.com/api/image", {
  method:"POST", headers:{"Content-Type":"application/json"},
  body: JSON.stringify({prompt:"cyberpunk cat", project_id:"abc123"})
});
const {urls, error} = await r.json();
```

---

## 4. Video Generation — Vibes AI (vibes.py)

### POST /api/video
**Async dispatch+poll — returns `job_id` instantly (no timeouts). Every vibes feature exposed.**

**Body:**
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | yes | What to generate |
| `project_id` | string | no | Hub project id |
| `aspect_ratio` | string | no | `9:16` (default), `16:9`, `1:1`, `4:5`, `3:4`, `4:3` — also accepts `aspect` |
| `resolution` | string | no | `480p` (fast, default), `720p`, `1080p` |
| `model` | string | no | `midjen-short` (fast default), `midjen-long`, `midjen`, `meta-juggernaut`, `sora`, `veo` — also `videoModel` |
| `count` | int | no | 1-4 variations, also `n` |
| `reference_image_url` | string | no | Public image URL for image-to-video (also `ref_url`) |
| `generationType` | string | no | `t2v` (default), `i2v` (auto when ref provided), also `gen_type` |
| `timeout` | int | no | Max wait for completion on poll, default 420 |

**Response:**
```json
{"job_id":"abc123def456","project_id":"xyz"}
```

**cURL:**
```bash
curl -X POST https://media-gen-mcp.onrender.com/api/video \
  -H "Content-Type: application/json" \
  -d '{"prompt":"drone over ocean at sunrise, cinematic","aspect_ratio":"16:9","resolution":"720p","model":"midjen-short","count":1}'
```

### GET /api/job/{job_id}
Poll until `status` is `done`.

**Response:**
```json
{"id":"abc123","kind":"video","status":"running","elapsed_s":12}
{"id":"abc123","kind":"video","status":"done","elapsed_s":87,"items":[{"url":"https://scontent...mp4","type":"video","thumb":"https://...jpg"}]}
{"id":"abc123","kind":"video","status":"error","error":"..."}
```

**Poll loop (JS):**
```js
const {job_id} = await fetch("/api/video",{method:"POST",body:JSON.stringify({prompt})}).then(r=>r.json());
while(true){
  const j = await fetch(`/api/job/${job_id}`).then(r=>r.json());
  if(j.status==="running"){ await new Promise(r=>setTimeout(r,8000)); continue; }
  if(j.status==="done") console.log(j.items);
  break;
}
```

**With reference image:**
```bash
curl -X POST https://media-gen-mcp.onrender.com/api/video \
  -d '{"prompt":"animate this image","reference_image_url":"https://example.com/photo.jpg"}'
```

---

## 5. Download Proxy

### GET /api/download?url=<encoded_cdn_url>
Streams the file with `Content-Disposition: attachment` so the browser saves it.

Allowed hosts: `*.fbcdn.net`, `*.vibes.ai`, `*.meta.ai`, `*.cdninstagram.com`

```bash
curl -L "https://media-gen-mcp.onrender.com/api/download?url=$(python3 -c 'import urllib.parse; print(urllib.parse.quote("https://scontent...mp4"))')" -o video.mp4
```

UI uses this automatically for Download buttons.

---

## 6. MCP (for Claude / ChatGPT)

**Endpoint:** `POST https://media-gen-mcp.onrender.com/mcp` — Streamable HTTP, stateless, no auth.

Add as custom connector in Claude (Settings → Connectors) or ChatGPT (Developer mode → Apps & Connectors).

**Tools (18):**
`generate_image(prompt, project_id)`, `edit_image(image_url, instruction)`, `transparent_image(prompt)`, `make_gif(prompt)`, `generate_video(prompt, aspect_ratio, resolution, model, count, reference_image_url, project_id)` → `job_id`, `check_generation(job_id)`, `animate_image(image_url, prompt, aspect_ratio, resolution, project_id)`, `create_lipsync(source_url, audio_url, prompt)`, `meta_chat(message)`, `web_search(query)`, `deep_research(topic)`, `social_search(query)`, `places_search(query)`, `list_voices()`, `media_library(media_type, limit)`, `favorite_media(item_id, is_favorited)`, `delete_media(item_id)`, `media_status()`

Video tools are async: call `generate_video` → get `job_id` → poll `check_generation` every ~20s.

---

## 7. Optional API Keys

APIs are open, but you can create keys for tracking:

```bash
curl -X POST https://media-gen-mcp.onrender.com/api/keys -H "Content-Type: application/json" -d '{}'
# {"api_key":"sk-..."}

curl https://media-gen-mcp.onrender.com/api/keys -H "x-api-key: sk-..."
# {"keys":["sk-JjZ0...m0vk"],"count":1}
```

If you set `MASTER_API_KEY` env on Render, only that key can create new keys. Otherwise any caller can mint one. Send as `x-api-key` or `Authorization: Bearer sk-...`.

---

## 8. Errors & Stability

- **Image blocked (IP/trademark):** `{"urls":[],"error":"I can't generate that — references protected characters..."}` — rephrase with original descriptions (e.g., "red-gold armored hero" not "Iron Man").
- **Video 401 once:** auto-heals — password flow with Pixel 9 Android headers (`sec-ch-ua*`) replicates your successful HAR, so Meta doesn't flag the IP. Just retry the job after ~5s.
- **Render free spins down:** UptimeRobot pings `/health` every 5 min (already configured) — first request after sleep may take 30s.

---

## 9. Full Example — Production App

```js
// 1. Create / get project
let proj = await fetch("/api/projects?kind=video").then(r=>r.json());
let pid = proj.projects[0]?.id || await fetch("/api/projects",{method:"POST",body:JSON.stringify({kind:"video",name:"Campaign"})}).then(r=>r.json()).then(j=>j.id);

// 2. Image
let img = await fetch("/api/image",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({prompt:"futuristic city at dusk", project_id:pid})}).then(r=>r.json());

// 3. Video (async)
let {job_id} = await fetch("/api/video",{method:"POST",body:JSON.stringify({prompt:"city flythrough", project_id:pid})}).then(r=>r.json());
let job;
do{ await new Promise(r=>setTimeout(r,10000)); job = await fetch(`/api/job/${job_id}`).then(r=>r.json()); } while(job.status==="running");
console.log(job.items[0].url);
```

Hosted on Render Free (Oregon) — `render.yaml` included, auto-deploys on `git push`.
