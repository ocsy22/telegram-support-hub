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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

DATA = os.environ.get("DB_PATH", "support.sqlite3")
TOKEN_PATTERN = re.compile(r"([1-9][0-9]{4,15}):[A-Za-z0-9_-]{20,100}")
CSRF = secrets.token_urlsafe(32)
WELCOME = "您好，请直接发送您的问题，客服会在这里回复您。"
FORMAT = {"text", "photo", "video", "document", "audio", "voice", "animation", "sticker", "video_note", "contact", "location"}


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
            CREATE TABLE IF NOT EXISTS routes (
                bot INTEGER NOT NULL, agent INTEGER NOT NULL,
                message INTEGER NOT NULL, customer INTEGER NOT NULL,
                PRIMARY KEY (bot, agent, message)
            );
        """)


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
        if result["ok"]:
            return result["result"]
    except Exception:
        pass
    raise TelegramError(method)


def relay(bot, message):
    chat = message["chat"]
    if chat["type"] != "private":
        return
    token = bot["token"]
    sender = chat["id"]
    agents = json.loads(bot["agents"])
    body = message.get("text", "")
    if body == "/start":
        text = "请直接回复机器人转来的客户消息。" if sender in agents else bot["welcome"]
        api(token, "sendMessage", chat_id=sender, text=text)
        return
    if not FORMAT.intersection(message):
        api(token, "sendMessage", chat_id=sender, text="暂不支持此消息类型，请发送文字或常见附件。")
        return
    copy = {"from_chat_id": sender, "message_id": message["message_id"]}
    if sender in agents:
        reply = message.get("reply_to_message", {}).get("message_id")
        with db() as conn:
            route = conn.execute(
                "SELECT customer FROM routes WHERE bot=? AND agent=? AND message=?",
                (bot["id"], sender, reply),
            ).fetchone()
        if not route:
            api(token, "sendMessage", chat_id=sender, text="请使用 Telegram 的回复功能，回复客户原消息。")
            return
        try:
            api(token, "copyMessage", chat_id=route["customer"], **copy)
        except TelegramError:
            api(token, "sendMessage", chat_id=sender, text="回复发送失败，请稍后重试。")
        return

    delivered = False
    for agent in agents:
        try:
            result = api(token, "copyMessage", chat_id=agent, **copy)
        except TelegramError:
            logging.warning("Bot %s could not deliver to agent %s", bot["id"], agent)
            continue
        with db() as conn:
            conn.execute("INSERT OR REPLACE INTO routes VALUES (?, ?, ?, ?)",
                         (bot["id"], agent, result["message_id"], sender))
        delivered = True
    if not delivered:
        api(token, "sendMessage", chat_id=sender, text="客服暂时无法接收消息，请稍后重试。")


def get_bot(bot_id):
    with db() as conn:
        return conn.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()


def poll(bot_id):
    while True:
        bot = get_bot(bot_id)
        if not bot or not bot["enabled"]:
            return
        try:
            updates = api(bot["token"], "getUpdates", timeout=25, limit=20,
                          offset=bot["offset"], allowed_updates=["message"])
            for update in updates:
                current = get_bot(bot_id)
                if not current or not current["enabled"] or current["token"] != bot["token"]:
                    break
                try:
                    if "message" in update:
                        relay(current, update["message"])
                except TelegramError as exc:
                    logging.warning("Bot %s message action failed (%s)", bot_id, exc)
                with db() as conn:
                    conn.execute("UPDATE bots SET offset=? WHERE id=? AND token=?",
                                 (update["update_id"] + 1, bot_id, bot["token"]))
        except TelegramError:
            logging.warning("Bot %s polling failed", bot_id)
            time.sleep(5)


def supervise():
    workers = {}
    while True:
        with db() as conn:
            ids = [row["id"] for row in conn.execute("SELECT id FROM bots WHERE enabled=1")]
        workers = {key: worker for key, worker in workers.items() if worker.is_alive()}
        for bot_id in ids:
            if bot_id not in workers:
                workers[bot_id] = threading.Thread(target=poll, args=(bot_id,), daemon=True)
                workers[bot_id].start()
        time.sleep(2)


def form(bot=None):
    bot = dict(bot) if bot else {}
    identity = bot.get("id", "")
    username = html.escape(bot.get("username", "添加 Bot"))
    agents = ", ".join(map(str, json.loads(bot.get("agents", "[]"))))
    welcome = html.escape(bot.get("welcome", WELCOME))
    checked = "checked" if bot.get("enabled", True) else ""
    hint = "留空保留现有 Token" if identity else "从 @BotFather 获取"
    required = "" if identity else "required"
    return f"""<section><h2>{username}</h2><form method="post" action="/">
    <input type="hidden" name="csrf" value="{CSRF}">
    <input type="hidden" name="id" value="{identity}">
    <label>Bot Token<input type="password" name="token" autocomplete="new-password" placeholder="{hint}" {required}></label>
    <label>客服 Telegram 数字 ID（可用逗号或空格分隔）<input name="agents" value="{agents}" required></label>
    <label>客户欢迎语<textarea name="welcome" maxlength="4096" required>{welcome}</textarea></label>
    <label class="check"><input type="checkbox" name="enabled" {checked}>启用</label>
    <button>保存</button></form></section>"""


def save(fields):
    bot_id = int(fields.get("id") or 0)
    token = fields.get("token", "").strip()
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
    if "enabled" in fields and (not old or not old["enabled"] or token != old["token"]):
        if api(token, "getWebhookInfo").get("url"):
            raise ValueError("该 Bot 已设置 Webhook，请先在原服务中停用并移除 Webhook。")
    with db() as conn:
        conn.execute("""
            INSERT INTO bots (id, token, username, agents, welcome, enabled) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET token=excluded.token, username=excluded.username,
                agents=excluded.agents, welcome=excluded.welcome, enabled=excluded.enabled
        """, (identity, token, username, json.dumps(sorted(set(map(int, agents)))),
              welcome, int("enabled" in fields)))


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

    def page(self, notice="", status=200):
        with db() as conn:
            cards = "".join(form(row) for row in conn.execute("SELECT * FROM bots ORDER BY id"))
        body = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Telegram 客服管理</title><style>
        body{{max-width:900px;margin:40px auto;padding:0 20px;background:#f1f5f9;color:#132238;font:16px system-ui}}
        section{{background:white;border:1px solid #dce5ef;border-radius:12px;padding:22px;margin:16px 0}}
        h2{{margin-top:0}} label{{display:block;margin:12px 0}}
        input:not([type=checkbox]),textarea{{display:block;width:100%;box-sizing:border-box;padding:9px;margin-top:6px;border:1px solid #9cabc0;border-radius:6px;font:inherit}}
        button{{background:#164bad;color:white;border:0;border-radius:6px;padding:9px 22px;font:inherit;cursor:pointer}}
        </style><main><h1>Telegram 客服管理</h1>
        <p>客服先在 Telegram 打开对应 Bot 并发送 /start，再用「回复」处理收到的客户消息。</p>
        {f'<p role="status">{html.escape(notice)}</p>' if notice else ''}
        {cards}{form()}</main></html>""".encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.authenticated():
            if self.path in ("/", "/?saved"):
                self.page("已保存。" if self.path == "/?saved" else "")
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
    if len(password) < 16:
        raise SystemExit("ADMIN_PASSWORD must contain at least 16 characters")
    init()
    bind_host = os.environ.get("BIND_HOST", "127.0.0.1")
    server = ThreadingHTTPServer((bind_host, int(os.environ.get("PORT", "8765"))), Admin)
    server.credentials = b"Basic " + base64.b64encode(f"admin:{password}".encode())
    threading.Thread(target=supervise, daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
