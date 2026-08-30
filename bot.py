"""
Telegram Task Manager Bot - powered by Gemini (free tier) via the Interactions API.
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

client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.5-flash-lite"]

DB_PATH = "tasks.db"

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
        CREATE TABLE IF NOT EXISTS chat_state (
            chat_id INTEGER PRIMARY KEY,
            last_interaction_id TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def get_last_interaction_id(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT last_interaction_id FROM chat_state WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def save_last_interaction_id(chat_id: int, interaction_id: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO chat_state (chat_id, last_interaction_id) VALUES (?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET last_interaction_id=excluded.last_interaction_id""",
        (chat_id, interaction_id),
    )
    conn.commit()
    conn.close()


def parse_reply(raw: str):
    """Split Gemini's reply into (user_facing_text, tasks_list)."""
    tasks = []
    reply_text = (raw or "").strip()

    match = re.search(r"TASKS:\s*(\[.*\])\s*$", raw or "", re.DOTALL)
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


def is_transient_gemini_error(error: Exception) -> bool:
    """True if the error is likely temporary (worth trying a fallback model),
    False if it's a permanent problem (bad key, bad request) that no fallback will fix."""
    text = str(error).lower()
    transient_signals = [
        "high demand", "internal server error", "service unavailable",
        "unavailable", "500", "502", "503", "504", "429",
        "rate limit", "too many requests",
    ]
    return any(signal in text for signal in transient_signals)


def run_interaction(chat_id: int, content_input):
    """Call the Gemini Interactions API, chaining conversation via previous_interaction_id.
    Tries each model in GEMINI_MODELS in order on transient errors (e.g. temporary high demand)."""
    prev_id = get_last_interaction_id(chat_id)

    last_error = None
    for model_name in GEMINI_MODELS:
        kwargs = dict(
            model=model_name,
            input=content_input,
            system_instruction=SYSTEM_PROMPT,
            store=True,
        )
        if prev_id:
            kwargs["previous_interaction_id"] = prev_id

        try:
            interaction = client.interactions.create(**kwargs)
            save_last_interaction_id(chat_id, interaction.id)
            return interaction.output_text
        except Exception as e:
            last_error = e
            if not is_transient_gemini_error(e):
                logger.exception(f"Non-transient Gemini error on {model_name}")
                raise
            logger.warning(f"Gemini model {model_name} temporarily failed: {e}")
            continue

    logger.error("All Gemini fallback models failed")
    raise last_error


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text
    await update.message.chat.send_action("typing")

    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    prompt = f"(النهارده {now}) {text}"

    try:
        output_text = run_interaction(chat_id, prompt)
    except Exception:
        await update.message.reply_text("النظام مزحوم شوية دلوقتي، جرب تاني كمان ثواني 🙏")
        return

    reply_text, tasks = parse_reply(output_text)

    added = save_tasks(chat_id, tasks)
    final_reply = reply_text + format_added_tasks(added)

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

        content_input = [
            {"type": "text", "text": f"(النهارده {now}) استمع للرسالة الصوتية دي:"},
            {"type": "audio", "data": audio_bytes, "mime_type": "audio/ogg"},
        ]
        try:
            output_text = run_interaction(chat_id, content_input)
        except Exception:
            await update.message.reply_text("النظام مزحوم شوية دلوقتي، جرب تاني كمان ثواني 🙏")
            return
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

    reply_text, tasks = parse_reply(output_text)
    added = save_tasks(chat_id, tasks)
    final_reply = reply_text + format_added_tasks(added)

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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled Telegram error", exc_info=context.error)


def main():
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tasks", list_tasks))
    app.add_handler(CommandHandler("done", done_task))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_error_handler(error_handler)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", minutes=5, args=[app])
    scheduler.start()

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
