#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo bash install.sh" >&2
  exit 1
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install -d -m 0755 /opt/telegram-support-hub
install -m 0644 "$source_dir/app.py" /opt/telegram-support-hub/app.py
if ! command -v python3 >/dev/null; then
  apt-get update
  apt-get install -y python3
fi

install -d -m 0700 /etc/telegram-support-hub
config=/etc/telegram-support-hub/service.env
if [[ ! -f "$config" ]]; then
  (
    umask 077
    printf 'ADMIN_PASSWORD=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" > "$config"
  )
fi

cat > /etc/systemd/system/telegram-support-hub.service <<'UNIT'
[Unit]
Description=Telegram Support Hub
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
DynamicUser=yes
StateDirectory=telegram-support-hub
StateDirectoryMode=0700
Environment=DB_PATH=/var/lib/telegram-support-hub/support.sqlite3
EnvironmentFile=/etc/telegram-support-hub/service.env
ExecStart=/usr/bin/python3 /opt/telegram-support-hub/app.py
Restart=on-failure
RestartSec=3
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable telegram-support-hub.service
systemctl restart telegram-support-hub.service
for attempt in {1..20}; do
  if python3 -c 'import urllib.request, urllib.error
try:
    urllib.request.urlopen("http://127.0.0.1:8765/", timeout=1)
except urllib.error.HTTPError as exc:
    raise SystemExit(0 if exc.code == 401 else 1)' 2>/dev/null; then
    echo "Admin listening at http://127.0.0.1:8765/"
    echo "Username: admin. To read the password: sudo cat $config"
    exit 0
  fi
  sleep 1
done
journalctl -u telegram-support-hub.service -n 30 --no-pager >&2
exit 1
