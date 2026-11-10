# Autonomous Daily News Podcast Pipeline (`daily-news-podcast`)

An autonomous daily morning news briefing pipeline powered by **Google NotebookLM**, **Podbean**, and **ffmpeg**, designed for **Railway primary deployment** with **automatic local machine failover** (Linux & Windows) and zero extra cloud database costs.

## Architecture Overview

- **Primary (Railway)**: Runs a FastAPI server (`src.cli serve`) with a built-in daily scheduler (`06:00` by default), `/health` check, `/api/state/*` snapshots, and `/api/status/today`.
- **External Podcast Hosting (Podbean)**: Holds the MP3 files and public `feed.xml` so Spotify ingestion stays online even if Railway goes down.
- **Zero-Cost Continuity State (`storage/state.sqlite`)**: Each node stores `story_arcs` and `episodes` in SQLite and synchronizes JSON state via `/api/state/export` and `/api/state/merge` — no external paid database required.
- **Automatic Local Failover (Linux & Windows)**:
  - Local machine(s) run `python -m src.cli failover-check` via `systemd` timers (Linux) or Task Scheduler (Windows), or run `serve` in standby mode.
  - If Railway is unreachable past the cutoff window (`+20` mins), missed today's run, or marked today as `failed` (for instance, Google NotebookLM cookies expired on Railway's datacenter IP), the highest-priority local machine acquires the run lock, verifies Podbean has no duplicate episode for today, generates & publishes the podcast, and pushes updated `story_arcs` back when Railway returns.

---

## 1. Local Setup (Linux / macOS / Windows)

```bash
# Using uv (recommended)
uv venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
uv pip install -r requirements.txt

# Optional: if you need interactive browser login on this machine
uv pip install "notebooklm-py[browser]"
playwright install chromium
notebooklm login
```

Create `.env` from `.env.example`:

```bash
cp .env.example .env
```

Verify tools and credentials:

```bash
python -m src.cli doctor
```

Test harvesting, story arc matching, dossier PDF generation, and prompt building without calling NotebookLM or Podbean:

```bash
python -m src.cli run-once --dry-run
```

---

## 2. Deploying Primary to Railway

1. Create a Railway project from this repository (uses `Dockerfile` and `railway.toml`).
2. *(Recommended)* Attach a Railway **Volume** mounted at `/app/storage` so `state.sqlite` and rotated cookies persist across redeploys.
3. Encode your local NotebookLM session for Railway:
   ```bash
   base64 -w 0 ~/.notebooklm/profiles/default/storage_state.json
   ```
4. Set the following **Railway Service Variables**:
   - `NODE_ROLE=primary`
   - `NODE_ID=railway-primary`
   - `NODE_PRIORITY=0`
   - `API_TOKEN=<generate-a-long-random-secret>`
   - `STORAGE_DIR=/app/storage`
   - `TIMEZONE=Asia/Kolkata`
   - `PRIMARY_RUN_TIME=06:00`
   - `NOTEBOOKLM_AUTH_B64=<paste base64 output>`
   - `NOTEBOOKLM_STORAGE_PATH=/app/storage/notebooklm_storage_state.json`
   - `PUBLISHER_BACKEND=podbean`
   - `PODBEAN_CLIENT_ID=<your-podbean-client-id>`
   - `PODBEAN_CLIENT_SECRET=<your-podbean-client-secret>`

Railway will automatically check `GET /health` via `railway.toml`.

---

## 3. Setting Up Local Failover Machine(s)

On each local machine (Linux and/or Windows), set `.env`:

```env
NODE_ROLE=fallback
NODE_ID=my-linux-laptop       # unique per machine
NODE_PRIORITY=10              # lower number runs first (e.g. desktop=5, laptop=10)
API_TOKEN=<same-secret-as-railway>
PRIMARY_URL=https://<your-app>.up.railway.app
PEER_URLS=                    # optional comma-separated Tailscale/LAN URLs of sibling local machines
PODBEAN_CLIENT_ID=<your-podbean-client-id>
PODBEAN_CLIENT_SECRET=<your-podbean-client-secret>
```

### Linux (`systemd` user timer)

```bash
chmod +x scripts/install_linux_systemd.sh
./scripts/install_linux_systemd.sh
# Add --with-standby to also run the local HTTP peer server for Tailscale peer election:
./scripts/install_linux_systemd.sh --with-standby
```

### Windows (Task Scheduler)

Run in PowerShell from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_task.ps1
# Add -WithStandbyDaemon to also run the local HTTP peer server at logon:
powershell -ExecutionPolicy Bypass -File .\scripts\install_windows_task.ps1 -WithStandbyDaemon
```

---

## 4. Useful CLI Commands

| Command | Purpose |
|---|---|
| `python -m src.cli doctor [--live]` | Verify `ffmpeg`, NotebookLM auth, Podbean credentials, and Railway/peer reachability |
| `python -m src.cli run-once [--dry-run]` | Run today's episode pipeline immediately |
| `python -m src.cli failover-check [--force]` | Probe Railway + peers and run locally if Railway failed/missed cutoff |
| `python -m src.cli sync-state` | Pull & merge `story_arcs` and `episodes` from Railway / peers |
| `python -m src.cli status` | Print JSON status of today's episode and active developing story arcs |
| `python -m src.cli serve` | Start the FastAPI server + background scheduler |

