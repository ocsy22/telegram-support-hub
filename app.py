import base64
import hmac
import html
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

DATA = os.environ.get("DB_PATH", "support.sqlite3")
TOKEN_PATTERN = re.compile(r"([1-9][0-9]{4,15}):[A-Za-z0-9_-]{20,100}")
CSRF = secrets.token_urlsafe(32)
WELCOME = "您好，请直接发送您的问题，客服会在这里回复您。"
FORMAT = {"text", "photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "contact", "location"}
POLL_STATE = {}


@contextmanager
def db():
    connection = sqlite3.connect(DATA, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def init():
    with db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS bots (
                id INTEGER PRIMARY KEY, token TEXT NOT NULL, username TEXT NOT NULL,
                agents TEXT NOT NULL, welcome TEXT NOT NULL, enabled INTEGER NOT NULL,
                offset INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS topics (
                bot INTEGER NOT NULL, chat INTEGER NOT NULL, customer INTEGER NOT NULL,
                thread INTEGER NOT NULL,
                PRIMARY KEY (bot, chat, customer), UNIQUE (bot, chat, thread)
            );
            CREATE TABLE IF NOT EXISTS relay_messages (
                bot INTEGER NOT NULL, chat INTEGER NOT NULL, message INTEGER NOT NULL,
                thread INTEGER NOT NULL, PRIMARY KEY (bot, chat, message)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, bot INTEGER NOT NULL, at TEXT NOT NULL,
                level TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS events_bot_id ON events(bot, id DESC);
        """)
        conn.execute("BEGIN IMMEDIATE")
        columns = {column["name"] for column in conn.execute("PRAGMA table_info(bots)")}
        if "support_group" not in columns:
            conn.execute("ALTER TABLE bots ADD COLUMN support_group INTEGER")
        if "note" not in columns:
            conn.execute("ALTER TABLE bots ADD COLUMN note TEXT NOT NULL DEFAULT ''")


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def event(bot, detail, level="信息"):
    with db() as conn:
        conn.execute("INSERT INTO events(bot, at, level, detail) VALUES (?, ?, ?, ?)",
                     (bot, timestamp(), level, detail))
        conn.execute("""DELETE FROM events WHERE bot=? AND id < (
            SELECT id FROM events WHERE bot=? ORDER BY id DESC LIMIT 1 OFFSET 499
        )""", (bot, bot))


class TelegramError(Exception):
    pass


def api(token, method, **data):
    request = Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(data).encode(), headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=35 if method == "getUpdates" else 8) as response:
            result = json.load(response)
        if result.get("ok"):
            return result["result"]
    except HTTPError as exc:
        # Never log request URLs, response bodies or Bot Tokens.
        reason = {401: "检查 Token", 403: "检查机器人权限或客户是否屏蔽了机器人",
                  409: "检查同一 Token 是否有其他轮询实例或 Webhook", 429: "Telegram 限流"}.get(exc.code)
        suffix = f" · {reason}" if reason else ""
        raise TelegramError(f"{method} HTTP {exc.code}{suffix}") from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise TelegramError(f"{method} 网络或响应异常") from None
    raise TelegramError(f"{method} 请求失败")


def relay(bot, message):
    chat = message["chat"]
    group = bot["support_group"]
    if not group:
        return
    token = bot["token"]
    source = chat["id"]
    if chat["type"] == "private":
        if message.get("text") == "/start":
            api(token, "sendMessage", chat_id=source, text=bot["welcome"])
            return
        if not FORMAT.intersection(message):
            api(token, "sendMessage", chat_id=source, text="请发送文字或常见附件。")
            return
        try:
            with db() as conn:
                topic = conn.execute("SELECT thread FROM topics WHERE bot=? AND chat=? AND customer=?",
                                     (bot["id"], group, source)).fetchone()
            if topic:
                thread = topic["thread"]
            else:
                name = message.get("from", {}).get("first_name") or "客户"
                title = f'{bot["username"]} · {name} · {source}'[:128]
                thread = api(token, "createForumTopic", chat_id=group, name=title)["message_thread_id"]
                with db() as conn:
                    conn.execute("INSERT INTO topics VALUES (?, ?, ?, ?)",
                                 (bot["id"], group, source, thread))
            copied = api(token, "copyMessage", chat_id=group, message_thread_id=thread,
                         from_chat_id=source, message_id=message["message_id"])
            with db() as conn:
                conn.execute("INSERT OR REPLACE INTO relay_messages VALUES (?, ?, ?, ?)",
                             (bot["id"], group, copied["message_id"], thread))
        except TelegramError as exc:
            event(bot["id"], f"客户消息转入群失败 · {exc}", "错误")
            api(token, "sendMessage", chat_id=source, text="客服暂时无法接收消息，请稍后重试。")
            return
        event(bot["id"], f"客户消息已转入话题 {thread}")
        return

    if chat["type"] != "supergroup" or source != group:
        return
    sender = message.get("from", {})
    if sender.get("is_bot") or not FORMAT.intersection(message):
        return
    thread = message.get("message_thread_id")
    reply = message.get("reply_to_message") or {}
    with db() as conn:
        if not thread and reply.get("message_id"):
            mapped = conn.execute(
                "SELECT thread FROM relay_messages WHERE bot=? AND chat=? AND message=?",
                (bot["id"], group, reply["message_id"]),
            ).fetchone()
            if mapped:
                thread = mapped["thread"]
        route = conn.execute(
            "SELECT customer FROM topics WHERE bot=? AND chat=? AND thread=?",
            (bot["id"], group, thread),
        ).fetchone()
    if not route:
        event(bot["id"], f"群消息未匹配客户话题 · 话题 {thread or '未提供'}", "提示")
        return
    if message.get("sender_chat") or sender.get("id") not in json.loads(bot["agents"]):
        reason = "请切换为个人身份发言" if message.get("sender_chat") else f"发送者 ID {sender.get('id', '未知')} 未在客服名单"
        event(bot["id"], f"话题 {thread} 回复已跳过 · {reason}", "警告")
        return
    try:
        if "text" in message:
            api(token, "sendMessage", chat_id=route["customer"], text=message["text"],
                entities=message.get("entities", []))
        else:
            api(token, "copyMessage", chat_id=route["customer"], from_chat_id=group,
                message_id=message["message_id"])
    except TelegramError as exc:
        event(bot["id"], f"话题 {thread} 回复发送失败 · {exc}", "错误")
        api(token, "sendMessage", chat_id=group, text="回复发送失败，请在后台查看该机器人的日志。",
            reply_parameters={"message_id": message["message_id"]})
        return
    event(bot["id"], f"话题 {thread} 回复已发送")


def get_bot(bot_id):
    with db() as conn:
        return conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()


def poll(bot_id):
    while True:
        bot = get_bot(bot_id)
        if not bot or not bot["enabled"] or not bot["support_group"]:
            POLL_STATE.pop(bot_id, None)
            return
        try:
            updates = api(bot["token"], "getUpdates", timeout=25, limit=20,
                          offset=bot["offset"], allowed_updates=["message"])
            if POLL_STATE.get(bot_id, ("", ""))[0] != "运行中":
                event(bot_id, "Telegram 轮询已连接")
            POLL_STATE[bot_id] = ("运行中", timestamp())
            for update in updates:
                current = get_bot(bot_id)
                if not current or not current["enabled"] or current["token"] != bot["token"]:
                    break
                try:
                    if "message" in update:
                        relay(current, update["message"])
                except TelegramError as exc:
                    event(bot_id, f"消息处理失败 · {exc}", "错误")
                with db() as conn:
                    conn.execute("UPDATE bots SET offset=? WHERE id=? AND token=?",
                                 (update["update_id"] + 1, bot_id, bot["token"]))
        except TelegramError as exc:
            status = f"重试中 · {exc}"
            if POLL_STATE.get(bot_id, ("", ""))[0] != status:
                event(bot_id, f"轮询失败 · {exc}", "错误")
            POLL_STATE[bot_id] = (status, timestamp())
            time.sleep(5)


def supervise():
    workers = {}
    while True:
        with db() as conn:
            ids = [row["id"] for row in conn.execute(
                "SELECT id FROM bots WHERE enabled=1 AND support_group IS NOT NULL"
            )]
        workers = {key: worker for key, worker in workers.items() if worker.is_alive()}
        for bot_id in ids:
            if bot_id not in workers:
                workers[bot_id] = threading.Thread(target=poll, args=(bot_id,), daemon=True)
                workers[bot_id].start()
        time.sleep(2)


def form(bot=None):
    bot = dict(bot) if bot else {}
    identity = bot.get("id", "")
    username = html.escape(bot.get("username", "添加机器人"))
    group = bot.get("support_group") or ""
    note = html.escape(bot.get("note", ""))
    agents = ", ".join(map(str, json.loads(bot.get("agents", "[]"))))
    welcome = html.escape(bot.get("welcome", WELCOME))
    checked = "checked" if bot.get("enabled", True) else ""
    hint = "留空保留现有 Token" if identity else "从 @BotFather 获取"
    required = "" if identity else "required"
    state, last = POLL_STATE.get(identity, ("连接中", ""))
    state = "待绑定" if not group else (state if checked else "已停用")
    badge = f'<span class="state">{html.escape(state)}</span>' if identity else ""
    detail = f'<p>最近轮询 · {html.escape(last or "—")} <a href="/logs?bot={identity}">查看日志</a></p>' if identity else ""
    return f"""<section id="{'new-bot' if not identity else 'bot-' + str(identity)}">
    <div class="card-title"><h2>{username}</h2>{badge}</div>{detail}<form method="post" action="/">
    <input type="hidden" name="csrf" value="{CSRF}">
    <input type="hidden" name="id" value="{identity}">
    <label>备注<input name="note" value="{note}" maxlength="160" placeholder="例如 港区售后"></label>
    <label>Bot Token<input type="password" name="token" autocomplete="new-password" placeholder="{hint}" {required}></label>
    <label>客服超级群 ID<input name="group" value="{group}" placeholder="-100…" required></label>
    <label>客服用户 ID<input name="agents" value="{agents}" placeholder="多个 ID 用逗号或空格分隔" required></label>
    <label>客户欢迎语<textarea name="welcome" maxlength="4096" required>{welcome}</textarea></label>
    <label class="check"><input type="checkbox" name="enabled" {checked}>启用</label>
    <button>{'保存设置' if identity else '添加机器人'}</button></form></section>"""


def save(fields):
    bot_id = int(fields.get("id") or 0)
    token = fields.get("token", "").strip()
    note = fields.get("note", "").strip()
    if len(note) > 160:
        raise ValueError("备注最多 160 个字符。")
    group = fields.get("group", "").strip()
    if not re.fullmatch(r"-100[0-9]{5,13}", group):
        raise ValueError("请填写以 -100 开头的超级群数字 ID。")
    group = int(group)
    agents = re.split(r"[,，\s]+", fields.get("agents", "").strip())
    if not agents or any(not re.fullmatch(r"[1-9][0-9]{0,15}", agent) for agent in agents):
        raise ValueError("请填写客服的 Telegram 数字 ID。")
    welcome = fields.get("welcome", "").strip()
    if not 1 <= len(welcome) <= 4096:
        raise ValueError("请填写 1 到 4096 字的欢迎语。")
    old = get_bot(bot_id) if bot_id else None
    if bot_id and not old:
        raise ValueError("找不到这个 Bot。")
    if not token and old:
        token = old["token"]
    match = TOKEN_PATTERN.fullmatch(token)
    if not match:
        raise ValueError("Bot Token 格式不正确。")
    identity = int(match.group(1))
    if bot_id and identity != bot_id:
        raise ValueError("此 Token 对应另一个 Bot，请在添加区配置。")
    with db() as conn:
        if not bot_id and conn.execute("SELECT 1 FROM bots WHERE id=?", (identity,)).fetchone():
            raise ValueError("此 Bot 已存在，请编辑原配置。")
    if old and token == old["token"]:
        username = old["username"]
    else:
        info = api(token, "getMe")
        username = "@" + info["username"]
    if "enabled" in fields:
        if api(token, "getWebhookInfo").get("url"):
            raise ValueError("该 Bot 已设置 Webhook，请先在原服务中停用并移除 Webhook。")
        try:
            chat = api(token, "getChat", chat_id=group)
            membership = api(token, "getChatMember", chat_id=group, user_id=identity)
        except TelegramError:
            raise ValueError("无法访问该超级群，请检查群 ID 并先将机器人添加到群中。") from None
        if chat.get("type") != "supergroup" or not chat.get("is_forum"):
            raise ValueError("请在指定超级群开启话题功能。")
        if membership.get("status") != "administrator" or not membership.get("can_manage_topics"):
            raise ValueError("请将机器人设为超级群管理员，并授予管理话题权限。")
    with db() as conn:
        conn.execute("""
            INSERT INTO bots (id, token, username, agents, welcome, enabled, support_group, note)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET token=excluded.token, username=excluded.username,
                agents=excluded.agents, welcome=excluded.welcome, enabled=excluded.enabled,
                support_group=excluded.support_group, note=excluded.note
        """, (identity, token, username, json.dumps(sorted(set(map(int, agents)))),
              welcome, int("enabled" in fields), group, note))
    event(identity, "机器人配置已保存")


class Admin(BaseHTTPRequestHandler):
    def authenticated(self):
        credentials = self.headers.get("Authorization", "").encode()
        if hmac.compare_digest(credentials, self.server.credentials):
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Telegram Support"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def page(self, notice="", status=200, module="bots", bot_filter=None):
        with db() as conn:
            bots = conn.execute("SELECT * FROM bots ORDER BY id").fetchall()
            logs = conn.execute("""
                SELECT e.*, b.username, b.note FROM events e JOIN bots b ON b.id=e.bot
                WHERE (? IS NULL OR e.bot=?) ORDER BY e.id DESC LIMIT 200
            """, (bot_filter, bot_filter)).fetchall() if module == "logs" else []
        if module == "logs":
            filters = '<a href="/logs">全部</a>' + "".join(
                f'<a href="/logs?bot={row["id"]}" {"aria-current=page" if bot_filter == row["id"] else ""}>'
                f'{html.escape(row["note"] or row["username"])}</a>' for row in bots
            )
            rows = "".join(
                f'<tr><td>{html.escape(row["at"])}</td><td>{html.escape(row["note"] or row["username"])}</td>'
                f'<td>{html.escape(row["level"])}</td><td>{html.escape(row["detail"])}</td></tr>'
                for row in logs
            ) or '<tr><td colspan="4">暂无日志</td></tr>'
            content = f"""<h1>运行日志</h1><p>时间使用 UTC。刷新页面可查看最新记录。</p>
            <nav class="filters" aria-label="筛选机器人">{filters}</nav>
            <section class="log-table"><table><thead><tr><th>时间</th><th>机器人</th>
            <th>级别</th><th>事件</th></tr></thead><tbody>{rows}</tbody></table></section>"""
        else:
            cards = "".join(form(row) for row in bots)
            active = sum(row["enabled"] and POLL_STATE.get(row["id"], ("", ""))[0] == "运行中" for row in bots)
            content = f"""<div class="toolbar"><div><h1>机器人管理</h1>
            <p>{len(bots)} 个机器人 · {active} 个已连接</p></div>
            <a class="primary" href="#new-bot">添加机器人</a></div>
            <p>客服在客户专属话题中发送消息即可回复客户。</p>
            <div class="grid">{cards}{form()}</div>"""
        body = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Telegram 客服管理</title><style>
        *{{box-sizing:border-box}} body{{margin:0;background:#f5f7fb;color:#192945;font:15px system-ui,sans-serif}}
        .layout{{display:grid;grid-template-columns:220px minmax(0,1fr);min-height:100vh}}
        aside{{background:#101e38;color:#fff;padding:26px 18px}}
        aside strong{{display:block;padding:0 10px;margin-bottom:22px;font-size:18px}}
        aside a{{display:block;padding:11px 12px;border-radius:8px;color:#dbe8fc;text-decoration:none}}
        aside a:hover,aside [aria-current]{{background:#243e69;color:#fff}}
        .wrap{{width:100%;max-width:1280px;margin:0 auto;padding:32px 26px}}
        .card-title,.toolbar{{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}}
        h1{{font-size:28px;margin:0}} h2{{font-size:18px;margin:0}} p{{line-height:1.6}}
        .toolbar p{{margin:6px 0 0;color:#51617a}} .toolbar{{margin:6px 0 20px}}
        .filters{{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}}
        .filters a{{padding:6px 12px;background:#eaf0ff;border-radius:8px;color:#22498c;text-decoration:none}}
        .filters [aria-current]{{background:#1954c5;color:#fff}}
        .log-table{{overflow:auto}} table{{width:100%;border-collapse:collapse}}
        th,td{{text-align:left;padding:12px;border-bottom:1px solid #dbe4f1}} th{{white-space:nowrap}}
        @media(max-width:700px){{.layout{{display:block}} aside{{padding:16px}} aside strong{{margin-bottom:8px}}
        aside nav{{display:flex;flex-wrap:wrap}} .wrap{{padding:22px 16px}}}}
        .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,340px),1fr));gap:20px;align-items:start}}
        section{{background:#fff;border:1px solid #dbe4f1;border-radius:16px;padding:22px;box-shadow:0 5px 20px #1627450a}}
        #new-bot{{border-top:3px solid #1755ca}} .state{{background:#eaf0ff;color:#22498c;border-radius:24px;padding:5px 10px;white-space:nowrap}}
        label{{display:block;margin:16px 0;color:#34435d;font-weight:600}} .check{{display:flex;align-items:center;gap:8px}}
        input:not([type=checkbox]),textarea{{display:block;width:100%;padding:10px;margin-top:6px;border:1px solid #b5c3d8;border-radius:8px;font:inherit;font-weight:400}}
        textarea{{min-height:76px;resize:vertical}} button,.primary{{background:#1954c5;color:#fff;border:0;border-radius:8px;padding:10px 17px;font:inherit;text-decoration:none;cursor:pointer}}
        :focus-visible{{outline:3px solid #548aff;outline-offset:3px}} [role=status]{{background:#eaf0ff;padding:12px;border-radius:8px}}
        </style><div class="layout"><aside><strong>Telegram 客服</strong><nav aria-label="后台模块">
        <a href="/" {"aria-current=page" if module == "bots" else ""}>机器人管理</a>
        <a href="/logs" {"aria-current=page" if module == "logs" else ""}>运行日志</a>
        </nav></aside><main class="wrap">
        {f'<p role="status">{html.escape(notice)}</p>' if notice else ''}
        {content}</main></div></html>""".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.authenticated():
            url = urlsplit(self.path)
            if url.path == "/":
                self.page("已保存。" if url.query == "saved" else "")
            elif url.path == "/logs":
                bot = parse_qs(url.query).get("bot", [""])[0]
                if bot and not re.fullmatch(r"[1-9][0-9]{0,15}", bot):
                    self.send_error(400)
                    return
                self.page(module="logs", bot_filter=int(bot) if bot else None)
            else:
                self.send_error(404)

    def do_POST(self):
        if not self.authenticated():
            return
        if self.path != "/":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 65536:
                raise ValueError()
            fields = {key: values[0] for key, values in parse_qs(
                self.rfile.read(size).decode(), keep_blank_values=True, max_num_fields=12,
            ).items()}
        except ValueError:
            self.send_error(400)
            return
        if not hmac.compare_digest(fields.get("csrf", "").encode(), CSRF.encode()):
            self.send_error(403)
            return
        try:
            save(fields)
        except ValueError as exc:
            self.page(str(exc), 400)
            return
        except TelegramError:
            self.page("Telegram 验证失败，请检查 Token 和服务器网络。", 502)
            return
        self.send_response(303)
        self.send_header("Location", "/?saved")
        self.end_headers()

    def setup(self):
        super().setup()
        self.connection.settimeout(15)


def main():
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO)
    password = os.environ["ADMIN_PASSWORD"]
    username = os.environ.get("ADMIN_USERNAME", "admin")
    if len(password) < 8:
        raise SystemExit("ADMIN_PASSWORD must contain at least 8 characters")
    init()
    bind_host = os.environ.get("BIND_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((bind_host, int(os.environ.get("PORT", "8765"))), Admin)
    server.credentials = b"Basic " + base64.b64encode(f"{username}:{password}".encode())
    threading.Thread(target=supervise, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
