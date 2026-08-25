# media-gen-mcp

A remote **MCP server** that lets Claude / ChatGPT generate **images** (meta.ai)
and **videos** (vibes.ai). Hosted on Render, kept alive 24/7 by UptimeRobot.

## Tools

| Tool | Backend | Returns |
|---|---|---|
| `generate_image(prompt)` | meta.ai | public image CDN URL(s) |
| `generate_video(prompt, ..., reference_image_url)` | vibes.ai | public video CDN URL(s) |
| `animate_image(image_url, prompt)` | vibes.ai i2v | video CDN URL |
| `create_lipsync(source_url, audio_url, prompt)` | vibes.ai | lip-synced video URL |
| `list_voices()` | vibes.ai | TTS voice IDs |
| `media_library(media_type, limit)` | vibes.ai | past media + IDs |
| `favorite_media(item_id, is_favorited)` | vibes.ai | ok |
| `delete_media(item_id)` | vibes.ai | ok |
| `media_status()` | both | session health |

Media is never stored on the server — backends return public CDN URLs, so
nothing to clean up.

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
