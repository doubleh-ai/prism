<p align="center">
  <img src="https://img.shields.io/badge/Prism-AI%20Research-3b5d8a?style=flat" alt="Prism">
  <img src="https://img.shields.io/badge/license-Apache%202.0-blue" alt="License">
</p>

# Prism

> AI-powered investment research platform. Refined, fast, built for the Chinese A-share market.

Prism is a self-hosted web application that combines a clean editing-terminal aesthetic with Claude Code's multi-skill AI architecture. It streams live analysis — company deep-dives, supply-chain bottlenecks, macro research — directly to your browser via SSE.

---

## Architecture

```
Browser (Vanilla JS + SSE)  →  FastAPI (Python)  →  Claude Code CLI
                                   │
                            SQLite (WAL mode)
```

- **Frontend** — No framework. Vanilla HTML/CSS/JS, custom CSS variable–driven dark/light themes, stream-typing effect, glossary overlay, term explanations
- **Backend** — FastAPI + SQLite WAL, bcrypt auth, per‑provider API keys, rate limiting
- **AI Engine** — Claude Code CLI spawned as a subprocess; multi‑skill support via `--append-system-prompt`
- **Deploy** — Nginx reverse proxy + systemd on Ubuntu

## Quick Start

```bash
# Clone
git clone https://github.com/doubleh-ai/prism.git /home/ubuntu/prism
cd /home/ubuntu/prism

# Install
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# → Edit .env with your API keys

# Deploy
ADMIN_USER=admin ADMIN_PASS=your-password bash install.sh
```

### Environment Variables

| Variable | Description |
|---|---|
| `DEEPSEEK_API_KEY` | DeepSeek platform API key |
| `MINIMAX_API_KEY` | MiniMax platform API key |
| `LLM_API_KEY` | Legacy fallback (used if per‑provider keys are unset) |
| `STOCK_DATA_DIR` | Data directory (default: `./data`) |
| `RATE_CHAT_PER_MIN` | Chat rate limit (default: 20) |

Full list in `.env.example`.

## Skills

Prism uses Claude Code skills for domain‑specific analysis:

| Skill | Description |
|---|---|
| `bottleneck-hunter` | Supply-chain bottleneck analysis |
| `serenity` | Card-point / thesis analysis |
| `cn` (force-chinese) | Always‑on — enforces simplified Chinese output |

Skills are selected via the UI toolbar. Multiple skills can be active simultaneously — the backend merges them with `--append-system-prompt`.

See `skills/CLAUDE.md` for full setup instructions.

## Models

Multiple model providers supported with automatic key routing:

| Provider | Models |
|---|---|
| **DeepSeek** | V4 Pro, V4 Flash |
| **MiniMax** | M3, M2.7, M2.7 Fast (default: M3) |

Switch models in the UI dropdown. Keys are per‑provider; the backend routes to the correct endpoint automatically.

## Project Structure

```
├── server/           FastAPI backend (auth, chat, conversations, proxy)
├── static/           CSS (variable-driven themes), JS (SPA), libs
├── templates/        app.html (main), login.html
├── skills/           Claude Code skill manifests + force-chinese
├── config/           nginx, systemd, example Claude settings
├── scripts/          deploy helpers, SSL patch, smoke test
└── tests/            pytest suite (auth, API, frontend, integration)
```

## Deployment

```bash
# Push to deploy-tmp → server pulls
git push origin main:deploy-tmp

# On server
cd /home/ubuntu/prism
git reset --hard deploy-tmp
sudo systemctl restart prism.service
```

Or use the included `install.sh` for a full one‑command setup.

## License

Apache 2.0 — see [LICENSE](LICENSE)
