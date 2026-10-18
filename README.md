# 🌉 Silent Telegram Bridge Userbot & Assistant Bot

A stealthy, multi-account **Telegram Bridge** built with **Telethon** and **libSQL (Turso)**.

It seamlessly mirrors and forwards private DMs, group mentions, voice notes, video notes, and all media from your **main Telegram account** to your **second account**, complete with a two-way **Reply Bridge**, **Replied-To Thread Context**, **Outgoing Message Initiator (`/s`)**, and zero status leaks.

---

## ✨ Features

- 🤫 **Stealth Silent Mode**: Never updates your main account's online or last-seen status (`offline=True`), keeping your last seen frozen weeks ago.
- 📨 **Full DM & Media Forwarding**: Forwards text, photos, video notes (circles), voice notes (waveform preserved), stickers, animations, documents, and contacts.
- 🧵 **Replied-To Thread Context**: When someone replies to a message, the bridge forwards both the original message being replied to (text, sticker, VN, media) AND the new reply, giving you complete conversation context!
- 📤 **Send 1st Message (`/s <username/id>`)**: Initiate conversations with any user directly from your second account or assistant bot. Type `/s @username`, send your message/media/sticker/VN, and it is sent from your main account.
- 🔄 **Two-Way Reply Bridge**: Reply directly to any forwarded message on your second account; your reply is sent back to the original sender from your main account.
- 📢 **Group & Channel Mentions**: Catches any group message mentioning your username or replying to your messages.
- 🤖 **Smart Bot Filtering**: Automatically ignores automated bots while forwarding allowed bots (e.g. `@nubuserbot`).
- ⚡ **Persistent Message Map & Daily Stats**: Backed by Turso libSQL with automated database schema migration and auto-reconnection.
- 📊 **Commands (`/status`, `/stats`, `/s`, `/help`)**:
  - Send from your **main account** (in Saved Messages or any chat).
  - Send from your **second account** directly to your main account.
  - Or message your **Assistant Bot** token directly.
- 🐳 **Railway Ready**: Includes `Dockerfile`, `railway.json`, and an integrated aiohttp health check server.

---

## 🛠️ Architecture

```
[ Incoming DM / Mention / Reply ]
                │
                ▼
     [ Main Userbot ] (pyrexus - Silent & Offline)
                │
        (If message is reply)
                ├─► Forwards Original Replied-To Message
                ├─► Forwards New Reply + Context Card
                │
                ├─► [ Turso libSQL ] (Message Map + Stats)
                │
                ▼
     [ Second Account ] (Target ID)
                │
    ┌───────────┴───────────┐
    ▼                       ▼
(User replies)       (/s <username/id>)
    │                       │
    ▼                       ▼
Sends reply from       Sends 1st message/media/vn
Main to Original Chat  from Main Account to Target
```

---

## ⚙️ Environment Variables

Copy `.env.example` to `.env` or configure them in Railway:

| Variable | Required | Description |
| :--- | :---: | :--- |
| `API_ID` | ✅ | Telegram API ID from https://my.telegram.org |
| `API_HASH` | ✅ | Telegram API Hash from https://my.telegram.org |
| `SESSION_STRING` | ✅ | Telethon StringSession of your **main** account |
| `SECOND_ID` | ✅ | Numeric user ID of the account that receives forwarded content |
| `SECOND_SESSION_STRING` | Optional | StringSession of your second account for auto-handshake and entity resolution |
| `BOT_TOKEN` | Optional | Telegram Bot Token from `@BotFather` for the assistant bot |
| `TURSO_DATABASE_URL` | Optional | `libsql://<db-name>.turso.io` (defaults to local SQLite if unset) |
| `TURSO_AUTH_TOKEN` | Optional | Turso database authentication token |
| `EXCEPTION_BOT` | Optional | Username of allowed bot (default: `nubuserbot`) |

---

## 💬 Commands

| Command | Description |
| :--- | :--- |
| `/s <username/id>` | Set target to initiate sending a message, media, vn, or sticker to anyone from your main account |
| `/cancel` | Cancel an active `/s` sending session |
| `/status` | Shows live connection status, account IDs, DB state, and silent mode |
| `/stats` | Shows today's counters (messages, DMs, group mentions, media, replies, initiated sends) |
| `/stats all` | Shows historical statistics breakdown |
| `/help` | Shows command help and features |

---

## 🔒 Security

- Treat `SESSION_STRING` like a password. Anyone with access can control the account.
- Never commit `.env` or session strings to public repositories.
- The repository `.gitignore` automatically excludes `.env`, `*.session`, and database files.
