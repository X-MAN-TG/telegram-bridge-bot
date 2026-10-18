# Silent Telegram Bridge Userbot

Forwards **all DMs** (text, media, voice notes, documents…) and **group mentions** from your main Telegram account to a second account ID.  
Includes a **reply bridge** so you can answer from the second account and the reply is sent from the main account.

- ✅ Telethon StringSession (no phone login on server)
- ✅ Never updates online / last-seen status
- ✅ Skips every bot **except** `@nubuserbot` (configurable)
- ✅ `/stats` daily counters, `/status`, `/help`
- ✅ Turso (libSQL) for message map + stats
- ✅ Railway-ready (`Procfile` + `worker`)

---

## 1. Get API credentials

1. Go to https://my.telegram.org → API development tools
2. Create an app → copy `API_ID` and `API_HASH`

## 2. Generate StringSession (once, on your PC)

```bash
pip install telethon
python -c "
from telethon.sync import TelegramClient
from telethon.sessions import StringSession
print(StringSession.save(TelegramClient(StringSession(), API_ID, API_HASH).start()))
"
```

(Replace `API_ID` / `API_HASH`. Login with your **main** phone number once. Copy the long string that is printed.)

## 3. Create Turso database

```bash
# install Turso CLI if needed
curl -sSfL https://get.tur.so/install.sh | bash

turso db create telegram-bridge
turso db show telegram-bridge --url
turso db tokens create telegram-bridge
```

Copy the URL and the token.

## 4. Environment variables

Copy `.env.example` → `.env` (or set them in Railway):

| Variable             | Required | Description                                             |
| -------------------- | -------- | ------------------------------------------------------- |
| `API_ID`             | ✅       | from my.telegram.org                                    |
| `API_HASH`           | ✅       | from my.telegram.org                                    |
| `SESSION_STRING`     | ✅       | Telethon StringSession of **main** account              |
| `SECOND_ID`          | ✅       | numeric user ID of the account that receives everything |
| `TURSO_DATABASE_URL` | ✅\*     | `libsql://…turso.io`                                    |
| `TURSO_AUTH_TOKEN`   | ✅\*     | Turso auth token                                        |
| `EXCEPTION_BOT`      | optional | default `nubuserbot` (username without @)               |

\* If Turso vars are missing the bot falls back to a local SQLite file (fine for testing, not for Railway).

## 5. Deploy on Railway

1. Push this folder to a GitHub repo.
2. Railway → New Project → Deploy from GitHub.
3. Add all environment variables under **Variables**.
4. Railway detects the `Procfile` and runs `worker: python main.py`.
5. Check the logs – you should see “Bridge running”.

No public HTTP port is needed; this is a pure worker process.

## 6. Commands (send from **main** account)

| Command      | What it does                         |
| ------------ | ------------------------------------ |
| `/status`    | Shows connection, IDs, DB type       |
| `/stats`     | Today’s counters (msgs, dms, media…) |
| `/stats all` | Last ~100 daily rows                 |
| `/help`      | Short help                           |

## 7. How the reply bridge works

1. A message arrives on main → bot copies it to `SECOND_ID` and stores a mapping.
2. On the second account you **reply** to that forwarded message.
3. The userbot detects the reply, looks up the original chat + message ID, and sends your reply text/media **from the main account** as a real reply.

## 8. What is forwarded

| Source                      | Forwarded?                            |
| --------------------------- | ------------------------------------- |
| Private DMs (any user)      | Yes (text + all media + voice notes)  |
| Group / channel mentions    | Yes (when Telegram marks `mentioned`) |
| Messages from `@nubuserbot` | Always (even in groups)               |
| Other bots                  | Never                                 |
| Your own outgoing msgs      | Never                                 |

## 9. Silent / last-seen

On start and every 60 s the client calls `account.UpdateStatus(offline=True)`.  
Telegram therefore never marks the account “online” because of this bot.

## 10. Local test

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill values
python main.py
```

---

**Security notes**

- Treat `SESSION_STRING` like a password – anyone with it can control the account.
- Never commit `.env` or the session string.
- Use a dedicated second account / Saved Messages for the target if you prefer.
