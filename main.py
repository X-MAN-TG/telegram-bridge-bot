#!/usr/bin/env python3
"""
Silent Telegram Bridge Userbot
- Forwards DMs + media + voice notes + group mentions from MAIN account → SECOND_ID
- Skips all bot messages except @nubuserbot (or EXCEPTION_BOT)
- Reply bridge: reply on SECOND_ID to a forwarded msg → sends reply from MAIN account
- /stats  → daily counters (msgs, media, mentions, vns)
- /status → connection check
- Never updates online / last-seen status
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone, date
from typing import Optional

from dotenv import load_dotenv
from telethon import TelegramClient, events, functions, types
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message
from telethon.errors import FloodWaitError, RPCError

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
SESSION_STRING = os.getenv("SESSION_STRING", "")
SECOND_ID_RAW = os.getenv("SECOND_ID", "").strip()
SECOND_SESSION_STRING = (
    os.getenv("SECOND_SESSION_STRING", "")
    or os.getenv("SECOND_ID_STRING", "")
).strip()

SECOND_ID = 0
if SECOND_ID_RAW.isdigit() or (SECOND_ID_RAW.startswith("-") and SECOND_ID_RAW[1:].isdigit()):
    SECOND_ID = int(SECOND_ID_RAW)
elif SECOND_ID_RAW.startswith("1"):
    SECOND_SESSION_STRING = SECOND_ID_RAW

# If SECOND_ID is missing but we have a session string for the second account, resolve it now
if SECOND_ID == 0 and SECOND_SESSION_STRING and API_ID and API_HASH:
    try:
        async def _resolve_sec_id():
            async with TelegramClient(StringSession(SECOND_SESSION_STRING), API_ID, API_HASH) as sec_c:
                sec_me = await sec_c.get_me()
                return sec_me.id
        _tmp_loop = asyncio.new_event_loop()
        SECOND_ID = _tmp_loop.run_until_complete(_resolve_sec_id())
        _tmp_loop.close()
        print(f"Resolved SECOND_ID = {SECOND_ID}")
    except Exception as e:
        print(f"Warning: Failed to resolve SECOND_ID from second string session: {e}")

EXCEPTION_BOT = (os.getenv("EXCEPTION_BOT") or "nubuserbot").lstrip("@").lower()
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "")
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "")

if not all([API_ID, API_HASH, SESSION_STRING, SECOND_ID]):
    print("Missing required env vars: API_ID, API_HASH, SESSION_STRING, SECOND_ID (or SECOND_SESSION_STRING)")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bridge")

# ---------------------------------------------------------------------------
# Database (Turso / libSQL)
# ---------------------------------------------------------------------------
_db = None

def get_db():
    global _db
    if _db is not None:
        return _db
    import libsql
    if TURSO_URL and TURSO_TOKEN:
        _db = libsql.connect(database=TURSO_URL, auth_token=TURSO_TOKEN)
        log.info("Connected to Turso remote DB")
    else:
        _db = libsql.connect("bridge_local.db")
        log.warning("No Turso credentials – using local SQLite file")
    _db.execute(
        """
        CREATE TABLE IF NOT EXISTS message_map (
            fwd_msg_id INTEGER PRIMARY KEY,
            original_chat_id INTEGER NOT NULL,
            original_msg_id INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    _db.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_stats (
            day TEXT NOT NULL,
            kind TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (day, kind)
        )
        """
    )
    _db.commit()
    return _db


def db_exec(sql: str, params: tuple = ()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur


def db_fetchone(sql: str, params: tuple = ()):
    return get_db().execute(sql, params).fetchone()


def db_fetchall(sql: str, params: tuple = ()):
    return get_db().execute(sql, params).fetchall()


def bump_stat(kind: str, amount: int = 1):
    today = date.today().isoformat()
    db_exec(
        """
        INSERT INTO daily_stats (day, kind, count) VALUES (?, ?, ?)
        ON CONFLICT(day, kind) DO UPDATE SET count = count + ?
        """,
        (today, kind, amount, amount),
    )


def save_map(fwd_msg_id: int, orig_chat_id: int, orig_msg_id: int):
    db_exec(
        "INSERT OR REPLACE INTO message_map (fwd_msg_id, original_chat_id, original_msg_id, created_at) VALUES (?, ?, ?, ?)",
        (fwd_msg_id, orig_chat_id, orig_msg_id, datetime.now(timezone.utc).isoformat()),
    )


def get_map(fwd_msg_id: int) -> Optional[tuple]:
    row = db_fetchone(
        "SELECT original_chat_id, original_msg_id FROM message_map WHERE fwd_msg_id = ?",
        (fwd_msg_id,),
    )
    return (row[0], row[1]) if row else None


# ---------------------------------------------------------------------------
# Client – silent, no online status
# ---------------------------------------------------------------------------
client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
    receive_updates=True,
    sequential_updates=True,
    # Keep connection alive but do not announce presence
    connection_retries=5,
    retry_delay=2,
    auto_reconnect=True,
)


async def stay_offline():
    """Periodically force offline status so last-seen stays old."""
    while True:
        try:
            await client(functions.account.UpdateStatusRequest(offline=True))
        except Exception as e:
            log.debug("stay_offline: %s", e)
        await asyncio.sleep(60)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def is_bot_sender(msg: Message) -> bool:
    if not msg.sender:
        return False
    if getattr(msg.sender, "bot", False):
        return True
    return False


def is_exception_bot(msg: Message) -> bool:
    if not msg.sender:
        return False
    uname = (getattr(msg.sender, "username", None) or "").lower()
    return uname == EXCEPTION_BOT


def is_mention_of_me(msg: Message, me_id: int) -> bool:
    if not msg.mentioned:
        return False
    # Any message that Telegram flags as mentioning us
    return True


def media_kind(msg: Message) -> str:
    if msg.voice or msg.audio:
        return "voice"
    if msg.photo:
        return "photo"
    if msg.video or msg.video_note:
        return "video"
    if msg.document:
        return "document"
    if msg.sticker:
        return "sticker"
    if msg.gif:
        return "gif"
    return "other_media"


async def format_header(msg: Message) -> str:
    """Build a short human-readable header for the forwarded copy."""
    sender = await msg.get_sender()
    chat = await msg.get_chat()

    name = "Unknown"
    if sender:
        name = getattr(sender, "first_name", "") or ""
        if getattr(sender, "last_name", None):
            name += f" {sender.last_name}"
        uname = getattr(sender, "username", None)
        if uname:
            name += f" (@{uname})"
        name += f" [{sender.id}]"

    chat_title = getattr(chat, "title", None) or "Private"
    chat_id = msg.chat_id

    kind = "DM"
    if msg.is_group or msg.is_channel:
        kind = "GROUP"
        if is_mention_of_me(msg, (await client.get_me()).id):
            kind = "MENTION"

    return (
        f"── {kind} ──\n"
        f"From: {name}\n"
        f"Chat: {chat_title} ({chat_id})\n"
        f"MsgID: {msg.id}\n"
        f"Time: {msg.date.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"{'─' * 20}"
    )


# ---------------------------------------------------------------------------
# Core forward logic
# ---------------------------------------------------------------------------
async def forward_to_second(msg: Message, header: str):
    """Send a faithful copy of the message to SECOND_ID with header."""
    try:
        # Text / caption
        text = msg.text or msg.message or ""
        full_text = f"{header}\n\n{text}" if text else header

        # Media
        if msg.media:
            sent = await client.send_file(
                SECOND_ID,
                file=msg.media,
                caption=full_text[:1024],  # Telegram caption limit
                voice_note=bool(msg.voice),
                video_note=bool(msg.video_note),
                silent=True,
            )
            kind = media_kind(msg)
            bump_stat(kind)
            bump_stat("media")
        else:
            sent = await client.send_message(
                SECOND_ID,
                full_text,
                silent=True,
                link_preview=False,
            )

        # Map for reply bridge
        if isinstance(sent, list):
            sent = sent[0]
        save_map(sent.id, msg.chat_id, msg.id)

        bump_stat("messages")
        if msg.is_group or msg.is_channel:
            bump_stat("group_msgs")
            if is_mention_of_me(msg, (await client.get_me()).id):
                bump_stat("mentions")
        else:
            bump_stat("dms")

        log.info("Forwarded %s → SECOND (map %s)", msg.id, sent.id)
        return sent
    except FloodWaitError as e:
        log.warning("FloodWait %ss – sleeping", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return await forward_to_second(msg, header)
    except Exception as e:
        log.exception("Forward failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------
@client.on(events.NewMessage(incoming=True))
async def on_new_message(event: events.NewMessage.Event):
    msg: Message = event.message
    me = await client.get_me()

    # Ignore our own messages
    if msg.out or msg.sender_id == me.id:
        return

    # Never re-forward messages that come from the second account
    # (prevents loops with the reply bridge)
    if msg.sender_id == SECOND_ID:
        return

    # Skip bots except the allowed exception
    if is_bot_sender(msg) and not is_exception_bot(msg):
        return

    # Private chats (DMs) – always forward
    if event.is_private:
        header = await format_header(msg)
        await forward_to_second(msg, header)
        return

    # Groups / channels – only if we are mentioned OR it is the exception bot
    if event.is_group or event.is_channel:
        if is_exception_bot(msg) or is_mention_of_me(msg, me.id):
            header = await format_header(msg)
            await forward_to_second(msg, header)
        return


@client.on(events.NewMessage(chats=SECOND_ID, incoming=True))
async def on_reply_from_second(event: events.NewMessage.Event):
    """
    Main account sees messages from SECOND_ID as *incoming* in the private
    chat with SECOND_ID. When SECOND_ID replies to one of our forwarded
    messages, look up the original chat+msg and send the reply from MAIN.
    """
    msg: Message = event.message
    if not msg.is_reply:
        return

    # Only care about messages that come from the second account
    if msg.sender_id != SECOND_ID:
        return

    reply_to = await msg.get_reply_message()
    if not reply_to:
        return

    mapping = get_map(reply_to.id)
    if not mapping:
        return  # not one of our forwards

    orig_chat_id, orig_msg_id = mapping
    try:
        if msg.media:
            await client.send_file(
                orig_chat_id,
                file=msg.media,
                caption=msg.text or None,
                reply_to=orig_msg_id,
                silent=True,
            )
        else:
            await client.send_message(
                orig_chat_id,
                msg.text or "",
                reply_to=orig_msg_id,
                silent=True,
                link_preview=False,
            )
        log.info("Bridge reply → chat %s msg %s", orig_chat_id, orig_msg_id)
        bump_stat("replies_bridged")
    except Exception as e:
        log.exception("Reply bridge failed: %s", e)
        try:
            await client.send_message(
                SECOND_ID,
                f"⚠️ Reply bridge failed: {e}",
                silent=True,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Commands (only accepted from MAIN account itself)
# ---------------------------------------------------------------------------
@client.on(events.NewMessage(outgoing=True, pattern=r"^/status$"))
async def cmd_status(event: events.NewMessage.Event):
    me = await client.get_me()
    connected = client.is_connected()
    text = (
        f"✅ Status\n"
        f"Connected: {connected}\n"
        f"Main ID: {me.id}\n"
        f"Username: @{me.username or 'none'}\n"
        f"Second ID: {SECOND_ID}\n"
        f"Exception bot: @{EXCEPTION_BOT}\n"
        f"DB: {'Turso' if TURSO_URL else 'local'}"
    )
    await event.reply(text, silent=True)


@client.on(events.NewMessage(outgoing=True, pattern=r"^/stats(?:\s+(.+))?$"))
async def cmd_stats(event: events.NewMessage.Event):
    arg = (event.pattern_match.group(1) or "").strip().lower()
    today = date.today().isoformat()

    if arg in ("", "today"):
        rows = db_fetchall(
            "SELECT kind, count FROM daily_stats WHERE day = ? ORDER BY kind",
            (today,),
        )
        if not rows:
            await event.reply(f"📊 No stats for {today} yet.", silent=True)
            return
        lines = [f"📊 Stats for {today}"]
        for kind, cnt in rows:
            lines.append(f"• {kind}: {cnt}")
        await event.reply("\n".join(lines), silent=True)
        return

    if arg == "all":
        rows = db_fetchall(
            "SELECT day, kind, count FROM daily_stats ORDER BY day DESC, kind LIMIT 100"
        )
        if not rows:
            await event.reply("📊 No stats recorded yet.", silent=True)
            return
        lines = ["📊 Recent stats (last 100 rows)"]
        for day, kind, cnt in rows:
            lines.append(f"{day} | {kind}: {cnt}")
        await event.reply("\n".join(lines), silent=True)
        return

    await event.reply("Usage: /stats  or  /stats all", silent=True)


@client.on(events.NewMessage(outgoing=True, pattern=r"^/help$"))
async def cmd_help(event: events.NewMessage.Event):
    text = (
        "Bridge Userbot commands (send from main account):\n"
        "/status – connection & config\n"
        "/stats – today's counters\n"
        "/stats all – recent history\n"
        "/help – this message\n\n"
        "Auto features:\n"
        "• All DMs + media + voice notes → SECOND_ID\n"
        "• Group mentions of you → SECOND_ID\n"
        "• Messages from @nubuserbot always forwarded\n"
        "• Reply to a forwarded msg on SECOND_ID → reply is sent from MAIN"
    )
    await event.reply(text, silent=True)


# Optional HTTP server for Railway / PaaS health checks
async def start_health_server():
    port_str = os.getenv("PORT")
    if not port_str:
        return
    try:
        from aiohttp import web
        port = int(port_str)
        app = web.Application()
        async def handle_ping(request):
            return web.Response(text="OK - Telegram Bridge Userbot is running\n")
        app.router.add_get("/", handle_ping)
        app.router.add_get("/health", handle_ping)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", port)
        await site.start()
        log.info("Health check server listening on 0.0.0.0:%d", port)
    except Exception as e:
        log.warning("Could not start health check server: %s", e)


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
async def main():
    get_db()  # init tables

    await client.connect()
    if not await client.is_user_authorized():
        log.error("Session string is invalid or expired. Generate a new one.")
        sys.exit(1)

    me = await client.get_me()
    log.info("Logged in as %s (%s) – silent mode", me.first_name, me.id)

    # Force offline immediately
    try:
        await client(functions.account.UpdateStatusRequest(offline=True))
    except Exception:
        pass

    # Background task to keep last-seen frozen
    asyncio.create_task(stay_offline())

    # Start optional health server if Railway assigned a PORT
    await start_health_server()

    log.info("Bridge running. Forwarding → %s", SECOND_ID)
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user")
