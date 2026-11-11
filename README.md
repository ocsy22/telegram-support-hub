# Telegram 多 Bot 客服后台

一个网页后台管理多个 Telegram Bot，每个 Bot 可配置多名客服、欢迎语和启用状态。客户私聊 Bot 后，文字和常见媒体会复制给该 Bot 的所有客服。任一客服在 Telegram 内回复那条消息，Bot 会把回复发回对应客户。多位客服可以同时回复同一个客户。

## Ubuntu 一键部署

先把后台域名的 DNS 记录指向 VPS，并放行公网 TCP 80 和 443。使用支持 systemd 的 Ubuntu。Caddy 自动申请 HTTPS 证书，后台使用安装时生成的管理员密码。

在 Ubuntu 的 root 终端粘贴这一条命令：

```bash
wget -qO /tmp/telegram-support-deploy.sh https://raw.githubusercontent.com/ocsy22/telegram-support-hub/main/deploy.sh && bash /tmp/telegram-support-deploy.sh
```

按提示填写后台域名。也可以在命令末尾指定域名，例如 `bash /tmp/telegram-support-deploy.sh support.example.com`。部署检查通过后，终端会输出 HTTPS 地址、用户名 `admin` 和密码。重复部署会保留现有数据库和密码。

若 VPS 已有站点，请确认 Caddy 可以监听 80 和 443，且后台域名未被其他站点占用。已有反向代理的服务器也可运行 `sudo bash install.sh`，然后将该域名的 HTTPS 流量转发至 `127.0.0.1:8765`。

## 配置 Bot 和客服

1. 在 @BotFather 创建 Bot，把 Token 填入网页后台。
2. 填写客服的 Telegram 数字用户 ID，用逗号或空格隔开。每位客服要先打开对应 Bot 并发送 `/start`。
3. 从另一个 Telegram 账号给 Bot 发消息，客服使用 Telegram 的「回复」功能回复收到的客户消息。
4. 配置其他 Bot 时，重复添加流程。已有 Bot 可编辑名单、欢迎语或停用，Token 留空可保留原值。

本服务使用长轮询。迁移已有 Bot 时，先停止原来的轮询程序或在旧服务中移除 Webhook。每个 Token 只运行一份轮询实例。

## 数据和排查

- 服务日志：`sudo journalctl -u telegram-support-hub -f`
- Bot Token 和回复路由存放在服务器本机 SQLite 数据库 `/var/lib/telegram-support-hub/support.sqlite3`。备份时请保护数据库和环境配置文件。
- Telegram 通信失败时服务会重试轮询。若进程在发送消息后、保存更新进度前退出，重启后可能重复投递该条消息。
