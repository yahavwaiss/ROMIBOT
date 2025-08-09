import os
import telebot
from flask import Flask, request, abort
import logging
import sys

# --- הגדרת מערכת לוגים ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout)

# --- הגדרות ותצורה ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
ALLOWED_IDS_STR = os.getenv('ALLOWED_CHAT_IDS', '')
# קוד משופר לקריאת המשתנה, שמנקה רווחים
ALLOWED_CHAT_IDS = [id.strip() for id in ALLOWED_IDS_STR.split(',')] if ALLOWED_IDS_STR else []

# --- אתחול ---
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
app = Flask(__name__)

# --- פקודת אבחון מיוחדת ---
@bot.message_handler(commands=['admin_check'])
def admin_check(message):
    user_chat_id = str(message.chat.id)
    
    # בניית הודעת האבחון
    debug_message = (
        f"--- בדיקת הרשאות ---\n\n"
        f"1. ה-Chat ID שהתקבל ממך:\n`{user_chat_id}`\n\n"
        f"2. רשימת המורשים כפי שהיא נקראת מהשרת:\n`{ALLOWED_CHAT_IDS}`\n\n"
        f"--- השוואה ---\n"
    )

    # בדיקה והוספת מסקנה
    if user_chat_id in ALLOWED_CHAT_IDS:
        debug_message += "✅ תוצאה: ההרשאה נמצאה! הבוט אמור לעבוד."
    else:
        debug_message += "❌ תוצאה: ההרשאה לא נמצאה! זו הסיבה שהבוט לא מגיב. בדוק אם יש אי התאמה (כמו רווחים נסתרים) בין שני הערכים."

    bot.reply_to(message, debug_message, parse_mode='Markdown')

# --- Webhook (נשאר זהה) ---
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=['POST'])
def webhook():
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    else:
        abort(403)

# --- הודעת ברירת מחדל לכל הודעה אחרת ---
@bot.message_handler(func=lambda message: True)
def handle_other_messages(message):
    bot.reply_to(message, "מצב אבחון פעיל. אנא השתמש בפקודה /admin_check כדי לבדוק את ההגדרות.")
