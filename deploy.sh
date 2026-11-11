#!/usr/bin/env bash
set -euo pipefail

if (( EUID != 0 )); then
  echo '请用 root 或 sudo 运行部署命令。' >&2
  exit 1
fi

address=${1:-}
if [[ -z "$address" ]]; then
  read -r -p '请输入 VPS 公网 IPv4 或后台域名: ' address </dev/tty
fi
if [[ "$address" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  direct_ip=1
elif [[ "$address" =~ ^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z][a-zA-Z0-9-]{1,62}$ ]]; then
  direct_ip=0
else
  echo '请输入公网 IPv4 或完整域名。' >&2
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl python3
if (( direct_ip )); then
  if ! python3 -c 'import ipaddress, sys; sys.exit(not ipaddress.IPv4Address(sys.argv[1]).is_global)' "$address"; then
    echo '请输入有效的公网 IPv4。' >&2
    exit 1
  fi
else
  apt-get install -y caddy
fi

work=$(mktemp -d)
trap 'rm -f -- "$work/app.py" "$work/install.sh" "$work/site.caddy"; rmdir -- "$work"' EXIT
source_url=https://raw.githubusercontent.com/ocsy22/telegram-support-hub/main
for file in app.py install.sh; do
  curl -fsSL --retry 3 "$source_url/$file" -o "$work/$file"
done
override=/etc/systemd/system/telegram-support-hub.service.d/90-deploy-bind.conf
if (( direct_ip )); then
  install -d -m 0755 "$(dirname -- "$override")"
  printf '[Service]\nEnvironment=BIND_HOST=0.0.0.0\n' > "$override"
else
  rm -f -- "$override"
fi
bash "$work/install.sh"

if (( direct_ip )); then
  printf '\n后台地址 http://%s:8765/\n用户名 admin\n' "$address"
  cat /etc/telegram-support-hub/service.env
  printf '请在 VPS 防火墙和云安全组放行 TCP 8765。HTTP 登录和 Bot Token 配置均为明文传输，建议仅允许管理端 IP 访问。\n'
  exit 0
fi

caddyfile=/etc/caddy/Caddyfile
site=/etc/caddy/telegram-support-hub.caddy
printf '%s {\n    reverse_proxy 127.0.0.1:8765\n}\n' "$address" > "$work/site.caddy"
install -m 0644 "$work/site.caddy" "$site"
if ! grep -Fxq "import $site" "$caddyfile"; then
  printf '\nimport %s\n' "$site" >> "$caddyfile"
fi
caddy validate --config "$caddyfile" --adapter caddyfile
systemctl enable --now caddy
systemctl reload caddy

url="https://$address/"
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
