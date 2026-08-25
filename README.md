# media-gen-mcp

A remote **MCP server** that lets Claude / ChatGPT generate **images** (meta.ai)
and **videos** (vibes.ai). Hosted on Render, kept alive 24/7 by UptimeRobot.

## Tools

| Tool | Backend | Returns |
|---|---|---|
| `generate_image(prompt)` | meta.ai | inline image + CDN URL(s) |
| `edit_image(image_url, instruction)` | meta.ai | edited image inline + URL |
| `transparent_image(prompt)` | meta.ai | transparent PNG asset |
| `make_gif(prompt)` | meta.ai | animated GIF |
| `meta_chat(message)` | meta.ai | full suite: sandbox files, docs, conversions |
| `web_search(query)` | meta.ai | cited web answer |
| `deep_research(topic)` | meta.ai | structured report |
| `social_search(query)` | meta.ai | IG/FB/Threads summary |
| `places_search(query)` | meta.ai | places w/ address/hours/price |
| `generate_video(prompt, ..., reference_image_url)` | vibes.ai | video URL(s) + poster preview |
| `animate_image(image_url, prompt)` | vibes.ai i2v | video URL |
| `create_lipsync(source_url, audio_url, prompt)` | vibes.ai | lip-synced video URL |
| `list_voices()` | vibes.ai | TTS voice IDs |
| `media_library(media_type, limit)` | vibes.ai | past media + IDs |
| `favorite_media(item_id, is_favorited)` | vibes.ai | ok |
| `delete_media(item_id)` | vibes.ai | ok |
| `media_status()` | both | session health |

Media delivery: images are embedded **inline** in the chat (MCP image
content blocks) *and* returned as public CDN URLs for download. A hourly
janitor purges stray temp files so Render's free disk never fills.

## Endpoints

- `GET /health` — keep-alive probe (UptimeRobot: HTTP monitor, every 5 min)
- `GET /` — info JSON
- `POST /mcp` — MCP Streamable HTTP transport (stateless)

## Run locally

```bash
pip install -r requirements.txt
python -m uvicorn server:app --host 0.0.0.0 --port 8000
```

## Deploy on Render

1. Push this repo to GitHub.
2. Render → New → Web Service → connect the repo.
   It reads `render.yaml`, or manually:
   - Build: `pip install -r requirements.txt`
   - Start: `uvicorn server:app --host 0.0.0.0 --port $PORT`
   - Health check path: `/health`
3. Optional env var: `META_TOKEN` (overrides embedded token).
4. Free plan sleeps after ~15 min idle → UptimeRobot pings `/health`.

## Connect clients

- **Claude**: Settings → Connectors → Add custom connector →
  `https://<your-app>.onrender.com/mcp`
- **ChatGPT**: Settings → Apps & Connectors → Create (Developer mode) →
  MCP Server URL: `https://<your-app>.onrender.com/mcp` (no auth)
