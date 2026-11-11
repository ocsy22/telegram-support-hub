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

## 接入客服超级群

1. 在 Telegram 创建超级群并开启「话题」。把机器人设为管理员，授予「管理话题」权限。
2. 登录后台，点击「添加机器人」。输入从 BotFather 获取的 Token、以 `-100` 开头的超级群数字 ID，以及客服的 Telegram 数字用户 ID。多个客服 ID 用逗号或空格分隔。
3. 用客户账号给 Bot 发消息。进入群中生成的客户话题，用配置名单里的客服账号发消息，客户会在私聊中收到回复。
4. 再次点击「添加机器人」可接入下一个 Bot。迁移已有 Bot 时，先停用旧轮询实例或移除旧 Webhook。

服务日志可通过 `journalctl -u telegram-support-hub -f` 查看。SQLite 数据存放在 `/var/lib/telegram-support-hub/`，管理员配置存放在 `/etc/telegram-support-hub/service.env`。

