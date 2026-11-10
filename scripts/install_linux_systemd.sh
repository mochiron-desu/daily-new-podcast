#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$REPO_DIR/.venv/bin/python}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
WITH_STANDBY="${1:-}"

if [[ ! -x "$PYTHON_BIN" ]]; then
    echo "[ERROR] Python interpreter not found at $PYTHON_BIN" >&2
    echo "Create a virtualenv first (e.g. uv venv && uv pip install -r requirements.txt)" >&2
    exit 1
fi

mkdir -p "$UNIT_DIR"

cat > "$UNIT_DIR/daily-news-podcast-failover.service" <<EOF
[Unit]
Description=Daily News Podcast Local Failover Check
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
WorkingDirectory=$REPO_DIR
Environment=PYTHONPATH=$REPO_DIR
ExecStart=$PYTHON_BIN -m src.cli failover-check
EOF

cat > "$UNIT_DIR/daily-news-podcast-failover.timer" <<EOF
[Unit]
Description=Run Daily News Podcast Failover Check (morning cutoff + wake catch-up)

[Timer]
OnCalendar=*-*-* 06:20,06:35,06:50,07:15:00
Persistent=true
AccuracySec=1min

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now daily-news-podcast-failover.timer
echo "[OK] Installed and enabled daily-news-podcast-failover.timer"

if [[ "$WITH_STANDBY" == "--with-standby" ]]; then
    cat > "$UNIT_DIR/daily-news-podcast-standby.service" <<EOF
[Unit]
Description=Daily News Podcast Local Standby Peer & State Sync Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$REPO_DIR
Environment=PYTHONPATH=$REPO_DIR
ExecStart=$PYTHON_BIN -m src.cli serve
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
    systemctl --user enable --now daily-news-podcast-standby.service
    echo "[OK] Installed and started daily-news-podcast-standby.service"
fi

echo "Check status with: systemctl --user list-timers daily-news-podcast-*"

