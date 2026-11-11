#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run with sudo bash install.sh" >&2
  exit 1
fi

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
if ! command -v python3 >/dev/null; then
  apt-get update
  apt-get install -y python3
fi

install -d -m 0700 /etc/telegram-support-hub
config=/etc/telegram-support-hub/service.env
if [[ ! -f "$config" || "${1:-}" == --configure-admin ]]; then
  read -r -p '管理员用户名: ' admin_user </dev/tty
  if [[ ! "$admin_user" =~ ^[A-Za-z0-9_.-]{1,64}$ ]]; then
    echo '用户名只接受字母、数字、点、下划线和连字符。' >&2
    exit 1
  fi
  read -rs -p '管理员密码（至少 8 个字符）: ' admin_password </dev/tty
  echo
  read -rs -p '再次输入密码: ' admin_confirm </dev/tty
  echo
  if [[ ${#admin_password} -lt 8 || "$admin_password" != "$admin_confirm" ]]; then
    echo '密码太短或两次输入不一致。' >&2
    exit 1
  fi
  escaped=${admin_password//\\/\\\\}
  escaped=${escaped//\"/\\\"}
  temp_config=$(mktemp /etc/telegram-support-hub/.service.XXXXXX)
  printf 'ADMIN_USERNAME=%s\nADMIN_PASSWORD="%s"\n' "$admin_user" "$escaped" > "$temp_config"
  mv -f -- "$temp_config" "$config"
  unset admin_password admin_confirm escaped
fi

install -d -m 0755 /opt/telegram-support-hub
install -m 0644 "$source_dir/app.py" /opt/telegram-support-hub/app.py

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
    exit 0
  fi
  sleep 1
done
journalctl -u telegram-support-hub.service -n 30 --no-pager >&2
exit 1
