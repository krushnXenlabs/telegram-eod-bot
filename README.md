# 🤖 Telegram EOD Bot

A personal Telegram bot that helps you log your day, reflect at night, and wake up with intention — powered by Claude AI.

## What it does

Every night at **11:30 PM IST**, the bot automatically pings you on Telegram to run your End of Day (EOD) review. It asks about your wins, blockers, and mood, then generates a structured daily note saved to your Obsidian vault (via GitHub) and to Supabase.

In the morning, `/gm` runs a personalized good-morning ritual — today's date, a motivational message in your voice, and a preview of what's ahead.

### Commands

| Command | What it does |
|---|---|
| `/eod` | Trigger an EOD session manually |
| `/gm` | Good morning — start your day with intention |
| `/gn` | Good night — wrap the day |
| `/geteod` | Retrieve your EOD for today (or a past date) |
| `/skills` | Update your skills/focus areas |
| `/interview` | Mock interview prep with Claude |
| `/status` | Check bot health and scheduler status |
| `/help` | List all commands |

The bot also auto-triggers EOD at **11:30 PM IST** every day via APScheduler.

---

## How it works

```
You ──► Telegram ──► Bot (Python, GCS VM)
                        │
                        ├──► Claude API  (EOD generation, reflection, GM message)
                        ├──► Supabase    (activity log + conversation history)
                        └──► GitHub      (pushes Obsidian daily note as .md file)
```

1. You answer a few quick questions in Telegram
2. Claude synthesizes your answers into a structured EOD report matching your personal voice (defined in `personality.md`)
3. The report is saved as a markdown daily note to your Obsidian vault repo on GitHub
4. Activity data is logged to Supabase for `/geteod` retrieval

---

## Tech stack

| Layer | Tech |
|---|---|
| Bot framework | [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) v21.6 |
| AI | [Anthropic Claude](https://anthropic.com) — Sonnet for EOD/interviews, Haiku for skills/personality |
| Scheduler | APScheduler 3.10 with `AsyncIOScheduler` + `CronTrigger` |
| Database | [Supabase](https://supabase.com) (Postgres) |
| Notes | Obsidian vault synced via GitHub (markdown daily notes) |
| Hosting | Google Cloud VM (e2-micro, `us-central1-a`) as a systemd service |
| Timezone | `Asia/Kolkata` (IST) — VM runs UTC, all scheduling is IST-aware |

---

## Setup

### 1. Prerequisites

- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- An [Anthropic API key](https://console.anthropic.com)
- A [Supabase](https://supabase.com) project (optional but recommended)
- A GitHub repo for your Obsidian vault (optional)
- A GCS VM or any Linux server with Python 3.11+

### 2. Clone and configure

```bash
git clone https://github.com/krushnXenlabs/telegram-eod-bot
cd telegram-eod-bot
cp .env.example .env
# Edit .env and fill in your credentials
```

### 3. Set up Supabase schema

Run `schema.sql` in your Supabase SQL editor to create the `activities` and `conversations` tables.

### 4. Deploy to GCS VM

```bash
# One-time VM setup (run on the VM)
bash setup.sh

# Install as a systemd service (run on the VM)
bash install_service.sh

# Deploy updates from your Mac
bash deploy.sh
```

### 5. Verify

```
/status   ← should show scheduler running and next EOD trigger time
```

---

## Environment variables

See `.env.example` for the full list. Required vars:

```
TELEGRAM_BOT_TOKEN      Your bot token from BotFather
ANTHROPIC_API_KEY       Your Anthropic API key
TELEGRAM_USER_ID        Your Telegram numeric user ID
```

Optional (Supabase + Obsidian):
```
SUPABASE_URL
SUPABASE_KEY
GITHUB_TOKEN            For pushing Obsidian daily notes
GITHUB_REPO             e.g. username/obsidian-vault
OBSIDIAN_NOTES_PATH     Path inside the repo for daily notes
```

---

## Project structure

```
eod_bot.py          Main bot — all commands, scheduler, Claude integration
personality.md      Your voice profile used in Claude prompts
requirements.txt    Python dependencies
schema.sql          Supabase table definitions
.env.example        Environment variable template (never commit .env!)
deploy.sh           Mac → GCS VM deploy script
setup.sh            One-time VM setup (Python, venv, dependencies)
install_service.sh  Installs bot as a systemd service
```

---

## Security note

Never commit your `.env` file. The `.gitignore` excludes it, along with `venv/`, `*.json` data files, and `__pycache__`. All secrets are read from environment variables at runtime.
