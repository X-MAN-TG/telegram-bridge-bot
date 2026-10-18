#!/usr/bin/env python3
"""
Silent Telegram Bridge Userbot + Assistant Bot
- Forwards DMs + media + voice notes + group mentions from MAIN account → SECOND_ID
- Includes full thread context (original message that was replied to)
- Skips all bot messages except @nubuserbot (or EXCEPTION_BOT)
- Reply bridge: reply on SECOND_ID to a forwarded msg → sends reply from MAIN account
- Outgoing initiator: send 1st message to anyone via /s <username or user_id>
- /stats  → daily counters (msgs, media, mentions, vns, replies)
- /status → connection check (works from Main ID, Second ID, or via the Bot)
- Never updates online / last-seen status (stealth mode)
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
from telethon.errors import FloodWaitError

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
SECOND_ID_RAW = os.getenv("SECOND_ID", "").strip()
SECOND_SESSION_STRING = (
    os.getenv("SECOND_SESSION_STRING", "")
    or os.getenv("SECOND_ID_STRING", "")
).strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
EXCEPTION_BOT = (os.getenv("EXCEPTION_BOT") or "nubuserbot").lstrip("@").lower()
TURSO_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()

SECOND_ID = 0
if SECOND_ID_RAW.isdigit() or (SECOND_ID_RAW.startswith("-") and SECOND_ID_RAW[1:].isdigit()):
    SECOND_ID = int(SECOND_ID_RAW)
elif SECOND_ID_RAW.startswith("1"):
    SECOND_SESSION_STRING = SECOND_ID_RAW

# Resolve SECOND_ID from string session if missing
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
        print(f"Warning: Failed to resolve SECOND_ID: {e}")

if not all([API_ID, API_HASH, SESSION_STRING, SECOND_ID]):
    print("Missing required env vars: API_ID, API_HASH, SESSION_STRING, SECOND_ID")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bridge")

# Pending sends state: sender_id -> dict
pending_sends = {}

# ---------------------------------------------------------------------------
# Database (Turso / libSQL) with auto-reconnect
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
    _db.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_cache (
            user_id INTEGER PRIMARY KEY,
            access_hash INTEGER NOT NULL
        )
        """
    )
    _db.commit()
    return _db


def db_exec(sql: str, params: tuple = ()):
    global _db
    try:
        db = get_db()
        cur = db.execute(sql, params)
        db.commit()
        return cur
    except Exception as e:
        log.warning("DB query failed (%s). Reconnecting to Turso...", e)
        _db = None
        db = get_db()
        cur = db.execute(sql, params)
        db.commit()
        return cur


def db_fetchone(sql: str, params: tuple = ()):
    global _db
    try:
        return get_db().execute(sql, params).fetchone()
    except Exception as e:
        log.warning("DB fetchone failed (%s). Reconnecting...", e)
        _db = None
        return get_db().execute(sql, params).fetchone()


def db_fetchall(sql: str, params: tuple = ()):
    global _db
    try:
        return get_db().execute(sql, params).fetchall()
    except Exception as e:
        log.warning("DB fetchall failed (%s). Reconnecting...", e)
        _db = None
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
# Telegram Clients
# 1. client: Main user account (Silent listener & bridge sender)
# 2. bot_client: Helper assistant bot (@Panda_forward_Robot)
# ---------------------------------------------------------------------------
client = TelegramClient(
    StringSession(SESSION_STRING),
    API_ID,
    API_HASH,
    receive_updates=True,
    sequential_updates=True,
    connection_retries=10,
    retry_delay=2,
    auto_reconnect=True,
)

bot_client: Optional[TelegramClient] = None
if BOT_TOKEN:
    bot_client = TelegramClient("bot_session", API_ID, API_HASH)


async def enforce_offline():
    """Silently ensure last-seen status stays old/offline."""
    try:
        await client(functions.account.UpdateStatusRequest(offline=True))
    except Exception as e:
        log.debug("enforce_offline: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def check_is_mention(msg: Message, me: types.User) -> bool:
    if msg.mentioned:
        return True
    text = (msg.text or msg.message or "").lower()
    if me.username and f"@{me.username.lower()}" in text:
        return True
    if msg.is_reply:
        try:
            rep = await msg.get_reply_message()
            if rep and rep.sender_id == me.id:
                return True
        except Exception:
            pass
    return False


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


async def format_header(msg: Message, reply_author: Optional[str] = None) -> str:
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
        name += f" [{getattr(sender, 'id', 'unknown')}]"

    chat_title = getattr(chat, "title", None) or "Private DM"
    chat_id = msg.chat_id

    kind = "DM"
    if msg.is_group or msg.is_channel:
        kind = "GROUP"
        if msg.mentioned:
            kind = "MENTION"
    if msg.is_reply:
        kind += " (REPLY)"

    time_str = msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if msg.date else ""
    lines = [
        f"── {kind} ──",
        f"From: {name}",
        f"Chat: {chat_title} ({chat_id})",
    ]
    if reply_author:
        lines.append(f"In Reply To: {reply_author}")
    lines.extend([
        f"MsgID: {msg.id}",
        f"Time: {time_str}",
        f"{'─' * 20}",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core Forward Logic (with Replied-To Context Forwarding)
# ---------------------------------------------------------------------------
async def get_second_entity():
    """Resolve SECOND_ID peer entity with access_hash fallback from Turso/dialogs."""
    try:
        return await client.get_input_entity(SECOND_ID)
    except Exception:
        pass

    # Check Turso entity_cache
    row = db_fetchone("SELECT access_hash FROM entity_cache WHERE user_id = ?", (SECOND_ID,))
    if row:
        return types.InputPeerUser(SECOND_ID, row[0])

    # Check dialogs
    try:
        async for dialog in client.iter_dialogs(limit=50):
            if dialog.entity and getattr(dialog.entity, "id", None) == SECOND_ID:
                access_hash = getattr(dialog.entity, "access_hash", None)
                if access_hash:
                    db_exec(
                        "INSERT OR REPLACE INTO entity_cache (user_id, access_hash) VALUES (?, ?)",
                        (SECOND_ID, access_hash),
                    )
                return dialog.input_entity
    except Exception:
        pass

    return SECOND_ID


async def forward_to_second(msg: Message):
    """
    Forward a message to SECOND_ID.
    If the message is a reply, first forwards the original message (thread context)
    so the user sees exactly what was replied to.
    """
    try:
        target_peer = await get_second_entity()
        me = await client.get_me()

        # Check if this message is a reply to another message
        reply_to_msg = None
        reply_author_desc = None
        if msg.is_reply:
            try:
                reply_to_msg = await msg.get_reply_message()
                if reply_to_msg:
                    if reply_to_msg.out or reply_to_msg.sender_id == me.id:
                        reply_author_desc = f"You ({me.first_name})"
                    else:
                        r_sender = await reply_to_msg.get_sender()
                        r_name = getattr(r_sender, "first_name", "") if r_sender else "User"
                        r_uname = f" (@{r_sender.username})" if (r_sender and getattr(r_sender, "username", None)) else ""
                        reply_author_desc = f"{r_name}{r_uname} [{reply_to_msg.sender_id}]"
            except Exception as rep_err:
                log.warning("Could not fetch replied-to message: %s", rep_err)

        # 1. If it was a reply, forward the original message being replied to (Thread Context)
        if reply_to_msg:
            thread_banner = (
                f"┌── 🔄 REPLIED TO THREAD ──\n"
                f"│ 👤 Original Sender: {reply_author_desc}\n"
                f"│ 💬 The user replied to the message below:\n"
                f"└──────────────────────────"
            )
            await client.send_message(target_peer, thread_banner, silent=True)

            try:
                rep_sent = await client.forward_messages(target_peer, reply_to_msg, silent=True)
                if isinstance(rep_sent, list):
                    rep_sent = rep_sent[0]
                if rep_sent:
                    save_map(rep_sent.id, reply_to_msg.chat_id, reply_to_msg.id)
            except Exception as fwd_rep_err:
                log.warning("Native forward of replied message fallback (%s)", fwd_rep_err)
                is_file = (
                    reply_to_msg.media
                    and not isinstance(
                        reply_to_msg.media,
                        (types.MessageMediaWebPage, types.MessageMediaEmpty, types.MessageMediaUnsupported),
                    )
                )
                if is_file:
                    rep_sent = await client.send_file(
                        target_peer,
                        file=reply_to_msg.media,
                        caption=reply_to_msg.text or None,
                        voice_note=bool(reply_to_msg.voice),
                        video_note=bool(reply_to_msg.video_note),
                        silent=True,
                    )
                else:
                    rep_sent = await client.send_message(
                        target_peer,
                        reply_to_msg.text or "[Media/Sticker]",
                        silent=True,
                        link_preview=True,
                    )
                if isinstance(rep_sent, list):
                    rep_sent = rep_sent[0]
                if rep_sent:
                    save_map(rep_sent.id, reply_to_msg.chat_id, reply_to_msg.id)

        # 2. Build and send the header card for the new reply/message
        header = await format_header(msg, reply_author=reply_author_desc)
        header_msg = await client.send_message(
            target_peer,
            header,
            silent=True,
            link_preview=False,
        )

        # 3. Forward the content of the new message
        sent = None
        try:
            # Native forward preserves voice note waveforms, video note circles, stickers, and albums
            sent = await client.forward_messages(target_peer, msg, silent=True)
            if isinstance(sent, list):
                sent = sent[0]
        except Exception as fwd_err:
            log.warning("Native forward fallback (%s): sending as copy", fwd_err)
            is_file = (
                msg.media
                and not isinstance(
                    msg.media,
                    (types.MessageMediaWebPage, types.MessageMediaEmpty, types.MessageMediaUnsupported),
                )
            )
            if is_file:
                sent = await client.send_file(
                    target_peer,
                    file=msg.media,
                    caption=msg.text or None,
                    voice_note=bool(msg.voice),
                    video_note=bool(msg.video_note),
                    silent=True,
                )
            else:
                sent = await client.send_message(
                    target_peer,
                    msg.text or "[Empty message or unsupported media]",
                    silent=True,
                    link_preview=True,
                )
            if isinstance(sent, list):
                sent = sent[0]

        # 4. Save mapping for reply bridge
        if header_msg:
            save_map(header_msg.id, msg.chat_id, msg.id)
        if sent:
            save_map(sent.id, msg.chat_id, msg.id)

        # 5. Bump stats
        bump_stat("messages")
        if msg.is_group or msg.is_channel:
            bump_stat("group_msgs")
            if msg.mentioned:
                bump_stat("mentions")
        else:
            bump_stat("dms")

        if msg.media:
            kind = media_kind(msg)
            bump_stat(kind)
            bump_stat("media")

        # 6. Keep account offline
        await enforce_offline()

        log.info("Forwarded %s (chat %s) → SECOND_ID", msg.id, msg.chat_id)
        return sent
    except FloodWaitError as e:
        log.warning("FloodWait %ss – sleeping", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return await forward_to_second(msg)
    except Exception as e:
        log.exception("Forward failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Outgoing Initiator Logic (/s <username/id>)
# ---------------------------------------------------------------------------
async def handle_send_command(event, target_str: str):
    """Initiates an outgoing message session to any user."""
    target_str = (target_str or "").strip()
    if not target_str:
        await event.reply(
            "Usage: `/s <username or user_id>`\n\n"
            "Examples:\n"
            "• `/s @CanibalXDevil`\n"
            "• `/s CanibalXDevil`\n"
            "• `/s 6798950954`",
            silent=True,
        )
        return

    clean_target = target_str.lstrip("@").strip()
    target_arg = int(clean_target) if (clean_target.isdigit() or (clean_target.startswith("-") and clean_target[1:].isdigit())) else clean_target

    try:
        target_entity = await client.get_entity(target_arg)
    except Exception as e:
        await event.reply(
            f"❌ Could not resolve target `{target_str}`: {e}\n\n"
            "Tips:\n"
            "• If using a username, double-check spelling.\n"
            "• If using a numeric ID, your main account must have an open chat or shared group with that user so Telegram provides the access hash.",
            silent=True,
        )
        return

    target_id = target_entity.id
    target_name = getattr(target_entity, "first_name", "") or getattr(target_entity, "title", "") or "User"
    target_uname = getattr(target_entity, "username", None)
    uname_display = f"@{target_uname}" if target_uname else f"ID: {target_id}"

    # Cache entity access hash
    access_hash = getattr(target_entity, "access_hash", None)
    if access_hash:
        db_exec(
            "INSERT OR REPLACE INTO entity_cache (user_id, access_hash) VALUES (?, ?)",
            (target_id, access_hash),
        )

    # Store pending send state
    pending_sends[event.sender_id] = {
        "target_id": target_id,
        "target_name": target_name,
        "target_uname": uname_display,
        "target_entity": target_entity,
        "time": datetime.now(timezone.utc),
    }

    me = await client.get_me()
    await event.reply(
        f"🎯 **Target Set:** {target_name} ({uname_display})\n\n"
        f"💬 **You can now send any message, media, voice note, video note, or sticker.**\n"
        f"It will be sent directly to {target_name} from your main account (@{me.username or me.id}).\n\n"
        f"_(Send `/cancel` to abort)_",
        silent=True,
    )


async def handle_pending_send(event) -> bool:
    """Checks if sender has an active /s target, sends the message, and returns True."""
    sender_id = event.sender_id
    if sender_id not in pending_sends:
        return False

    msg: Message = event.message
    text_strip = (msg.text or "").strip()

    # Cancel command
    if text_strip.lower() == "/cancel":
        target_info = pending_sends.pop(sender_id)
        await event.reply(f"❌ Cancelled sending to {target_info['target_name']}.", silent=True)
        return True

    # Switching target via another /s command
    if text_strip.lower().startswith("/s "):
        parts = text_strip.split(maxsplit=1)
        if len(parts) > 1:
            await handle_send_command(event, parts[1])
            return True

    send_info = pending_sends.pop(sender_id)
    target_ent = send_info["target_entity"]
    target_name = send_info["target_name"]
    target_id = send_info["target_id"]

    try:
        sent = None
        is_file = (
            msg.media
            and not isinstance(
                msg.media,
                (types.MessageMediaWebPage, types.MessageMediaEmpty, types.MessageMediaUnsupported),
            )
        )
        if is_file:
            sent = await client.send_file(
                target_ent,
                file=msg.media,
                caption=msg.text or None,
                voice_note=bool(msg.voice),
                video_note=bool(msg.video_note),
                silent=True,
            )
        else:
            sent = await client.send_message(
                target_ent,
                msg.text or "",
                silent=True,
                link_preview=False,
            )

        if isinstance(sent, list):
            sent = sent[0]

        if sent:
            # Save mapping in message_map so replies from target are bridged
            save_map(msg.id, target_id, sent.id)
            save_map(sent.id, target_id, sent.id)
            bump_stat("messages")
            bump_stat("initiated_sends")
            await enforce_offline()

            await event.reply(
                f"✅ **Sent to {target_name} ({send_info['target_uname']})!**\n"
                f"Any reply from them will be automatically bridged back to you here.",
                silent=True,
            )
            log.info("Sent initiated message to %s (%s)", target_name, target_id)
            return True
    except Exception as e:
        log.exception("Failed to send initiated message: %s", e)
        await event.reply(f"⚠️ Failed to send message to {target_name}: {e}", silent=True)
        return True

    return False


# ---------------------------------------------------------------------------
# Status & Stats Formatters
# ---------------------------------------------------------------------------
async def build_status_text() -> str:
    me = await client.get_me()
    connected = client.is_connected()
    bot_name = "none"
    if bot_client and bot_client.is_connected():
        b_me = await bot_client.get_me()
        bot_name = f"@{b_me.username}" if b_me else "active"

    return (
        f"✅ **Telegram Bridge Status**\n\n"
        f"• **Main Account:** {me.first_name} (`{me.id}`)\n"
        f"• **Username:** @{me.username or 'none'}\n"
        f"• **Main Connected:** {connected}\n"
        f"• **Second Target ID:** `{SECOND_ID}`\n"
        f"• **Assistant Bot:** {bot_name}\n"
        f"• **Silent Stealth Mode:** Active (offline status preserved)\n"
        f"• **Exception Bot:** @{EXCEPTION_BOT}\n"
        f"• **Database:** {'Turso libSQL' if TURSO_URL else 'Local SQLite'}"
    )


def build_stats_text(arg: str = "") -> str:
    arg = (arg or "").strip().lower()
    today = date.today().isoformat()

    if arg in ("", "today"):
        rows = db_fetchall(
            "SELECT kind, count FROM daily_stats WHERE day = ? ORDER BY kind",
            (today,),
        )
        if not rows:
            return f"📊 **Stats for {today}**\n\nNo activity recorded yet today."
        lines = [f"📊 **Stats for {today}**\n"]
        for kind, cnt in rows:
            lines.append(f"• **{kind}**: {cnt}")
        return "\n".join(lines)

    if arg == "all":
        rows = db_fetchall(
            "SELECT day, kind, count FROM daily_stats ORDER BY day DESC, kind LIMIT 100"
        )
        if not rows:
            return "📊 No stats recorded yet."
        lines = ["📊 **Recent Stats History**\n"]
        for day, kind, cnt in rows:
            lines.append(f"`{day}` | **{kind}**: {cnt}")
        return "\n".join(lines)

    return "Usage: `/stats` or `/stats all`"


# ---------------------------------------------------------------------------
# Userbot Event Handlers
# ---------------------------------------------------------------------------
@client.on(events.NewMessage(incoming=True))
async def on_new_message(event: events.NewMessage.Event):
    msg: Message = event.message
    me = await client.get_me()

    # Ignore our own messages
    if msg.out or msg.sender_id == me.id:
        return

    # Check if message is from the second account
    if msg.sender_id == SECOND_ID:
        sender = await msg.get_sender()
        if sender and getattr(sender, "access_hash", None):
            db_exec(
                "INSERT OR REPLACE INTO entity_cache (user_id, access_hash) VALUES (?, ?)",
                (sender.id, sender.access_hash),
            )

        # 1. If sender has an active /s session, send the message to the target!
        if event.sender_id in pending_sends:
            handled = await handle_pending_send(event)
            if handled:
                return

        text_strip = (msg.text or "").strip()
        text_lower = text_strip.lower()

        # Check /s command
        if text_lower.startswith("/s ") or text_lower == "/s":
            parts = text_strip.split(maxsplit=1)
            target_arg = parts[1] if len(parts) > 1 else ""
            await handle_send_command(event, target_arg)
            return

        # Check /status, /stats, /help, /start
        if text_lower.startswith("/status"):
            status_text = await build_status_text()
            await event.reply(status_text, silent=True)
            return

        if text_lower.startswith("/stats"):
            parts = text_lower.split(maxsplit=1)
            arg = parts[1] if len(parts) > 1 else ""
            await event.reply(build_stats_text(arg), silent=True)
            return

        if text_lower.startswith("/help") or text_lower.startswith("/start"):
            await event.reply(
                "ℹ️ **Bridge Help & Commands**\n\n"
                "• All DMs and group mentions to your main account are forwarded here.\n"
                "• **Reply** directly to any forwarded message to reply from your main account.\n"
                "• `/s <username/id>` - Send 1st message/media/sticker to anyone from your main account.\n"
                "• `/status` - Check bridge connection\n"
                "• `/stats` - View today's stats\n"
                "• `/stats all` - View recent history\n",
                silent=True,
            )
            return

        # If it's a reply to one of our forwarded messages, let the reply handler process it
        if msg.is_reply:
            return

        # General message from SECOND_ID
        await event.reply(
            "ℹ️ **Bridge Active**\n"
            f"This chat is connected to your main account @{me.username or me.id}.\n"
            "• Messages from other users will be forwarded here.\n"
            "• Reply to any forwarded message to send a reply back from your main account.\n"
            "• Use `/s <username/id>` to initiate a message to anyone.\n"
            "• Send `/status` or `/stats` anytime.",
            silent=True,
        )
        return

    # Identify sender
    sender = await msg.get_sender()
    is_bot = getattr(sender, "bot", False) if sender else False
    uname = (getattr(sender, "username", "") or "").lower()
    is_exception = uname == EXCEPTION_BOT

    # Skip bots except the allowed exception
    if is_bot and not is_exception:
        return

    # Private chats (DMs) – always forward
    if event.is_private:
        await forward_to_second(msg)
        return

    # Groups / channels – only if we are mentioned OR it is the exception bot
    if event.is_group or event.is_channel:
        is_mention = await check_is_mention(msg, me)
        if is_exception or is_mention:
            await forward_to_second(msg)
        return


@client.on(events.NewMessage(chats=SECOND_ID, incoming=True))
async def on_reply_from_second(event: events.NewMessage.Event):
    """
    When SECOND_ID replies to a forwarded message, look up the original
    chat and message ID and send the reply from the MAIN user account.
    """
    msg: Message = event.message
    if not msg.is_reply:
        return

    if msg.sender_id != SECOND_ID:
        return

    sender = await msg.get_sender()
    if sender and getattr(sender, "access_hash", None):
        db_exec(
            "INSERT OR REPLACE INTO entity_cache (user_id, access_hash) VALUES (?, ?)",
            (sender.id, sender.access_hash),
        )

    # If sender has a pending /s session, handle it first
    if event.sender_id in pending_sends:
        handled = await handle_pending_send(event)
        if handled:
            return

    reply_to = await msg.get_reply_message()
    if not reply_to:
        return

    mapping = get_map(reply_to.id)
    if not mapping:
        return  # not one of our forwards

    orig_chat_id, orig_msg_id = mapping
    try:
        is_file = (
            msg.media
            and not isinstance(
                msg.media,
                (types.MessageMediaWebPage, types.MessageMediaEmpty, types.MessageMediaUnsupported),
            )
        )
        if is_file:
            await client.send_file(
                orig_chat_id,
                file=msg.media,
                caption=msg.text or None,
                reply_to=orig_msg_id,
                voice_note=bool(msg.voice),
                video_note=bool(msg.video_note),
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
        await enforce_offline()
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


# Commands sent by MAIN account itself (anywhere or in Saved Messages)
@client.on(events.NewMessage(outgoing=True, pattern=r"^/s(?:\s+(.+))?"))
async def cmd_s_main(event: events.NewMessage.Event):
    target_arg = (event.pattern_match.group(1) or "").strip()
    await handle_send_command(event, target_arg)


@client.on(events.NewMessage(outgoing=True, pattern=r"^/status(?:\s.*)?$"))
async def cmd_status_main(event: events.NewMessage.Event):
    text = await build_status_text()
    await event.reply(text, silent=True)


@client.on(events.NewMessage(outgoing=True, pattern=r"^/stats(?:\s+(.+))?$"))
async def cmd_stats_main(event: events.NewMessage.Event):
    arg = (event.pattern_match.group(1) or "").strip()
    await event.reply(build_stats_text(arg), silent=True)


@client.on(events.NewMessage(outgoing=True, pattern=r"^/help(?:\s.*)?$"))
async def cmd_help_main(event: events.NewMessage.Event):
    text = (
        "Bridge Userbot commands (send from main account):\n"
        "/s <user> – send 1st message to anyone\n"
        "/status – connection & config\n"
        "/stats – today's counters\n"
        "/stats all – recent history\n"
        "/help – this message\n\n"
        "Auto features:\n"
        "• All DMs + media + voice notes → SECOND_ID\n"
        "• Full thread context forwarded on replies\n"
        "• Group mentions of you → SECOND_ID\n"
        "• Messages from @nubuserbot always forwarded\n"
        "• Reply to a forwarded msg on SECOND_ID → reply is sent from MAIN"
    )
    await event.reply(text, silent=True)


# ---------------------------------------------------------------------------
# Helper Bot (@Panda_forward_Robot) Event Handlers
# Allows interacting with the bridge via the bot from any account
# ---------------------------------------------------------------------------
if bot_client:
    @bot_client.on(events.NewMessage(pattern=r"^/(start|help)"))
    async def bot_start(event: events.NewMessage.Event):
        await event.reply(
            "🤖 **Telegram Bridge Assistant Bot**\n\n"
            "This bot monitors your Telegram bridge.\n\n"
            "Commands:\n"
            "• `/s <username/id>` - Send 1st message to anyone from your main account\n"
            "• `/status` - Check live connection & status\n"
            "• `/stats` - View today's stats\n"
            "• `/stats all` - View recent history\n"
        )

    @bot_client.on(events.NewMessage(pattern=r"^/s(?:\s+(.+))?"))
    async def bot_cmd_s(event: events.NewMessage.Event):
        target_arg = (event.pattern_match.group(1) or "").strip()
        await handle_send_command(event, target_arg)

    @bot_client.on(events.NewMessage(pattern=r"^/status"))
    async def bot_status(event: events.NewMessage.Event):
        text = await build_status_text()
        await event.reply(text)

    @bot_client.on(events.NewMessage(pattern=r"^/stats(?:\s+(.+))?"))
    async def bot_stats(event: events.NewMessage.Event):
        arg = (event.pattern_match.group(1) or "").strip()
        await event.reply(build_stats_text(arg))

    @bot_client.on(events.NewMessage())
    async def bot_general_message(event: events.NewMessage.Event):
        if event.sender_id in pending_sends:
            text_s = (event.message.text or "").strip().lower()
            if not text_s.startswith(("/start", "/status", "/stats", "/help")):
                await handle_pending_send(event)


# ---------------------------------------------------------------------------
# HTTP Health Check Server (Railway / PaaS)
# ---------------------------------------------------------------------------
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

    log.info("Connecting user client...")
    await client.connect()
    if not await client.is_user_authorized():
        log.error("Session string is invalid or expired. Generate a new one.")
        sys.exit(1)

    me = await client.get_me()
    log.info("Logged in as %s (%s) – silent mode", me.first_name, me.id)

    # Force offline immediately
    await enforce_offline()

    # Start helper bot client if configured
    if bot_client:
        try:
            await bot_client.start(bot_token=BOT_TOKEN)
            b_me = await bot_client.get_me()
            log.info("Helper bot started as @%s", b_me.username)
        except Exception as e:
            log.warning("Could not start helper bot: %s", e)

    # If second string session is provided, initialize communication so peer entity is cached
    if SECOND_SESSION_STRING:
        try:
            async with TelegramClient(StringSession(SECOND_SESSION_STRING), API_ID, API_HASH) as sec_client:
                main_username = me.username or "pyrexus"
                log.info("Handshake: Resolving main account @%s from second account...", main_username)
                main_peer = await sec_client.get_entity(main_username)
                await sec_client.send_message(
                    main_peer,
                    "🟢 **Telegram Bridge Initialized**\nYour bridge is now live and running on Railway.",
                    silent=True,
                )
                log.info("Sent startup handshake from SECOND to MAIN (@%s)", main_username)
                sec_me = await sec_client.get_me()
                if sec_me and getattr(sec_me, "access_hash", None):
                    db_exec(
                        "INSERT OR REPLACE INTO entity_cache (user_id, access_hash) VALUES (?, ?)",
                        (sec_me.id, sec_me.access_hash),
                    )
                # Wait 2 seconds for main client to process incoming message and cache entity
                await asyncio.sleep(2)
        except Exception as e:
            log.warning("Startup handshake note: %s", e)

    # Resolve target peer entity
    target_peer = await get_second_entity()

    # Send confirmation to second ID from main account
    try:
        await client.send_message(
            target_peer,
            f"🟢 **Telegram Bridge Bot is Live & Ready!**\n\n"
            f"• **Main Account:** `{me.first_name}` (`{me.id}`)\n"
            f"• **Target ID:** `{SECOND_ID}`\n"
            f"• **Status:** Active & Forwarding (with Replied-Thread Context)\n"
            f"• **Commands:** `/s <user>` to start new chat, `/status`, `/stats`\n"
            f"• **Silent Mode:** Enabled (offline last-seen preserved)\n",
            silent=True,
        )
        await enforce_offline()
        log.info("Sent startup confirmation to SECOND_ID successfully")
    except Exception as e:
        log.warning("Could not send startup greeting to SECOND_ID: %s", e)

    # Start health server for Railway
    await start_health_server()

    log.info("Bridge fully running. Forwarding → %s", SECOND_ID)

    tasks = [client.run_until_disconnected()]
    if bot_client:
        tasks.append(bot_client.run_until_disconnected())
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Stopped by user")
