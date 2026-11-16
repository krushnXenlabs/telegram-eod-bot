#!/usr/bin/env python3
"""
Telegram EOD Bot powered by Claude AI
--------------------------------------
- Sends an hourly check-in asking what you did
- Claude asks intelligent follow-up questions until it has full details
- At midnight, auto-generates a formatted EOD report
- Handles multi-hour gaps (you can reply for 4 hrs at once, no problem)

Setup: see README.md
"""

import asyncio
import io
import json
import logging
import os
import re
import subprocess
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
import pytz

IST = pytz.timezone("Asia/Kolkata")

def _now_ist() -> datetime:
    """Return current time in IST (aware datetime)."""
    return datetime.now(IST)

from anthropic import Anthropic
from apscheduler.schedulers.asyncio import AsyncIOScheduler
try:
    from supabase import create_client, Client as SupabaseClient
    _SUPABASE_AVAILABLE = True
except ImportError:
    _SUPABASE_AVAILABLE = False
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import Update, constants
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

URL_REGEX = re.compile(r'https?://[^\s]+|www\.[^\s]+')

# ── Load environment variables ──────────────────────────────────────────────
load_dotenv()

TELEGRAM_BOT_TOKEN: str = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY: str  = os.environ["ANTHROPIC_API_KEY"]
TELEGRAM_USER_ID: int   = int(os.environ["TELEGRAM_USER_ID"])
IMGUR_CLIENT_ID: str    = os.environ.get("IMGUR_CLIENT_ID", "")
SUPABASE_URL: str       = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY: str       = os.environ.get("SUPABASE_KEY", "")
# Obsidian vault (optional — set to your cloned vault path on the VM)
OBSIDIAN_VAULT_PATH: str     = os.environ.get("OBSIDIAN_VAULT_PATH", "")
OBSIDIAN_DAILY_NOTES_DIR: str = os.environ.get("OBSIDIAN_DAILY_NOTES_DIR", "Daily Notes")

# ── Paths & logging ──────────────────────────────────────────────────────────
DATA_FILE        = Path("daily_activities.json")
MEMORY_FILE      = Path("memory.json")
PERSONALITY_FILE = Path("personality.md")
MEMORY_DAYS      = 7

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Personality profile ───────────────────────────────────────────────────────
def _load_personality() -> str:
    """Load personality.md and return its contents, or empty string if missing."""
    if PERSONALITY_FILE.exists():
        try:
            return PERSONALITY_FILE.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""

PERSONALITY = _load_personality()

# ── Anthropic client ─────────────────────────────────────────────────────────
client = Anthropic(api_key=ANTHROPIC_API_KEY)

# ── Supabase client (optional — gracefully disabled if creds missing) ─────────
supabase: "SupabaseClient | None" = None
if _SUPABASE_AVAILABLE and SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        logger.info("Supabase connected.")
    except Exception as _e:
        logger.warning("Supabase init failed: %s — running without it.", _e)

# ── In-memory state ──────────────────────────────────────────────────────────
confirmed_activities: list[dict] = []   # activities confirmed today
chat_history: list[dict]         = []   # rolling chat for current session
gathering_info: bool             = False
scheduler_paused: bool           = True    # starts paused; unpauses on good morning
gm_time: datetime | None         = None    # when good morning was said/sent today
# Attachments: {"type": "photo"|"file"|"url", "ref": file_id or url,
#               "name": filename or domain, "caption": str, "time": HH:MM,
#               "linked_activity": name or None}
attachments: list[dict]          = []

# ── Persistence helpers ───────────────────────────────────────────────────────

def load_state() -> None:
    """Load today's saved state from disk (survives restarts)."""
    global confirmed_activities, chat_history, gathering_info, attachments, paste_import
    global scheduler_paused, gm_time
    today = _today()
    if DATA_FILE.exists():
        try:
            data = json.loads(DATA_FILE.read_text())
            if data.get("date") == today:
                confirmed_activities = data.get("confirmed_activities", [])
                chat_history         = data.get("chat_history", [])
                gathering_info       = data.get("gathering_info", False)
                attachments          = data.get("attachments", [])
                paste_import.update(data.get("paste_import", {}))
                scheduler_paused     = data.get("scheduler_paused", True)
                gm_str               = data.get("gm_time")
                if gm_str:
                    gm_naive = datetime.fromisoformat(gm_str)
                    # Make timezone-aware in IST if naive (old saves were naive)
                    gm_time = IST.localize(gm_naive) if gm_naive.tzinfo is None else gm_naive.astimezone(IST)
                else:
                    gm_time = None
                logger.info("Loaded state — paused=%s gm=%s", scheduler_paused, gm_time)
                return
        except Exception as e:
            logger.warning("Could not load state: %s", e)
    confirmed_activities = []
    chat_history         = []
    gathering_info       = False
    attachments          = []
    paste_import.clear()
    scheduler_paused     = True
    gm_time              = None
    _save_state()


def _save_state() -> None:
    DATA_FILE.write_text(
        json.dumps(
            {
                "date": _today(),
                "confirmed_activities": confirmed_activities,
                "chat_history": chat_history,
                "gathering_info": gathering_info,
                "attachments": attachments,
                "paste_import": dict(paste_import),
                "scheduler_paused": scheduler_paused,
                "gm_time": gm_time.isoformat() if gm_time else None,
            },
            indent=2,
        )
    )


def _today() -> str:
    return _now_ist().strftime("%d-%m-%Y")


def _yesterday() -> str:
    from datetime import timedelta
    from datetime import timedelta
    return (_now_ist() - timedelta(days=1)).strftime("%d-%m-%Y")


# Backfill state — separate from the main daily state
backfill_session: dict = {}   # {"date": "...", "activities": [], "chat_history": [], "active": False}

# Paste-import state — waiting for user to paste an EOD report
paste_import: dict = {}       # {"active": True, "date": "DD-MM-YYYY"}


def _reset_for_new_day() -> None:
    global confirmed_activities, chat_history, gathering_info, attachments, scheduler_paused, gm_time
    confirmed_activities = []
    chat_history         = []
    gathering_info       = False
    attachments          = []
    scheduler_paused     = True   # pause overnight until good morning
    gm_time              = None
    _save_state()
    logger.info("Daily state reset for new day.")


# ── Memory helpers ────────────────────────────────────────────────────────────

def _load_memory() -> list[dict]:
    """Return list of daily memory entries, most recent last."""
    if MEMORY_FILE.exists():
        try:
            return json.loads(MEMORY_FILE.read_text())
        except Exception:
            pass
    return []


def _save_memory(entries: list[dict]) -> None:
    MEMORY_FILE.write_text(json.dumps(entries, indent=2))


def _build_memory_context() -> str:
    """Return a formatted string of recent days' summaries to inject into prompts.
    Prefers Obsidian vault if configured; falls back to memory.json."""
    # Prefer live Obsidian notes - richer and always up to date
    obsidian_ctx = _read_obsidian_context(MEMORY_DAYS)
    if obsidian_ctx:
        return obsidian_ctx
    # Fallback: legacy memory.json
    entries = _load_memory()
    if not entries:
        return ""
    recent = entries[-MEMORY_DAYS:]
    lines = ["--- Krushn's recent activity history (for context) ---"]
    for e in recent:
        lines.append(f"\n[{e['date']}]")
        lines.append(e["summary"])
    lines.append("--- End of history ---")
    return "\n".join(lines)


async def _update_personality(conversation: list[dict]) -> None:
    """After each session, Claude reads the conversation and refines personality.md
    with any new voice patterns, phrases, or preferences it noticed."""
    global PERSONALITY
    if not conversation:
        return
    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in conversation
    )
    current = PERSONALITY or "(no profile yet)"
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=(
                "You are maintaining a personality and writing style profile for Krushn Gandhi. "
                "Your job is to update the profile based on new evidence from a real conversation. "
                "Rules: "
                "- Keep everything already in the profile that is still accurate. "
                "- Add new patterns, phrases, or preferences you noticed in this conversation. "
                "- Remove anything the conversation contradicts. "
                "- Do NOT invent things not evidenced in the conversation. "
                "- Keep the same markdown structure as the existing profile. "
                "- Output ONLY the updated profile text, nothing else."
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Current profile:\n{current}\n\n"
                    f"New conversation to learn from:\n{convo_text}\n\n"
                    "Output the updated profile now."
                ),
            }],
        )
        updated = resp.content[0].text.strip()
        if updated:
            PERSONALITY_FILE.write_text(updated, encoding="utf-8")
            PERSONALITY = updated
            logger.info("personality.md updated from session.")
    except Exception as e:
        logger.warning("Personality update failed (non-fatal): %s", e)


async def _write_daily_memory(activities: list[dict], conversation: list[dict]) -> None:
    """Ask Claude to summarise today's session and append it to memory."""
    if not activities and not conversation:
        return
    payload = json.dumps(activities, indent=2)
    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in conversation
    ) if conversation else "(no conversation recorded)"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=(
            "You are summarising a daily activity log for future reference. "
            "Write a concise 3-8 sentence summary covering: what projects/tasks were worked on, "
            "any recurring themes or ongoing work, notable wins or struggles, and anything "
            "that would help ask better follow-up questions next time. "
            "Write in third person about Krushn. Be specific about project names. "
            "Never use long dashes (—). Use hyphens (-) or colons (:) instead. Plain text only. "
            + (f"Match Krushn's voice when describing reflections: {PERSONALITY[:300]}..." if PERSONALITY else "")
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Date: {_today()}\n\n"
                f"Confirmed activities:\n{payload}\n\n"
                f"Conversation:\n{convo_text}\n\n"
                "Write the summary now."
            ),
        }],
    )
    summary = response.content[0].text.strip()

    entries = _load_memory()
    # Replace if same date already exists
    entries = [e for e in entries if e.get("date") != _today()]
    entries.append({"date": _today(), "summary": summary})
    # Keep only last 30 days
    _save_memory(entries[-30:])
    logger.info("Memory updated for %s.", _today())
    # Write rich Obsidian daily note and push to git
    _write_obsidian_note(_today(), activities, summary)
    _git_sync_vault(f"EOD note: {_today()}")


async def _write_daily_memory_for(activities: list[dict], conversation: list[dict], date: str) -> None:
    """Like _write_daily_memory but for a specific date (used for backfill)."""
    if not activities and not conversation:
        return
    payload = json.dumps(activities, indent=2)
    convo_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in conversation
    ) if conversation else "(no conversation recorded)"

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=(
            "You are summarising a daily activity log for future reference. "
            "Write a concise 3-8 sentence summary covering: what projects/tasks were worked on, "
            "any recurring themes or ongoing work, notable wins or struggles, and anything "
            "that would help ask better follow-up questions next time. "
            "Write in third person about Krushn. Be specific about project names. "
            "Never use long dashes (—). Use hyphens (-) or colons (:) instead. Plain text only. "
            + (f"Match Krushn's voice when describing reflections: {PERSONALITY[:300]}..." if PERSONALITY else "")
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Date: {date}\n\n"
                f"Confirmed activities:\n{payload}\n\n"
                f"Conversation:\n{convo_text}\n\n"
                "Write the summary now."
            ),
        }],
    )
    summary = response.content[0].text.strip()
    entries = _load_memory()
    entries = [e for e in entries if e.get("date") != date]
    entries.append({"date": date, "summary": summary})
    _save_memory(sorted(entries[-30:], key=lambda x: x["date"]))
    logger.info("Memory updated for %s (backfill).", date)
    # Write rich Obsidian daily note and push to git
    _write_obsidian_note(date, activities, summary)
    _git_sync_vault(f"Backfill note: {date}")

# ── Supabase helpers ─────────────────────────────────────────────────────────

def _parse_hours(time_spent: str) -> float:
    """Convert '1 hr', '2.5 hrs', '30 min' etc. to a float number of hours."""
    if not time_spent:
        return 0.0
    s = time_spent.lower().strip()
    # Handle "X hr" / "X hrs" / "X hour" / "X hours"
    m = re.search(r'([\d.]+)\s*hr', s)
    if m:
        return float(m.group(1))
    # Handle "X min" / "X minutes"
    m = re.search(r'([\d.]+)\s*min', s)
    if m:
        return round(float(m.group(1)) / 60, 2)
    # Bare number fallback
    m = re.search(r'[\d.]+', s)
    if m:
        return float(m.group())
    return 0.0


def _extract_skills_sync(activity: dict) -> list[str]:
    """Ask Claude to extract 2-5 generic, aggregatable skill tags from a single activity."""
    text = (
        f"Activity: {activity.get('name', '')}\n"
        f"Outcome: {activity.get('outcome', '')}\n"
        f"Reflection: {activity.get('reflection', '')}"
    )
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            system=(
                "Extract 2-5 broad, reusable skill tags from this activity. "
                "Tags must be generic enough to appear across multiple activities so hours can be aggregated. "
                "Use domain/subject areas, not specific techniques. "
                "Good examples: 'Theory of Computation', 'Flutter', 'Python', 'Prompt Engineering', "
                "'Project Management', 'Frontend Development', 'System Design', 'AI Tools', "
                "'Public Law', 'Object-Oriented Programming', 'Product Thinking'. "
                "Bad examples (too specific): 'NFA-to-DFA conversion', 'Java interfaces', 'regex generation'. "
                "Return ONLY a JSON array of strings, nothing else. Example: [\"Flutter\", \"Firebase\"]"
            ),
            messages=[{"role": "user", "content": text}],
        )
        raw = resp.content[0].text.strip()
        # Strip markdown fences
        raw = raw.strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
        skills = json.loads(raw)
        return [s for s in skills if isinstance(s, str)][:6]
    except Exception as e:
        logger.warning("Skills extraction failed for '%s': %s", activity.get("name"), e)
        return []


async def _sync_activities_to_supabase(activities: list[dict], date: str,
                                        session_type: str = "checkin") -> None:
    """Enrich activities with skills and upsert to Supabase. Fire-and-forget."""
    if not supabase or not activities:
        return
    try:
        rows = []
        for a in activities:
            skills = _extract_skills_sync(a)
            rows.append({
                "date":           date,
                "name":           a.get("name", ""),
                "category":       a.get("category", ""),
                "time_spent_hrs": _parse_hours(a.get("time_spent", "")),
                "outcome":        a.get("outcome", ""),
                "reflection":     a.get("reflection", ""),
                "skills":         skills,
            })
        # upsert: on conflict (date, name) update all fields
        supabase.table("activities").upsert(rows, on_conflict="date,name").execute()
        logger.info("Supabase: upserted %d activities for %s.", len(rows), date)
    except Exception as e:
        logger.error("Supabase activity sync failed: %s", e)


def _fetch_activities_from_supabase(date: str) -> list[dict]:
    """Fetch activities for a given DD-MM-YYYY date from Supabase.
    Returns a list of activity dicts compatible with _generate_eod_for()."""
    if not supabase:
        return []
    try:
        resp = supabase.table("activities").select("*").eq("date", date).execute()
        rows = resp.data or []
        # Convert Supabase rows back to the activity dict format the EOD generator expects
        activities = []
        for r in rows:
            hrs = r.get("time_spent_hrs") or 0
            activities.append({
                "name":       r.get("name", ""),
                "category":   r.get("category", ""),
                "time_spent": f"{hrs:.1f} hrs",
                "outcome":    r.get("outcome", ""),
                "reflection": r.get("reflection", ""),
            })
        return activities
    except Exception as e:
        logger.error("Supabase fetch activities failed for %s: %s", date, e)
        return []


async def _save_conversation_to_supabase(messages: list[dict], date: str,
                                          session_type: str) -> None:
    """Save a full conversation to the conversations table."""
    if not supabase or not messages:
        return
    try:
        supabase.table("conversations").insert({
            "date":         date,
            "session_type": session_type,
            "messages":     messages,
        }).execute()
        logger.info("Supabase: saved %d-message conversation (%s) for %s.",
                    len(messages), session_type, date)
    except Exception as e:
        logger.error("Supabase conversation save failed: %s", e)


# ── Obsidian vault helpers ────────────────────────────────────────────────────

def _vault_enabled() -> bool:
    return bool(OBSIDIAN_VAULT_PATH and Path(OBSIDIAN_VAULT_PATH).exists())


def _daily_note_path(date: str) -> Path:
    """Return the .md path for a given DD-MM-YYYY date string."""
    d = datetime.strptime(date, "%d-%m-%Y")
    filename = d.strftime("%Y-%m-%d") + ".md"
    return Path(OBSIDIAN_VAULT_PATH) / OBSIDIAN_DAILY_NOTES_DIR / filename


def _write_obsidian_note(date: str, activities: list[dict], summary: str) -> None:
    """Create or overwrite a rich daily note in the Obsidian vault."""
    if not _vault_enabled():
        return

    try:
        d = datetime.strptime(date, "%d-%m-%Y")
        date_display = d.strftime("%B %d, %Y")

        personal      = [a for a in activities if a.get("category") == "Personal"]
        professional  = [a for a in activities if a.get("category") == "Professional"]
        personal_hrs  = sum(_parse_hours(a.get("time_spent", "")) for a in personal)
        prof_hrs      = sum(_parse_hours(a.get("time_spent", "")) for a in professional)
        total_hrs     = personal_hrs + prof_hrs

        def activity_block(a: dict) -> list[str]:
            hrs = _parse_hours(a.get("time_spent", ""))
            block = [
                f"### {a.get('name', 'Unnamed')} ({hrs:.1f} hr{'s' if hrs != 1 else ''})",
                f"**Outcome:** {a.get('outcome', '')}",
                f"**Reflection:** {a.get('reflection', '')}",
                "",
            ]
            return block

        lines: list[str] = [
            "---",
            f"date: {d.strftime('%Y-%m-%d')}",
            f"total_hours: {total_hrs:.1f}",
            f"personal_hours: {personal_hrs:.1f}",
            f"professional_hours: {prof_hrs:.1f}",
            "---",
            "",
            f"# {date_display}",
            "",
            "## Summary",
            "",
            summary,
            "",
        ]

        if personal:
            lines += [f"## Personal ({personal_hrs:.1f} hrs)", ""]
            for a in personal:
                lines += activity_block(a)

        if professional:
            lines += [f"## Professional ({prof_hrs:.1f} hrs)", ""]
            for a in professional:
                lines += activity_block(a)

        note_path = _daily_note_path(date)
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Obsidian note written: %s", note_path)
    except Exception as e:
        logger.error("Failed to write Obsidian note for %s: %s", date, e)


def _read_obsidian_context(n: int = 7) -> str:
    """Read summaries from the last N daily notes for prompt injection."""
    if not _vault_enabled():
        return ""
    daily_dir = Path(OBSIDIAN_VAULT_PATH) / OBSIDIAN_DAILY_NOTES_DIR
    if not daily_dir.exists():
        return ""
    notes = sorted(daily_dir.glob("*.md"), reverse=True)[:n]
    if not notes:
        return ""

    lines = ["--- Krushn's recent activity history (for context) ---"]
    for note_path in reversed(notes):
        try:
            # Filename is YYYY-MM-DD.md — convert back to DD-MM-YYYY for display
            d = datetime.strptime(note_path.stem, "%Y-%m-%d")
            date_str = d.strftime("%d-%m-%Y")
        except ValueError:
            continue
        content = note_path.read_text(encoding="utf-8")
        # Extract the Summary section (everything between "## Summary" and next "##")
        summary = ""
        in_summary = False
        for line in content.splitlines():
            if line.startswith("## Summary"):
                in_summary = True
                continue
            if in_summary:
                if line.startswith("## "):
                    break
                if line.strip():
                    summary += line.strip() + " "
        if summary:
            lines.append(f"\n[{date_str}]")
            lines.append(summary.strip())

    lines.append("--- End of history ---")
    return "\n".join(lines)


def _git_sync_vault(message: str = "Update daily notes") -> None:
    """Stage, commit, and push all vault changes to the remote git repo."""
    if not _vault_enabled():
        return
    vault = str(OBSIDIAN_VAULT_PATH)
    try:
        subprocess.run(["git", "-C", vault, "add", "-A"],
                       capture_output=True, timeout=30, check=False)
        result = subprocess.run(
            ["git", "-C", vault, "commit", "-m", message],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if "nothing to commit" in result.stdout:
            return   # no changes — skip push
        subprocess.run(["git", "-C", vault, "push"],
                       capture_output=True, timeout=60, check=False)
        logger.info("Obsidian vault synced to git: %s", message)
    except Exception as e:
        logger.warning("Git sync failed (non-fatal): %s", e)


# ── Cloud upload helpers ──────────────────────────────────────────────────────

async def _upload_photo_to_imgur(image_bytes: bytes, caption: str) -> str | None:
    """Upload image bytes to Imgur. Returns public URL or None on failure."""
    if not IMGUR_CLIENT_ID:
        logger.warning("IMGUR_CLIENT_ID not set — photo won't be uploaded.")
        return None
    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.post(
                "https://api.imgur.com/3/image",
                headers={"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"},
                data={"image": image_bytes, "type": "file", "title": caption[:100]},
                timeout=30,
            )
        )
        data = resp.json()
        if data.get("success"):
            return data["data"]["link"]
        logger.error("Imgur upload failed: %s", data)
    except Exception as e:
        logger.error("Imgur upload exception: %s", e)
    return None


async def _upload_file_to_catbox(file_bytes: bytes, filename: str) -> str | None:
    """Upload any file to catbox.moe (free, no account needed). Returns URL or None."""
    try:
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: requests.post(
                "https://catbox.moe/user/api.php",
                data={"reqtype": "fileupload"},
                files={"fileToUpload": (filename, io.BytesIO(file_bytes))},
                timeout=60,
            )
        )
        url = resp.text.strip()
        if url.startswith("https://"):
            return url
        logger.error("Catbox upload returned: %s", url)
    except Exception as e:
        logger.error("Catbox upload exception: %s", e)
    return None


# ── Claude helpers ────────────────────────────────────────────────────────────

INTERVIEWER_SYSTEM_BASE = """You are Krushn Gandhi's always-on activity logger. Krushn can message you at any time - \
mid-task, between meetings, late at night - to log what he's been doing. You're like a smart notebook that asks \
just enough questions to write a great EOD report later.

For each activity mentioned you need:
  1. Activity name (short, clear title)
  2. Category - "Personal" or "Professional"
  3. Time spent (approximate is fine, e.g. "1.5 hrs")
  4. Outcome - what was accomplished
  5. Reflection - personal thoughts

Rules:
- Be natural and brief - Krushn might be messaging mid-flow, so don't be verbose.
- Use the history below to make follow-ups specific and smart.
- Ask ONE follow-up question per activity if the outcome or reflection is vague or missing. Make it specific and smart - use memory context to ask things like "did you end up submitting the COI quiz?" rather than "what did you learn?".
- If outcome AND reflection are both present with real detail, infer minor missing fields and move on.
- Default time to "~1 hr" if not mentioned.
- Category rules — ALWAYS follow these exactly:
  * College lectures, labs, tutorials, NPTEL quizzes, exams, or any academic coursework = "Personal". No exceptions.
  * Work at Stemious/Atalup, client work, internship tasks, professional projects = "Professional".
  * Personal habits, fitness, food, entertainment = "Personal".
- Do NOT fire [DONE] until you have a genuine outcome and reflection for every activity mentioned. Vague answers like "it went well" are not enough — ask once more.
- When writing outcomes and reflections in the JSON, match Krushn's voice exactly (see personality profile below).
- As soon as you have a reasonable picture of all activities mentioned in this message batch, respond with ONLY:

  [DONE]
  {{"activities": [
    {{
      "name": "...",
      "category": "Personal or Professional",
      "time_spent": "X hrs",
      "outcome": "...",
      "reflection": "..."
    }}
  ]}}

- If Krushn says "done", "that's it", "nothing else", or seems frustrated - fire [DONE] immediately.
- Keep replies to 1-2 sentences max. No greetings, no filler.

{PERSONALITY_CONTEXT}

{MEMORY_CONTEXT}
"""


def _build_interviewer_system() -> str:
    """Build the interviewer system prompt with personality + fresh memory injected."""
    memory = _build_memory_context()
    personality_section = (
        f"--- Krushn's personality and writing style ---\n{PERSONALITY}\n--- End of personality ---"
        if PERSONALITY else ""
    )
    return (
        INTERVIEWER_SYSTEM_BASE
        .replace("{PERSONALITY_CONTEXT}", personality_section)
        .replace("{MEMORY_CONTEXT}", memory)
    )

EOD_PARSE_SYSTEM = """You are parsing a pasted EOD (End of Day) report into structured activity data.

Extract every activity mentioned and return ONLY this JSON (no other text, no markdown):

{"date": "DD-MM-YYYY or as found in the report", "activities": [
  {
    "name": "activity name",
    "category": "Personal or Professional",
    "time_spent": "X hrs",
    "outcome": "what was accomplished",
    "reflection": "personal thoughts"
  }
]}

Rules:
- Extract the date from the report if present; otherwise use the date provided by the user.
- Include every activity, even partially described ones.
- Map the report fields directly: Time Spent → time_spent, Outcome → outcome, Reflection → reflection.
- Infer category from section headers (Personal / Professional) in the report, BUT always apply these overrides:
  * College lectures, labs, tutorials, NPTEL quizzes, exams, or any academic coursework = "Personal". No exceptions.
  * Work at Stemious/Atalup, client work, internship tasks = "Professional".
- Return valid JSON only.
"""

FORCE_SAVE_SYSTEM = """Extract all activities from this conversation and return ONLY this JSON (no other text):

{"activities": [
  {
    "name": "activity name",
    "category": "Personal or Professional",
    "time_spent": "X hrs",
    "outcome": "what was accomplished",
    "reflection": "thoughts or observations"
  }
]}

Rules:
- Include every activity mentioned, even partially described ones.
- Infer any missing fields from context. Use "~1 hr" if time unknown.
- Category rules — ALWAYS follow these exactly:
  * College lectures, labs, tutorials, NPTEL quizzes, exams, or any academic coursework = "Personal". No exceptions.
  * Work at Stemious/Atalup, client work, internship tasks, professional projects = "Professional".
  * Personal habits, fitness, food, entertainment = "Personal".
- Return valid JSON only. No markdown, no explanation.
"""

EOD_SYSTEM = """You are generating a polished End-of-Day (EOD) report for Krushn Gandhi.

CRITICAL: Output plain text only. NO markdown, no **, no __, no backticks.
The report will be copy-pasted into WhatsApp directly.

Format EXACTLY as shown (copy the structure, bullets, and spacing):

Name: Krushn Gandhi
Date: {{DATE}}

Personal
Activity: [name]
• Time Spent : [X hrs]
• Outcome : [detailed outcome]
• Reflection : [personal reflection]

Professional
Activity: [name]
• Time Spent : [X hrs]
• Outcome : [detailed outcome]
• Reflection : [personal reflection]

Rules:
- Plain text only. Absolutely no markdown symbols.
- NEVER use long dashes (—). Use a hyphen (-) or colon (:) instead.
- Group under Personal / Professional. Omit a section if empty.
- Write outcomes and reflections in Krushn's exact voice (see personality profile below).
- Be specific - name tools, people, and projects exactly as given.
- Do NOT add any preamble or postamble - output only the report.

{PERSONALITY_CONTEXT}
"""


async def _ask_claude_interviewer(user_message: str) -> str:
    """Send a user message through the rolling interview conversation."""
    chat_history.append({"role": "user", "content": user_message})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=_build_interviewer_system(),   # fresh memory every call
        messages=chat_history,
    )
    reply = response.content[0].text
    chat_history.append({"role": "assistant", "content": reply})
    _save_state()
    return reply


async def _force_save_from_history() -> list[dict]:
    """Ask Claude to extract activities from whatever is in chat_history, even if [DONE] never fired."""
    if not chat_history:
        return []
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=FORCE_SAVE_SYSTEM,
        messages=chat_history + [{"role": "user", "content": "Extract all activities from our conversation above."}],
    )
    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    raw = raw.strip("`").strip()
    if raw.startswith("json"):
        raw = raw[4:].strip()
    data = json.loads(raw)
    return data.get("activities", [])


BACKFILL_SYSTEM_BASE = """You are doing a thorough end-of-day debrief with Krushn Gandhi about a PAST day ({DATE}).

Your job is to uncover every activity from that day, one at a time, with proper detail.

For EACH activity you need:
  1. Activity name
  2. Category — Personal or Professional
  3. Time spent
  4. Outcome — what was actually accomplished
  5. Reflection — how it went, any struggles, thoughts

How to run the interview:
- Start by asking broadly what Krushn worked on that day
- Ask ONE follow-up question at a time — never multiple at once
- Be warm and specific; use the context below to ask smart questions
- Do NOT rush — get proper detail for each activity

IMPORTANT — as soon as you have complete details for ONE activity, emit a save block
BEFORE asking about the next one. Format exactly like this (no extra prose around it):

  [SAVE_ACTIVITY]
  {{"name": "...", "category": "Personal or Professional", "time_spent": "X hrs", "outcome": "...", "reflection": "..."}}

Then on the next line, ask "What else did you work on that day?" or a natural follow-up.

When Krushn confirms there's nothing more, respond with ONLY:

  [DONE]

(No JSON needed at the end — activities are already saved incrementally via [SAVE_ACTIVITY].)

--- Context: what Krushn has logged TODAY ---
{TODAY_CONTEXT}
--- End context ---

{MEMORY_CONTEXT}
"""


def _build_backfill_system() -> str:
    date = backfill_session.get("date", "yesterday")
    memory = _build_memory_context()

    if confirmed_activities:
        today_lines = [f"- {a['name']} ({a['category']}, {a['time_spent']})" for a in confirmed_activities]
        today_context = "\n".join(today_lines)
    else:
        today_context = "(nothing logged yet today)"

    return (
        BACKFILL_SYSTEM_BASE
        .replace("{DATE}", date)
        .replace("{TODAY_CONTEXT}", today_context)
        .replace("{MEMORY_CONTEXT}", memory)
    )


async def _ask_claude_backfill(user_message: str) -> str:
    """Interview Claude for a backfill session (different date, separate history)."""
    backfill_session["chat_history"].append({"role": "user", "content": user_message})
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=_build_backfill_system(),
        messages=backfill_session["chat_history"],
    )
    reply = response.content[0].text
    backfill_session["chat_history"].append({"role": "assistant", "content": reply})
    return reply


def _format_attachments_for_eod(atts: list[dict]) -> str:
    """Render attachments as a readable block to pass to Claude, with real URLs."""
    if not atts:
        return ""
    lines = ["\n--- Attachments logged (use the real URLs in References lines) ---"]
    for a in atts:
        linked = a.get("linked_activity") or "unlinked"
        url    = a.get("url") or a.get("ref", "")   # real URL if uploaded, else raw ref
        caption = a.get("caption", "(no caption)")
        if a["type"] == "url":
            lines.append(f'URL | link: {url} | caption: {caption} | linked to: {linked}')
        elif a["type"] == "photo":
            lines.append(f'Photo | link: {url} | caption: {caption} | linked to: {linked}')
        else:
            lines.append(f'File: {a.get("name","file")} | link: {url} | caption: {caption} | linked to: {linked}')
    lines.append("---")
    return "\n".join(lines)


async def _generate_eod_for(activities: list[dict], date: str, atts: list[dict] | None = None) -> str:
    """Generate EOD report for a specific date and activity list."""
    payload = json.dumps(activities, indent=2)
    att_block = _format_attachments_for_eod(atts or [])
    personality_section = (
        f"--- Krushn's personality and writing style ---\n{PERSONALITY}\n--- End of personality ---"
        if PERSONALITY else ""
    )
    system = EOD_SYSTEM.replace("{{DATE}}", date).replace("{PERSONALITY_CONTEXT}", personality_section)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": f"Activities:\n{payload}\n{att_block}\n\nGenerate the EOD report now."}],
    )
    return response.content[0].text


async def _generate_eod() -> str:
    """Generate EOD report from all confirmed activities."""
    payload = json.dumps(confirmed_activities, indent=2)
    att_block = _format_attachments_for_eod(attachments)
    personality_section = (
        f"--- Krushn's personality and writing style ---\n{PERSONALITY}\n--- End of personality ---"
        if PERSONALITY else ""
    )
    system = EOD_SYSTEM.replace("{{DATE}}", _today()).replace("{PERSONALITY_CONTEXT}", personality_section)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system,
        messages=[
            {
                "role": "user",
                "content": f"Activities:\n{payload}\n{att_block}\n\nGenerate the EOD report now.",
            }
        ],
    )
    return response.content[0].text

# ── Scheduled jobs ────────────────────────────────────────────────────────────

def _set_good_morning() -> None:
    """Mark good morning and unpause scheduler. Call whenever GM is detected."""
    global scheduler_paused, gm_time
    scheduler_paused = False
    gm_time          = _now_ist()
    _save_state()


async def job_good_morning(app: Application) -> None:
    """At 10 AM: if Krushn hasn't said good morning yet, send it for him."""
    if not scheduler_paused:
        logger.info("Good morning job: already awake, skipping.")
        return
    _set_good_morning()
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text="☀️ Good morning, Krushn! Hope you slept well 🌟\nFirst check-in coming at 11:00 AM.",
        )
        logger.info("Bot-initiated good morning sent.")
    except Exception as e:
        logger.error("Failed to send good morning: %s", e)


async def job_hourly_checkin(app: Application) -> None:
    """Ping the user every hour — but only if at least 1 hr has passed since good morning."""
    now = _now_ist()
    hour = now.hour
    if 1 <= hour < 7:
        logger.info("Skipping check-in during sleep hours (%d:00 IST).", hour)
        return

    # Still paused (no good morning yet)
    if scheduler_paused:
        logger.info("Skipping hourly ping — scheduler paused.")
        return

    # Must be at least 1 hour after good morning
    if gm_time and (now - gm_time).total_seconds() < 3600:
        logger.info("Skipping hourly ping — less than 1 hr since good morning.")
        return

    # Don't interrupt an active conversation or backfill
    if gathering_info or backfill_session.get("active"):
        logger.info("Skipping hourly ping — session in progress.")
        return

    global chat_history
    chat_history = []
    _save_state()

    time_label = _now_ist().strftime("%I:%M %p")
    text = (
        f"⏰ *{time_label} check-in*\n\n"
        "Hey Krushn! Anything to log? Just reply whenever you're ready 💬"
    )
    try:
        await app.bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text=text,
            parse_mode=constants.ParseMode.MARKDOWN,
        )
    except Exception as e:
        logger.error("Failed to send hourly check-in: %s", e)


async def job_midnight_eod(app: Application) -> None:
    """Generate and send the EOD report at midnight."""
    if not confirmed_activities:
        await app.bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text="🌙 Midnight check — no activities were logged today. Start fresh tomorrow!",
        )
        _reset_for_new_day()
        return

    await app.bot.send_message(
        chat_id=TELEGRAM_USER_ID,
        text="🌙 *Midnight — generating your EOD report…*",
        parse_mode=constants.ParseMode.MARKDOWN,
    )

    try:
        report = await _generate_eod()
        # Telegram message size limit is ~4096 chars; split if needed
        for chunk in _split_message(f"📋 *EOD Report*\n\n{report}"):
            await app.bot.send_message(
                chat_id=TELEGRAM_USER_ID,
                text=chunk,
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        logger.info("EOD report sent successfully.")
    except Exception as e:
        logger.error("EOD generation failed: %s", e)
        await app.bot.send_message(
            chat_id=TELEGRAM_USER_ID,
            text=f"❌ EOD generation failed: {e}",
        )
    finally:
        # Save full-day memory summary before resetting
        try:
            await _write_daily_memory(confirmed_activities, [])
        except Exception as e:
            logger.warning("Memory write at midnight failed: %s", e)
        _reset_for_new_day()

# ── Message handler ───────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle all free-text messages from the authorised user."""
    global gathering_info, chat_history

    if update.effective_user.id != TELEGRAM_USER_ID:
        await update.message.reply_text("Sorry, this is a private bot.")
        return

    user_text = update.message.text.strip()

    # ── Paste-import mode: must come FIRST — pasted EODs can contain "good night" ──
    if paste_import.get("active"):
        target_date = paste_import["date"]
        paste_import.clear()
        _save_state()   # persist the cleared state immediately

        await update.message.reply_text(f"⏳ Parsing your EOD for {target_date}…")
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                system=EOD_PARSE_SYSTEM,
                messages=[{"role": "user", "content": f"Date context: {target_date}\n\n{user_text}"}],
            )
            raw = response.content[0].text.strip().strip("`").strip()
            if raw.startswith("json"):
                raw = raw[4:].strip()
            parsed = json.loads(raw)
            activities = parsed.get("activities", [])
            # Always trust the date the user explicitly set — don't let Claude
            # re-interpret the date from the report text (format mismatch risk).
            resolved_date = target_date

            if not activities:
                await update.message.reply_text("⚠️ Couldn't find any activities in that text. Check the format and try again.")
                return

            # Save to memory + Supabase
            await _write_daily_memory_for(activities, [], resolved_date)
            asyncio.create_task(_sync_activities_to_supabase(activities, resolved_date, "importeod"))

            names = "\n".join(f"  • {a['name']} ({a['category']}, {a['time_spent']})" for a in activities)
            await update.message.reply_text(
                f"✅ Imported {len(activities)} activities into memory for *{resolved_date}*:\n\n{names}\n\n"
                f"Memory updated - I'll use this context in future check-ins 🧠",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error("EOD paste-import failed: %s", e)
            await update.message.reply_text(f"❌ Parse failed: {e}\n\nMake sure you pasted the full EOD text.")
        return
    # ─────────────────────────────────────────────────────────────────────────

    # ── Natural language GN/GM detection removed ─────────────────────────────
    # Use /goodnight and /goodmorning commands only.
    # ─────────────────────────────────────────────────────────────────────────

    # ── Backfill session takes priority ──────────────────────────────────────
    if backfill_session.get("active"):
        reply = await _ask_claude_backfill(user_text)
        ydate  = backfill_session["date"]

        # Extract any [SAVE_ACTIVITY] blocks and save them immediately
        save_blocks = re.findall(r'\[SAVE_ACTIVITY\]\s*(\{.*?\})', reply, re.DOTALL)
        newly_saved = []
        for block in save_blocks:
            try:
                activity = json.loads(block)
                backfill_session["activities"].append(activity)
                newly_saved.append(activity["name"])
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Could not parse [SAVE_ACTIVITY] block: %s", e)

        # Strip save blocks from the display text
        display = re.sub(r'\[SAVE_ACTIVITY\]\s*\{.*?\}', '', reply, flags=re.DOTALL).strip()

        if "[DONE]" in display:
            display = display.replace("[DONE]", "").strip()
            all_activities = backfill_session["activities"]

            if display:
                await update.message.reply_text(display)

            if not all_activities:
                await update.message.reply_text("⚠️ No activities were saved during this session.")
            else:
                await update.message.reply_text(f"⏳ Generating EOD for {ydate}…")
                report = await _generate_eod_for(all_activities, ydate)
                for chunk in _split_message(f"📋 EOD Report — {ydate}\n\n{report}"):
                    await update.message.reply_text(chunk)

                bf_convo = list(backfill_session["chat_history"])
                asyncio.create_task(
                    _write_daily_memory_for(all_activities, bf_convo, ydate)
                )
                asyncio.create_task(_sync_activities_to_supabase(all_activities, ydate, "backfill"))
                asyncio.create_task(_save_conversation_to_supabase(bf_convo, ydate, "backfill"))

            backfill_session.clear()

        else:
            # Show confirmation for any activities just saved, then Claude's follow-up question
            if newly_saved:
                names = ", ".join(f"*{n}*" for n in newly_saved)
                await update.message.reply_text(
                    f"✅ Saved: {names}",
                    parse_mode=constants.ParseMode.MARKDOWN,
                )
            if display:
                for chunk in _split_message(display):
                    await update.message.reply_text(chunk)
        return
    # ─────────────────────────────────────────────────────────────────────────

    # Auto-extract and save any URLs found in the message
    urls_found = URL_REGEX.findall(user_text)
    for url in urls_found:
        _save_attachment({
            "type": "url",
            "ref": url,
            "name": urlparse(url).netloc or url,
            "caption": user_text.replace(url, "").strip() or "(no caption)",
            "time": _now_ist().strftime("%H:%M"),
            "linked_activity": _last_activity_name(),
        })

    # Start a fresh session only if one isn't already open
    if not gathering_info:
        gathering_info = True
        chat_history   = []   # only reset when starting fresh
        _save_state()
    # else: continue the existing conversation as-is

    reply = await _ask_claude_interviewer(user_text)

    if "[DONE]" in reply:
        # Extract the JSON block
        try:
            json_block = reply.split("[DONE]")[1].strip()
            start = json_block.index("{")
            end   = json_block.rindex("}") + 1
            data  = json.loads(json_block[start:end])
            new_activities = data.get("activities", [])
            confirmed_activities.extend(new_activities)

            names = ", ".join(f"*{a['name']}*" for a in new_activities)
            confirmation = (
                f"✅ Logged {len(new_activities)} activit{'y' if len(new_activities)==1 else 'ies'}: {names}\n\n"
                f"Total today: {len(confirmed_activities)} activit{'y' if len(confirmed_activities)==1 else 'ies'} saved. "
                f"I'll check back next hour! 🚀"
            )
            await update.message.reply_text(confirmation, parse_mode=constants.ParseMode.MARKDOWN)

            # Write memory + sync to Supabase in background
            # Use confirmed_activities (full day) not new_activities (this session only)
            completed_convo = list(chat_history)
            asyncio.create_task(_write_daily_memory(list(confirmed_activities), completed_convo))
            asyncio.create_task(_sync_activities_to_supabase(new_activities, _today(), "checkin"))
            asyncio.create_task(_save_conversation_to_supabase(completed_convo, _today(), "checkin"))
            asyncio.create_task(_update_personality(completed_convo))

            # Clear session
            gathering_info = False
            chat_history   = []
            _save_state()

        except (ValueError, json.JSONDecodeError, KeyError) as e:
            logger.error("Failed to parse Claude's [DONE] block: %s\nRaw reply: %s", e, reply)
            # Fallback — show the raw reply without the JSON blob
            clean = reply.split("[DONE]")[0].strip()
            if clean:
                await update.message.reply_text(clean)
    else:
        # Normal follow-up question from Claude — send it as-is
        for chunk in _split_message(reply):
            await update.message.reply_text(chunk)

# ── Attachment helpers ────────────────────────────────────────────────────────

def _last_activity_name() -> str | None:
    """Return the name of the most recently confirmed activity, if any."""
    return confirmed_activities[-1]["name"] if confirmed_activities else None


def _save_attachment(att: dict) -> None:
    attachments.append(att)
    _save_state()
    logger.info("Attachment saved: %s", att)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download photo from Telegram, upload to Imgur, save shareable URL."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    caption = update.message.caption or ""
    linked  = _last_activity_name()

    await update.message.reply_text("📷 Uploading photo…")

    try:
        tg_file = await context.bot.get_file(update.message.photo[-1].file_id)
        img_bytes = await tg_file.download_as_bytearray()
        url = await _upload_photo_to_imgur(bytes(img_bytes), caption or "EOD attachment")
    except Exception as e:
        logger.error("Photo download/upload failed: %s", e)
        url = None

    ref = url or "(upload failed — no link)"
    _save_attachment({
        "type": "photo",
        "ref": ref,
        "name": "photo",
        "caption": caption or "(no caption)",
        "time": datetime.now().strftime("%H:%M"),
        "linked_activity": linked,
        "url": url,
    })

    linked_msg = f"linked to *{linked}*" if linked else "not linked to an activity yet — use /linklast"
    if url:
        await update.message.reply_text(
            f"✅ Photo uploaded!\n🔗 {url}\n_{caption or 'no caption'}_\n{linked_msg}",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "⚠️ Upload failed (check IMGUR_CLIENT_ID in .env). Photo reference saved without a link.",
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Download file from Telegram, upload to Catbox, save shareable URL."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    doc     = update.message.document
    caption = update.message.caption or ""
    linked  = _last_activity_name()
    fname   = doc.file_name or "file"

    await update.message.reply_text(f"📎 Uploading {fname}…")

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        url = await _upload_file_to_catbox(bytes(file_bytes), fname)
    except Exception as e:
        logger.error("File download/upload failed: %s", e)
        url = None

    ref = url or "(upload failed — no link)"
    _save_attachment({
        "type": "file",
        "ref": ref,
        "name": fname,
        "caption": caption or "(no caption)",
        "time": datetime.now().strftime("%H:%M"),
        "linked_activity": linked,
        "url": url,
    })

    linked_msg = f"linked to *{linked}*" if linked else "not linked yet — use /linklast"
    if url:
        await update.message.reply_text(
            f"✅ *{fname}* uploaded!\n🔗 {url}\n_{caption or 'no caption'}_\n{linked_msg}",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            f"⚠️ Upload to Catbox failed. File reference saved without a link.",
        )


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(
        f"👋 *EOD Bot is live, Krushn!*\n\n"
        f"Your Telegram ID: `{uid}`\n\n"
        "I'll ping you every hour to log activities and send an EOD report at midnight.\n\n"
        "*Commands:*\n"
        "/checkin — Start a check-in (Claude will ask follow-ups)\n"
        "/done — Force-save the current conversation\n"
        "/log <text> — Quick-log instantly, no questions\n"
        "/yesterday — Backfill interview for yesterday\n"
        "/backfill DD-MM-YYYY — Backfill any past date\n"
        "/importeod DD-MM-YYYY — Paste an existing EOD to import into memory\n"
        "/eod — Generate EOD report right now\n"
        "/geteod [DD-MM-YYYY] — Retrieve a past EOD report\n"
        "/status — See today's logged activities\n"
        "/attachments — See today's saved attachments\n"
        "/linklast <activity> — Link last attachment to an activity\n"
        "/gn — Pause check-ins for the night\n"
        "/gm — Resume check-ins in the morning\n"
        "/clear — Clear today's activities (careful!)\n\n"
        "💡 Send any photo, file, or URL anytime — I'll save it automatically!",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/help — full command reference with examples."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    msg = (
        "📖 *EOD Bot - Command Reference*\n"
        "\n"
        "─────────────────────────\n"
        "🌅 *Daily Flow*\n"
        "─────────────────────────\n"
        "\n"
        "/gm — Start your day, unpause check-ins\n"
        "_Say 'good morning' or 'gm' anytime too_\n"
        "\n"
        "/gn — End your day, pause check-ins\n"
        "_Say 'good night', 'gn', or 'bye' anytime too_\n"
        "\n"
        "─────────────────────────\n"
        "📝 *Logging Activities*\n"
        "─────────────────────────\n"
        "\n"
        "/checkin — Start a check-in session\n"
        "_Claude will ask you follow-up questions until it has full detail_\n"
        "_Example: just type what you did and let Claude interview you_\n"
        "\n"
        "/done — Force-save the current session without waiting for Claude to finish\n"
        "_Use this if the conversation got cut off or you want to stop early_\n"
        "\n"
        "/log <text> — One-liner quick log, no interview\n"
        "_Example: `/log reviewed PRs with Nikil Sir, 1 hr, professional`_\n"
        "_Example: `/log TOC lecture - state minimization, 1 hr, personal`_\n"
        "\n"
        "─────────────────────────\n"
        "📅 *Past Days*\n"
        "─────────────────────────\n"
        "\n"
        "/yesterday — Interview to backfill yesterday\n"
        "_Claude will walk through the day with you one activity at a time_\n"
        "\n"
        "/backfill DD-MM-YYYY — Backfill any specific past date\n"
        "_Example: `/backfill 10-03-2026`_\n"
        "\n"
        "/importeod DD-MM-YYYY — Paste an existing EOD report directly into memory\n"
        "_Example: `/importeod 11-03-2026` then paste your full EOD text_\n"
        "_Defaults to yesterday if no date given_\n"
        "\n"
        "─────────────────────────\n"
        "📋 *Reports & Status*\n"
        "─────────────────────────\n"
        "\n"
        "/eod — Generate your EOD report right now (don't wait for 11:30 PM)\n"
        "\n"
        "/geteod — Get yesterday's EOD report\n"
        "_Example: `/geteod` or `/geteod 11-03-2026` for a specific date_\n"
        "\n"
        "/status — See everything logged so far today\n"
        "_Shows activity names, categories, and time spent_\n"
        "\n"
        "/memory — See what the bot remembers about your recent days\n"
        "_Last 7 days of summaries injected into every check-in_\n"
        "\n"
        "─────────────────────────\n"
        "📎 *Attachments*\n"
        "─────────────────────────\n"
        "\n"
        "/attachments — List all photos, files, and URLs saved today\n"
        "\n"
        "/linklast <activity name> — Link the last attachment to a specific activity\n"
        "_Example: `/linklast Demo Dashboard Build`_\n"
        "\n"
        "_Send any photo, file, or URL anytime - saved automatically_\n"
        "\n"
        "─────────────────────────\n"
        "🗑️ *Housekeeping*\n"
        "─────────────────────────\n"
        "\n"
        "/clear — Wipe today's activities, memory, and Supabase data\n"
        "_Use if you logged something wrong and want to start fresh_\n"
        "\n"
        "/start — Show bot info and your Telegram ID\n"
    )
    # Split across two messages to stay under Telegram's 4096 char limit
    for chunk in _split_message(msg):
        await update.message.reply_text(chunk, parse_mode=constants.ParseMode.MARKDOWN)


async def cmd_checkin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    global gathering_info, chat_history
    gathering_info = True
    chat_history   = []
    _save_state()
    await update.message.reply_text(
        "📝 *Manual Check-in*\n\nWhat have you been working on? Go ahead — as much detail as you like! 💪",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def cmd_eod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    if not confirmed_activities:
        await update.message.reply_text("📋 No activities logged yet today — nothing to report!")
        return
    await update.message.reply_text("⏳ Generating your EOD report…")
    try:
        report = await _generate_eod()
        for chunk in _split_message(f"📋 *EOD Report*\n\n{report}"):
            await update.message.reply_text(chunk, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    if not confirmed_activities:
        await update.message.reply_text("📋 No activities logged yet today.")
        return
    lines = [f"📊 *Today's Log ({_today()})*\n"]
    for i, a in enumerate(confirmed_activities, 1):
        lines.append(
            f"{i}. *{a['name']}* — {a['category']} — {a['time_spent']}"
        )
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-save whatever is in the current conversation, even if [DONE] never fired."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    global gathering_info, chat_history
    if not chat_history:
        await update.message.reply_text("Nothing in the current session to save. Use /checkin to start one.")
        return
    await update.message.reply_text("⏳ Saving what I have so far…")
    try:
        new_activities = await _force_save_from_history()
        if not new_activities:
            await update.message.reply_text("Couldn't find any activities to extract. Try /log instead.")
            return
        confirmed_activities.extend(new_activities)
        names = ", ".join(f"*{a['name']}*" for a in new_activities)
        await update.message.reply_text(
            f"✅ Force-saved {len(new_activities)} activit{'y' if len(new_activities)==1 else 'ies'}: {names}\n\n"
            f"Total today: {len(confirmed_activities)} saved. 🚀",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        # Write memory + sync to Supabase in background
        # Use confirmed_activities (full day) not new_activities (this session only)
        completed_convo = list(chat_history)
        asyncio.create_task(_write_daily_memory(list(confirmed_activities), completed_convo))
        asyncio.create_task(_sync_activities_to_supabase(new_activities, _today(), "checkin"))
        asyncio.create_task(_save_conversation_to_supabase(completed_convo, _today(), "checkin"))
        asyncio.create_task(_update_personality(completed_convo))
        gathering_info = False
        chat_history   = []
        _save_state()
    except Exception as e:
        logger.error("Force-save failed: %s", e)
        await update.message.reply_text(f"❌ Save failed: {e}\n\nTry /log <activity description> instead.")


async def cmd_log(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/log <text> — instantly save a raw note, no questions asked."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    raw = " ".join(context.args) if context.args else ""
    if not raw:
        await update.message.reply_text(
            "Usage: `/log <what you did>`\n\nExample:\n`/log Built EOD bot with Cowork, ~1 hr, professional`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return
    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=FORCE_SAVE_SYSTEM,
            messages=[{"role": "user", "content": raw}],
        )
        text = response.content[0].text.strip().strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
        data = json.loads(text)
        new_activities = data.get("activities", [])
        confirmed_activities.extend(new_activities)
        names = ", ".join(f"*{a['name']}*" for a in new_activities)
        await update.message.reply_text(
            f"✅ Logged: {names}\nTotal today: {len(confirmed_activities)} activit{'y' if len(confirmed_activities)==1 else 'ies'}. 🚀",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        asyncio.create_task(_sync_activities_to_supabase(new_activities, _today(), "log"))
        _save_state()
    except Exception as e:
        logger.error("Quick log failed: %s", e)
        await update.message.reply_text(f"❌ Error: {e}")


async def _start_backfill(date_str: str, update: Update) -> None:
    """Shared logic to kick off a backfill interview for any date."""
    backfill_session.clear()
    backfill_session.update({
        "active": True,
        "date": date_str,
        "activities": [],
        "chat_history": [],
    })
    await update.message.reply_text(
        f"📅 *Backfilling {date_str}*\n\nLet me ask you about it properly — one thing at a time 🙌",
        parse_mode=constants.ParseMode.MARKDOWN,
    )
    opening = await _ask_claude_backfill(
        f"Please start the debrief for {date_str}. Ask me what I worked on that day."
    )
    await update.message.reply_text(opening)


async def cmd_importeod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/importeod DD-MM-YYYY — paste an existing EOD report to import into memory."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return

    raw = " ".join(context.args).strip() if context.args else ""

    # Try to parse date, default to yesterday if none given
    parsed = None
    if raw:
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue

    if raw and not parsed:
        await update.message.reply_text(
            "Couldn't read that date. Use: `/importeod 11-03-2026`\n"
            "Or just `/importeod` to default to yesterday.",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    date_str = parsed.strftime("%d-%m-%Y") if parsed else _yesterday()

    # Cancel any active session so the pasted text isn't treated as a check-in
    global gathering_info, chat_history
    gathering_info = False
    chat_history   = []
    backfill_session.clear()

    paste_import.clear()
    paste_import["active"] = True
    paste_import["date"]   = date_str
    _save_state()   # persist so a bot restart won't lose this mode

    await update.message.reply_text(
        f"📋 *Importing EOD for {date_str}*\n\n"
        "Paste your EOD report now and I'll extract all the activities into memory.\n"
        "_(Bot restart safe — I'll remember you're in import mode)_",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def cmd_yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/yesterday — backfill interview for yesterday."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    await _start_backfill(_yesterday(), update)


async def cmd_backfill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/backfill DD-MM-YYYY — backfill interview for any past date."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return

    raw = " ".join(context.args).strip() if context.args else ""

    # Try to parse and normalise whatever format the user gives
    parsed = None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            break
        except ValueError:
            continue

    if not parsed:
        await update.message.reply_text(
            "Please give me a date, e.g.:\n`/backfill 11-03-2026`",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    if parsed.date() >= _now_ist().date():
        await update.message.reply_text("That date is today or in the future — use /checkin instead!")
        return

    date_str = parsed.strftime("%d-%m-%Y")
    await _start_backfill(date_str, update)


async def cmd_attachments(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/attachments — show all attachments saved today."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    if not attachments:
        await update.message.reply_text("No attachments saved today.")
        return
    lines = [f"📎 *Today's Attachments*\n"]
    for i, a in enumerate(attachments, 1):
        icon = "📷" if a["type"] == "photo" else ("🔗" if a["type"] == "url" else "📎")
        linked = a.get("linked_activity") or "unlinked"
        lines.append(f"{i}. {icon} {a.get('name','—')} | _{a.get('caption','—')}_ | linked: *{linked}* | {a['time']}")
    await update.message.reply_text("\n".join(lines), parse_mode=constants.ParseMode.MARKDOWN)


async def cmd_linklast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/linklast <activity name> — link the last saved attachment to an activity."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    if not attachments:
        await update.message.reply_text("No attachments saved yet today.")
        return
    activity_name = " ".join(context.args).strip() if context.args else ""
    if not activity_name:
        # Show last activity as default suggestion
        suggestion = f"`/linklast {_last_activity_name()}`" if _last_activity_name() else "provide an activity name"
        await update.message.reply_text(
            f"Usage: `/linklast <activity name>`\n\nLast activity: {suggestion}",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return
    attachments[-1]["linked_activity"] = activity_name
    _save_state()
    att = attachments[-1]
    icon = "📷" if att["type"] == "photo" else ("🔗" if att["type"] == "url" else "📎")
    await update.message.reply_text(
        f"{icon} Linked *{att.get('name','attachment')}* → *{activity_name}* ✅",
        parse_mode=constants.ParseMode.MARKDOWN,
    )


async def cmd_geteod(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/geteod [DD-MM-YYYY] — retrieve a past EOD report. Defaults to yesterday."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return

    raw = " ".join(context.args).strip() if context.args else ""

    # Parse date — default to yesterday if none provided
    if raw:
        parsed = None
        for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%d-%m-%y", "%d/%m/%y", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if not parsed:
            await update.message.reply_text(
                "Please give me a date, e.g.:\n`/geteod 11-03-2026`\nor just `/geteod` for yesterday.",
                parse_mode=constants.ParseMode.MARKDOWN,
            )
            return
        date_str = parsed.strftime("%d-%m-%Y")
    else:
        date_str = _yesterday()

    await update.message.reply_text(f"📄 Fetching EOD for *{date_str}*...", parse_mode=constants.ParseMode.MARKDOWN)

    # ── 1. Try Obsidian vault note (already-generated report) ────────────────
    if _vault_enabled():
        note_path = _daily_note_path(date_str)
        if note_path.exists():
            content = note_path.read_text(encoding="utf-8")
            # Extract the EOD Report section
            eod_text = ""
            in_eod = False
            for line in content.splitlines():
                if line.startswith("## EOD Report"):
                    in_eod = True
                    continue
                if in_eod:
                    if line.startswith("## "):
                        break
                    eod_text += line + "\n"
            if eod_text.strip():
                for chunk in _split_message(eod_text.strip()):
                    await update.message.reply_text(chunk)
                return

    # ── 2. Try Supabase activities ────────────────────────────────────────────
    activities = _fetch_activities_from_supabase(date_str)

    # ── 3. Fall back to memory.json activities ────────────────────────────────
    if not activities:
        entries = _load_memory()
        entry = next((e for e in entries if e.get("date") == date_str), None)
        if entry:
            activities = entry.get("activities", [])

    if not activities:
        await update.message.reply_text(
            f"😕 No data found for *{date_str}*.\n\nTry `/backfill {date_str}` to log activities for that day.",
            parse_mode=constants.ParseMode.MARKDOWN,
        )
        return

    source = "Supabase" if _fetch_activities_from_supabase.__doc__ and supabase else "stored memory"
    await update.message.reply_text(f"🤖 Regenerating EOD from {len(activities)} activities...")
    try:
        report = await _generate_eod_for(activities, date_str)
        for chunk in _split_message(report):
            await update.message.reply_text(chunk)
    except Exception as e:
        logger.error("cmd_geteod regeneration failed: %s", e)
        await update.message.reply_text(f"❌ Failed to regenerate EOD: {e}")


async def cmd_memory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/memory — show all stored memory summaries."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    entries = _load_memory()
    if not entries:
        await update.message.reply_text("🧠 No memory stored yet. Complete a check-in session or use /importeod to populate it.")
        return
    lines = [f"🧠 *Memory ({len(entries)} days stored)*\n"]
    for e in sorted(entries, key=lambda x: x["date"]):
        lines.append(f"📅 *{e['date']}*\n{e['summary']}\n")
    for chunk in _split_message("\n".join(lines)):
        await update.message.reply_text(chunk, parse_mode=constants.ParseMode.MARKDOWN)


async def cmd_gn(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/gn — pause hourly check-ins for the night."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    global scheduler_paused
    scheduler_paused = True
    _save_state()
    await update.message.reply_text(
        "🌙 Good night! Check-ins paused.\nI'll send a good morning at 10 AM if you haven't already ☀️"
    )


async def cmd_gm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/gm — resume hourly check-ins after sleep."""
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    _set_good_morning()
    await update.message.reply_text(
        "☀️ Good morning! First check-in in 1 hour 🚀"
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user.id != TELEGRAM_USER_ID:
        return
    today = _today()
    _reset_for_new_day()
    # Wipe today's memory entry
    entries = _load_memory()
    entries = [e for e in entries if e.get("date") != today]
    _save_memory(entries)
    # Wipe today's Obsidian note if it exists
    note_path = _daily_note_path(today)
    if note_path.exists():
        note_path.unlink()
        _git_sync_vault(f"Clear: removed {today}")
    # Wipe today's Supabase activities
    if supabase:
        try:
            supabase.table("activities").delete().eq("date", today).execute()
            logger.info("Supabase: cleared activities for %s.", today)
        except Exception as e:
            logger.error("Supabase clear failed: %s", e)
    await update.message.reply_text("🗑️ Today's activities, memory, and Supabase data cleared. Starting completely fresh!", parse_mode="Markdown")

# ── Utility ───────────────────────────────────────────────────────────────────

def _split_message(text: str, limit: int = 4000) -> list[str]:
    """Split a long message into Telegram-safe chunks."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)
    return chunks

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    load_state()
    logger.info("Starting EOD bot for user %d …", TELEGRAM_USER_ID)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("checkin", cmd_checkin))
    app.add_handler(CommandHandler("done",      cmd_done))
    app.add_handler(CommandHandler("log",       cmd_log))
    app.add_handler(CommandHandler("importeod",  cmd_importeod))
    app.add_handler(CommandHandler("yesterday",  cmd_yesterday))
    app.add_handler(CommandHandler("backfill",    cmd_backfill))
    app.add_handler(CommandHandler("eod",         cmd_eod))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("attachments", cmd_attachments))
    app.add_handler(CommandHandler("linklast",    cmd_linklast))
    app.add_handler(CommandHandler("geteod",      cmd_geteod))
    app.add_handler(CommandHandler("memory",      cmd_memory))
    app.add_handler(CommandHandler("gn",          cmd_gn))
    app.add_handler(CommandHandler("gm",          cmd_gm))
    app.add_handler(CommandHandler("clear",       cmd_clear))

    # Media — works at any time, no session needed
    app.add_handler(MessageHandler(filters.PHOTO,    handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Free text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Scheduler — runs inside the same asyncio event loop as the bot
    scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
    IST_TZ = "Asia/Kolkata"   # explicit timezone on every trigger — don't rely on inheritance

    # 10 AM IST wake-up — sends good morning if Krushn hasn't said it yet
    scheduler.add_job(
        job_good_morning,
        CronTrigger(hour=10, minute=0, timezone=IST_TZ),
        args=[app],
        id="good_morning",
    )

    # Every hour on the IST hour mark
    scheduler.add_job(
        job_hourly_checkin,
        CronTrigger(minute=0, timezone=IST_TZ),
        args=[app],
        id="hourly_checkin",
    )

    # Midnight IST EOD
    scheduler.add_job(
        job_midnight_eod,
        CronTrigger(hour=23, minute=30, timezone=IST_TZ),
        args=[app],
        id="midnight_eod",
    )

    scheduler.start()
    logger.info("Scheduler started. Bot is polling…")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
