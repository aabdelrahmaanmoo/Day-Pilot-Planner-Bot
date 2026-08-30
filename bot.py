"""
Telegram Task Manager Bot - powered by Gemini (free tier)
Natural conversation: understands text & voice, asks follow-up questions,
extracts/organizes tasks, and sends reminders.
"""

import os
import re
import json
import sqlite3
import logging
from datetime import datetime, timedelta

from google import genai
from google.genai import types
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

VOICE_DIR = "voices"
os.makedirs(VOICE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(api_version="v1"))
MODEL_NAME = "gemini-2.5-flash"


def log_available_models():
    """Log which models this API key can actually use, to make future debugging instant."""
    try:
        names = [m.name for m in client.models.list() if "generateContent" in (m.supported_actions or [])]
        logger.info(f"AVAILABLE MODELS FOR THIS KEY: {names}")
    except Exception as e:
        logger.error(f"Could not list models: {e}")

DB_PATH = "tasks.db"
HISTORY_TURNS = 6  # how many past messages to keep as conversation context

SYSTEM_PROMPT = """انت مساعد شخصي بتتكلم مع المستخدم بشكل طبيعي وودود باللهجة المصرية، ومتخصص في تنظيم التاسكات (المهام) اليومية.

قواعد مهمة:
- لو المستخدم ذكر حاجة عايز يعملها (تاسك)، افهمها واستخرجها.
- لو التاسك ناقص تفاصيل مهمة (زي الميعاد) واحتاجت تسأل، اسأل سؤال طبيعي قصير بدل ما تفترض.
- لو المستخدم بيرد على سؤالك (زي بيحدد ميعاد)، اربط ردّه بالتاسك اللي كان ناقص من المحادثة اللي فاتت.
- لو مفيش تاسك في الرسالة (المستخدم بيسلم عليك بس أو بيسأل سؤال عادي)، رد عادي من غير ما تستخرج تاسكات.
- ردودك تبقى طبيعية ومختصرة، زي ما صاحب بيرد على صاحبه.

مهم جدًا: في آخر ردك، لازم تحط سطر منفصل يبدأ بـ TASKS: متبوع بـ JSON array لأي تاسكات جاهزة للحفظ (عندها title واضح، due_date لو موجود بصيغة YYYY-MM-DD HH:MM أو null، priority: high/medium/low).
لو مفيش تاسكات جاهزة للحفظ دلوقتي (زي لو لسه بتسأل سؤال متابعة)، حط: TASKS: []

مثال لرد كامل:
تمام، حددتلك ميعاد البنك الساعة 9 الصبح بكرة 👍
TASKS: [{"title": "اروح البنك", "due_date": "2025-01-15 09:00", "priority": "high"}]
"""


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            due_date TEXT,
            priority TEXT DEFAULT 'medium',
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reminded INTEGER DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def get_history(chat_id: int) -> list:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT role, content FROM chat_history WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, HISTORY_TURNS),
    ).fetchall()
    conn.close()
    return list(reversed(rows))


def save_history(chat_id: int, role: str, content: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO chat_history (chat_id, role, content, created_at) VALUES (?, ?, ?, ?)",
        (chat_id, role, content, datetime.now().isoformat()),
    )
    # keep table small: delete anything beyond last 20 turns per chat
    conn.execute(
        """DELETE FROM chat_history WHERE chat_id=? AND id NOT IN (
             SELECT id FROM chat_history WHERE chat_id=? ORDER BY id DESC LIMIT 20
           )""",
        (chat_id, chat_id),
    )
    conn.commit()
    conn.close()


def parse_reply(raw: str):
    """Split Gemini's reply into (user_facing_text, tasks_list)."""
    tasks = []
    reply_text = raw.strip()

    match = re.search(r"TASKS:\s*(\[.*\])\s*$", raw, re.DOTALL)
    if match:
        json_part = match.group(1)
        reply_text = raw[: match.start()].strip()
        try:
            tasks = json.loads(json_part)
        except json.JSONDecodeError:
            logger.error(f"Failed to parse TASKS json: {json_part}")
            tasks = []

    if not reply_text:
        reply_text = "تمام 👍"

    return reply_text, tasks


def build_contents(chat_id: int, new_parts: list) -> list:
    """Build the multi-turn contents list for Gemini from stored history + new message.
    new_parts: list of strings and/or types.Part (e.g. uploaded audio file)."""
    contents = []
    for role, content in get_history(chat_id):
        gemini_role = "user" if role == "user" else "model"
        contents.append(types.Content(role=gemini_role, parts=[types.Part.from_text(text=content)]))

    parts = []
    for p in new_parts:
        if isinstance(p, str):
            parts.append(types.Part.from_text(text=p))
        else:
            parts.append(p)  # already a types.Part (e.g. uploaded file)
    contents.append(types.Content(role="user", parts=parts))
    return contents


def save_tasks(chat_id: int, tasks: list) -> list:
    if not tasks:
        return []
    conn = sqlite3.connect(DB_PATH)
    added = []
    for t in tasks:
        title = (t.get("title") or "").strip()
        if not title:
            continue
        due_date = t.get("due_date")
        priority = t.get("priority", "medium")
        conn.execute(
            "INSERT INTO tasks (chat_id, title, due_date, priority, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, title, due_date, priority, datetime.now().isoformat()),
        )
        added.append((title, due_date, priority))
    conn.commit()
    conn.close()
    return added


def format_added_tasks(added: list) -> str:
    if not added:
        return ""
    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = ["\n\n📌 اتسجل:"]
    for title, due_date, priority in added:
        line = f"{priority_emoji.get(priority, '🟡')} {title}"
        if due_date:
            line += f" — {due_date}"
        lines.append(line)
    return "\n".join(lines)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text
    await update.message.chat.send_action("typing")

    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    contents = build_contents(chat_id, [f"(النهارده {now}) {text}"])

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=contents,
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
    )
    reply_text, tasks = parse_reply(response.text)

    added = save_tasks(chat_id, tasks)
    final_reply = reply_text + format_added_tasks(added)

    save_history(chat_id, "user", text)
    save_history(chat_id, "model", reply_text)

    await update.message.reply_text(final_reply)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    await update.message.chat.send_action("typing")

    voice = update.message.voice or update.message.audio
    tg_file = await context.bot.get_file(voice.file_id)
    file_path = os.path.join(VOICE_DIR, f"{voice.file_id}.ogg")
    await tg_file.download_to_drive(file_path)

    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        with open(file_path, "rb") as f:
            audio_bytes = f.read()
        audio_part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg")
        contents = build_contents(
            chat_id, [f"(النهارده {now}) استمع للرسالة الصوتية دي:", audio_part]
        )
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=contents,
            config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT),
        )
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

    reply_text, tasks = parse_reply(response.text)
    added = save_tasks(chat_id, tasks)
    final_reply = reply_text + format_added_tasks(added)

    save_history(chat_id, "user", "[رسالة صوتية]")
    save_history(chat_id, "model", reply_text)

    await update.message.reply_text(final_reply)


async def list_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, title, due_date, priority FROM tasks WHERE chat_id=? AND status='pending' ORDER BY due_date IS NULL, due_date ASC",
        (chat_id,),
    ).fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("مفيش تاسكات دلوقتي 🎉")
        return

    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = ["📋 التاسكات بتاعتك:\n"]
    for task_id, title, due_date, priority in rows:
        line = f"{priority_emoji.get(priority, '🟡')} #{task_id} {title}"
        if due_date:
            line += f" — {due_date}"
        lines.append(line)
    lines.append("\nعشان تقفل تاسك: /done رقم_التاسك")

    await update.message.reply_text("\n".join(lines))


async def done_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if not context.args:
        await update.message.reply_text("اكتب رقم التاسك: /done 3")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("رقم التاسك لازم يكون رقم صحيح.")
        return

    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "UPDATE tasks SET status='done' WHERE id=? AND chat_id=?", (task_id, chat_id)
    )
    conn.commit()
    conn.close()

    if cur.rowcount:
        await update.message.reply_text(f"✅ تمام، قفلت تاسك #{task_id}")
    else:
        await update.message.reply_text("مش لاقي تاسك بالرقم ده.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أهلاً! أنا مساعدك الشخصي 🙂 كلمني عادي (كتابة أو صوت) عن أي حاجة عايز تعملها وأنا هنظمهالك.\n\n"
        "الأوامر:\n"
        "/tasks - شوف التاسكات المفتوحة\n"
        "/done [رقم] - اقفل تاسك"
    )


async def check_reminders(app: Application):
    conn = sqlite3.connect(DB_PATH)
    now = datetime.now()
    window_end = now + timedelta(hours=1)
    rows = conn.execute(
        """SELECT id, chat_id, title, due_date FROM tasks
           WHERE status='pending' AND reminded=0 AND due_date IS NOT NULL"""
    ).fetchall()

    for task_id, chat_id, title, due_date in rows:
        try:
            due = datetime.strptime(due_date, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            continue
        if now <= due <= window_end:
            try:
                await app.bot.send_message(
                    chat_id=chat_id, text=f"⏰ تذكير: {title} — الساعة {due.strftime('%H:%M')}"
                )
                conn.execute("UPDATE tasks SET reminded=1 WHERE id=?", (task_id,))
                conn.commit()
            except Exception as e:
                logger.error(f"Failed to send reminder: {e}")
    conn.close()


def main():
    init_db()
    log_available_models()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tasks", list_tasks))
    app.add_handler(CommandHandler("done", done_task))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", minutes=5, args=[app])
    scheduler.start()

    app.run_polling()


if __name__ == "__main__":
    main()
