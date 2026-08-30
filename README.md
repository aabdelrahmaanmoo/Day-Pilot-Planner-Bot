# Telegram Task Bot (فري بالكامل)

بوت تيليجرام بينظملك تاسكات من كلام عادي، باستخدام Gemini (مجاني).

## 1. جيب المفاتيح

**Telegram Token:**
- كلم `@BotFather` في تيليجرام → `/newbot` → اتبع التعليمات → هيديك token

**Gemini API Key:**
- روح https://aistudio.google.com/app/apikey
- اعمل مفتاح جديد (مجاني، من غير كارت ائتمان)

## 2. جرب محليًا (اختياري)

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN="التوكن بتاعك"
export GEMINI_API_KEY="مفتاح Gemini بتاعك"
python bot.py
```

## 3. انشره على Railway (مجاني، 24/7)

1. روح https://railway.app وسجل دخول بـ GitHub
2. ارفع الفولدر ده على GitHub repo جديد
3. في Railway: New Project → Deploy from GitHub repo → اختار الـ repo
4. من تبويب Variables ضيف:
   - `TELEGRAM_TOKEN`
   - `GEMINI_API_KEY`
5. Railway هيشغل الـ Procfile تلقائيًا

## الأوامر

- ابعت أي رسالة عادية → البوت يستخرج التاسكات ويحفظها
- `/tasks` → يعرض التاسكات المفتوحة
- `/done [رقم]` → يقفل تاسك
- تذكيرات تلقائية قبل موعد التاسك بساعة (بيتشيك كل 5 دقايق)

## ملاحظات

- التخزين: SQLite (ملف `tasks.db` بيتعمل تلقائي)
- على Railway الفري تير، الـ SQLite ممكن يتمسح لو الخدمة اتعاد نشرها — لو عايز تخزين دائم قولي أضيف Railway Volume (لسه فري)
