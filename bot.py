"""
Telegram Task Manager Bot - powered by Gemini (free tier)
Sends free-text messages -> Gemini extracts structured tasks -> stores in SQLite -> sends reminders.
"""

import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta

import google.generativeai as genai
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

DB_PATH = "tasks.db"


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
    conn.commit()
    conn.close()


def extract_tasks_with_gemini(text: str) -> list:
    """Ask Gemini to parse free text into structured tasks."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
    prompt = f"""انت مساعد بينظم تاسكات. النهارده {now}.
حلل الرسالة دي واستخرج التاسكات المذكورة فيها. لكل تاسك حدد:
- title: عنوان التاسك (مختصر وواضح)
- due_date: التاريخ والوقت بصيغة "YYYY-MM-DD HH:MM" لو موجود، أو null لو مفيش تاريخ محدد
- priority: "high" أو "medium" أو "low" حسب أهمية التاسك من السياق

رجع JSON array بس، من غير أي شرح أو markdown. مثال:
[{{"title": "اروح البنك", "due_date": "2025-01-15 09:00", "priority": "high"}}]

الرسالة: {text}"""

    response = model.generate_content(prompt)
    raw = response.text.strip()
    # Strip markdown code fences if Gemini adds them
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        tasks = json.loads(raw)
        if isinstance(tasks, dict):
            tasks = [tasks]
        return tasks
    except json.JSONDecodeError:
        logger.error(f"Failed to parse Gemini response: {raw}")
        return []


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.message.chat_id

    await update.message.chat.send_action("typing")

    tasks = extract_tasks_with_gemini(text)

    if not tasks:
        await update.message.reply_text(
            "معرفتش أفهم تاسكات من الرسالة دي 🤔 جرب تكتبها بشكل تاني."
        )
        return

    conn = sqlite3.connect(DB_PATH)
    added = []
    for t in tasks:
        title = t.get("title", "").strip()
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

    if not added:
        await update.message.reply_text("معرفتش أفهم تاسكات من الرسالة دي 🤔")
        return

    priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = ["✅ ضفت التاسكات دي:\n"]
    for title, due_date, priority in added:
        line = f"{priority_emoji.get(priority, '🟡')} {title}"
        if due_date:
            line += f" — {due_date}"
        lines.append(line)

    await update.message.reply_text("\n".join(lines))


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
        "أهلاً! ابعتلي أي حاجة عايز تعملها وأنا هنظمهالك تاسكات.\n\n"
        "مثال: \"عايز اروح البنك بكرة الصبح وابعت الايميل النهاردة\"\n\n"
        "الأوامر:\n"
        "/tasks - شوف التاسكات المفتوحة\n"
        "/done [رقم] - اقفل تاسك"
    )


async def check_reminders(app: Application):
    """Runs periodically; sends reminders for tasks due soon."""
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
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tasks", list_tasks))
    app.add_handler(CommandHandler("done", done_task))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_reminders, "interval", minutes=5, args=[app])
    scheduler.start()

    app.run_polling()


if __name__ == "__main__":
    main()
