# Telegram 多 Bot 客服后台

一个网页后台管理多个 Telegram Bot，每个 Bot 可配置多名客服、欢迎语和启用状态。客户私聊 Bot 后，文字和常见媒体会复制给该 Bot 的所有客服。任一客服在 Telegram 内回复那条消息，Bot 会把回复发回对应客户。多位客服可以同时回复同一个客户。

## Ubuntu 安装

将本目录上传到 Ubuntu，进入目录后运行：

```bash
sudo bash install.sh
```

需要 Ubuntu（systemd、Python 3.10+）及到 Telegram Bot API 的 HTTPS 网络。重复运行安装命令会更新程序并保留数据库和管理员密码。服务开机自启。

从自己的电脑连接后台：

```bash
ssh -N -L 8765:127.0.0.1:8765 用户名@服务器地址
```

保持 SSH 连接，在浏览器打开 http://127.0.0.1:8765/ 。用户名为 `admin`，在服务器执行 `sudo cat /etc/telegram-support-hub/service.env` 查看密码。后台只监听 Ubuntu 的本地回环地址。若要直接通过域名访问，可在服务器上使用 HTTPS 反向代理。

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
