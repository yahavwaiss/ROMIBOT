import os
import logging
import sys
from flask import Flask, request

# --- הגדרת מערכת לוגים בסיסית ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)

# --- הגדרות ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_TOKEN_HERE')
app = Flask(__name__)

# --- הנתב הראשי של הבוט (Webhook) ---
@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=['POST'])
def webhook():
    # כל מה שהפונקציה עושה זה להדפיס ללוג שהיא קיבלה הודעה
    logging.info("SUCCESS: Webhook received a POST request from Telegram!")
    return "ok", 200

# נקודת כניסה להרצה
if __name__ == "__main__":
    # זה לא ירוץ ב-Render, הם משתמשים ב-gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get('PORT', 5000)))
