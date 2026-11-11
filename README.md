# Telegram 超级群客服后台

一个后台可添加多台机器人。每个 Bot 独立配置 Token、客服超级群、客服名单和欢迎语。同一个超级群可以接入多个 Bot，后台按 Bot 和话题识别客户。

客户私聊 Bot 后，Bot 在超级群中创建独立话题，后续消息进入同一话题。配置名单中的客服在话题里发消息即可回复客户。支持文字和常见附件。

## Ubuntu 一键部署

在支持 systemd 的 Ubuntu root 终端运行：

```bash
setup=$(mktemp) && wget -O "$setup" https://raw.githubusercontent.com/ocsy22/telegram-support-hub/main/deploy.sh && bash "$setup"
```

输入 VPS 公网 IPv4 或后台域名。首次部署时自设管理员用户名和密码。安装完成后终端打印后台地址和用户名。IPv4 模式使用 TCP 8765，请在服务器防火墙和云安全组放行管理端 IP。HTTP 不加密登录信息和 Bot Token。域名模式使用 HTTPS，要求域名解析到 VPS 且公网可访问 TCP 80 和 443。

升级已有安装并重新设置后台账号时，在命令末尾给脚本传入公网 IP 和 `--configure-admin`。已有机器人的 Token 和欢迎语会保留。在后台为每个已有 Bot 填写客服超级群并保存，轮询才会恢复。

## 一键卸载

在 Ubuntu 的 root 终端粘贴整个命令块。它会停止客服服务、取消开机自启并移除程序。如果部署时启用了域名访问，也会移除本项目的 Caddy 配置并重新加载 Caddy。

```bash
bash <<'UNINSTALL'
set -e
unit=telegram-support-hub.service
if systemctl cat "$unit" >/dev/null 2>&1; then
  systemctl disable --now "$unit"
fi
rm -f -- "/etc/systemd/system/$unit" \
  "/etc/systemd/system/$unit.d/90-deploy-bind.conf" \
  /opt/telegram-support-hub/app.py
systemctl daemon-reload
site=/etc/caddy/telegram-support-hub.caddy
if [ -f /etc/caddy/Caddyfile ] && grep -Fxq "import $site" /etc/caddy/Caddyfile; then
  sed -i '\|^import /etc/caddy/telegram-support-hub\.caddy$|d' /etc/caddy/Caddyfile
  if systemctl is-active --quiet caddy; then
    systemctl reload caddy
  fi
fi
rm -f -- "$site"
UNINSTALL
```

卸载会保留管理员账号、Bot 配置、话题映射和日志，重装时可以继续使用。

## 接入客服超级群

1. 在 Telegram 创建超级群并开启「话题」。把机器人设为管理员，授予「管理话题」权限。
2. 登录后台，点击「添加机器人」。输入从 BotFather 获取的 Token、以 `-100` 开头的超级群数字 ID，以及客服的 Telegram 数字用户 ID。多个客服 ID 用逗号或空格分隔。
3. 用客户账号给 Bot 发消息。进入群中生成的客户话题，用配置名单里的客服账号发消息，客户会在私聊中收到回复。
4. 再次点击「添加机器人」可接入下一个 Bot。迁移已有 Bot 时，先停用旧轮询实例或移除旧 Webhook。

## 状态与排查

- 在「机器人管理」为每套配置保存备注，并查看最近轮询时间和连接状态。
- 在「运行日志」按机器人筛选。日志会记录消息转入话题、客服回复、名单或发言身份不匹配，以及 Telegram 请求失败。每个机器人保留最近 500 条，时间使用 UTC。
- 若第二套客服收得到消息但客户收不到回复，以个人身份在对应话题发送一条测试消息，然后查看该 Bot 的日志。若没有对应事件，检查该 Bot 是否仍是客服群管理员，以及其 Token 是否在其他服务器上运行。
- 系统日志可通过 `journalctl -u telegram-support-hub -f` 查看。

## 换服务器部署

在新 Ubuntu 服务器运行同一条安装命令，输入新服务器公网 IP 或域名，再自设后台账号。若需要迁移已有配置和话题映射，先停止旧机服务，再备份旧机的 `/var/lib/telegram-support-hub/support.sqlite3`。将数据库恢复到新机的同一路径时，请先停掉新机服务，并保留目标文件的权限与属主。原机器人与超级群无需改动，恢复后启动新机服务即可。

