#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
  echo '请用 root 或 sudo 运行部署命令。' >&2
  exit 1
fi

domain=${1:-}
if [[ -z "$domain" ]]; then
  read -r -p '请输入已解析到 VPS 的后台域名（例如 support.example.com）: ' domain </dev/tty
fi
if [[ ! "$domain" =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z][a-zA-Z0-9-]{1,62}$ ]]; then
  echo '请输入完整域名。' >&2
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl python3 caddy

work=$(mktemp -d)
trap 'rm -f -- "$work/app.py" "$work/install.sh" "$work/site.caddy"; rmdir -- "$work"' EXIT
source_url=https://raw.githubusercontent.com/ocsy22/telegram-support-hub/main
for file in app.py install.sh; do
  curl -fsSL --retry 3 "$source_url/$file" -o "$work/$file"
done
bash "$work/install.sh"

caddyfile=/etc/caddy/Caddyfile
site=/etc/caddy/telegram-support-hub.caddy
printf '%s {\n    reverse_proxy 127.0.0.1:8765\n}\n' "$domain" > "$work/site.caddy"
install -m 0644 "$work/site.caddy" "$site"
if ! grep -Fxq "import $site" "$caddyfile"; then
  printf '\nimport %s\n' "$site" >> "$caddyfile"
fi
caddy validate --config "$caddyfile" --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy

url="https://$domain/"
for attempt in {1..24}; do
  status=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 3 "$url" 2>/dev/null || true)
  if [[ "$status" == 401 ]]; then
    printf '\n后台地址 %s\n用户名 admin\n' "$url"
    cat /etc/telegram-support-hub/service.env
    exit 0
  fi
  sleep 5
done

printf '服务已安装，但 %s 尚未通过 HTTPS 检查。请检查域名解析和公网 TCP 80/443，然后重试。\n' "$url" >&2
exit 1
