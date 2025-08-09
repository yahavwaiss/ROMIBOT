import os
import telebot
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import google.generativeai as genai
import json
from datetime import datetime
import pandas as pd
from flask import Flask, request, abort
import logging
import sys

# --- הגדרת מערכת לוגים מקצועית ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout  # ודא שהלוגים נשלחים לפלט הסטנדרטי
)

# --- הגדרות ותצורה ---
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', 'YOUR_TELEGRAM_BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', 'YOUR_GEMINI_API_KEY')
GOOGLE_SHEET_NAME = os.getenv('GOOGLE_SHEET_NAME', 'Your Google Sheet Name')
ALLOWED_IDS_STR = os.getenv('ALLOWED_CHAT_IDS', '')
ALLOWED_CHAT_IDS = [id.strip() for id in ALLOWED_IDS_STR.split(',')] if ALLOWED_IDS_STR else []

# --- אתחול שירותים ---
# הגדרת ה-API של גוגל ג'מיני
try:
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel('gemini-1.5-flash')
except Exception as e:
    logging.error(f"Fatal Error configuring Gemini API: {e}")

# הגדרת החיבור ל-Google Sheets
try:
    scope = ["https://spreadsheets.google.com/feeds", 'https://www.googleapis.com/auth/spreadsheets',
             "https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("romi-468413-52721fa379b8.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open(GOOGLE_SHEET_NAME).sheet1
except Exception as e:
    logging.error(f"Fatal Error connecting to Google Sheets: {e}")

# אתחול הבוט של טלגרם ואפליקציית Flask
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)
app = Flask(__name__)

# --- פונקציות ליבה ---

def classify_intent(text):
    prompt = f"""
    Is the user's intent in the following sentence to LOG new information or to QUERY existing data?
    Answer with a single word only: `LOG` for logging, or `QUERY` for a question.

    User text: "{text}"
    """
    try:
        response = model.generate_content(prompt)
        return response.text.strip().upper()
    except Exception as e:
        logging.error(f"Could not classify intent, defaulting to LOG. Error: {e}")
        return "LOG"

def analyze_log_text(text):
    prompt = f"""
    You are an assistant for a parenting log. Analyze the following text and extract the information into a JSON format with keys: "category" and "details".
    Possible categories are: 'אוכל', 'שינה', 'התנהגות', 'אחר'.
    For 'details', briefly summarize the action.
    Example 1: Text: "הלכה לישון עכשיו צהריים", JSON: {{"category": "שינה", "details": "הלכה לישון שנת צהריים"}}
    Example 2: Text: "היא סיימה צלחת שלמה של פסטה", JSON: {{"category": "אוכל", "details": "אכלה צלחת פסטה שלמה"}}
    Now, analyze the following text and return only the JSON object: Text: "{text}"
    """
    try:
        response = model.generate_content(prompt)
        cleaned_response = response.text.strip().replace('```json', '').replace('```', '').strip()
        return json.loads(cleaned_response)
    except Exception as e:
        logging.error(f"Error in AI analysis for logging: {e}")
        return {"category": "שגיאת ניתוח", "details": text}

def write_to_sheet(raw_text, ai_data):
    try:
        now = datetime.now()
        row = [
            now.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d'), now.strftime('%H:%M'),
            raw_text, ai_data.get('category', 'לא זוהה'), ai_data.get('details', 'לא זוהה'),
            'Telegram Bot'
        ]
        sheet.append_row(row)
        return True
    except Exception as e:
        logging.error(f"Error writing to sheet: {e}")
        return False

def answer_question_with_context(question):
    try:
        all_data = sheet.get_all_records()
        if not all_data:
            return "מצטער, הגיליון עדיין ריק ואין לי נתונים לענות מהם."
        df = pd.DataFrame(all_data)
        if df.empty:
             return "מצטער, הגיליון עדיין ריק ואין לי נתונים לענות מהם."
        data_context = df.to_csv(index=False)
        prompt = f"""
        You are a helpful assistant analyzing a child's log. Your task is to answer the user's question based ONLY on the data provided in the 'CONTEXT' section.
        If you can provide additional general knowledge that is relevant but NOT in the data, add a separate section at the end of your answer under the clear heading: '**הרחבה (מבוסס על ידע כללי):**'.
        If no expansion is needed, do not add this section.
        CONTEXT (Data from the tracking sheet): --- {data_context} ---
        USER'S QUESTION: {question}
        Answer based on the instructions.
        """
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        logging.error(f"Error answering question: {e}")
        return "אוי, הייתה לי בעיה בניתוח הנתונים כדי לענות על השאלה."

# --- פונקציות המטפלות בפעולות הבוט ---

def handle_log(message):
    user_text = message.text
    bot.reply_to(message, "קיבלתי, מתעד את המידע...")
    ai_result = analyze_log_text(user_text)
    success = write_to_sheet(user_text, ai_result)
    if success:
        category = ai_result.get('category', 'לא ידוע')
        details = ai_result.get('details', '')
        confirmation_message = f"✅ תועד בהצלחה!\n*קטגוריה:* {category}\n*פירוט:* {details}"
        bot.send_message(message.chat.id, confirmation_message, parse_mode='Markdown')
    else:
        bot.send_message(message.chat.id, "❌ אוי. הייתה בעיה בשמירת הנתונים לגיליון.")

def handle_query(message):
    user_text = message.text
    bot.reply_to(message, "זיהיתי שאלה, מחפש תשובה בנתונים... 🧐")
    answer = answer_question_with_context(user_text)
    bot.send_message(message.chat.id, answer, parse_mode='Markdown')

# --- הנתב הראשי של הבוט (Webhook) ---

@app.route(f"/{TELEGRAM_BOT_TOKEN}", methods=['POST'])
def webhook():
    # הוספנו את הלוג הזה כדי לוודא שהפונקציה מופעלת
    logging.info("Webhook received a POST request! Processing...")
    
    if request.headers.get('content-type') == 'application/json':
        json_string = request.get_data().decode('utf-8')
        update = telebot.types.Update.de_json(json_string)
        bot.process_new_updates([update])
        return ''
    else:
        abort(403)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_chat_id = str(message.chat.id)
    if user_chat_id not in ALLOWED_CHAT_IDS:
        logging.info(f"DEBUG: Access denied. Received ID: '{user_chat_id}', Allowed IDs: {ALLOWED_CHAT_IDS}")
        bot.reply_to(message, "🚫 מצטער, אין לך הרשאת גישה לבוט זה.")
        return

    if message.content_type != 'text':
        bot.reply_to(message, "אני יודע לעבוד רק עם הודעות טקסט כרגע.")
        return

    if message.text.lower() == '/start':
        welcome_message = """
        שלום! אני בוט המעקב החכם שלך. 🤓
        פשוט דבר איתי!
        ✍️ *כדי לתעד מידע חדש*, פשוט כתוב לי מה קרה.
        ❓ *כדי לשאול שאלות על המידע*, פשוט שאל אותן.
        אני אבין לבד מה לעשות.
        """
        bot.send_message(message.chat.id, welcome_message, parse_mode='Markdown')
        return

    intent = classify_intent(message.text)
    logging.info(f"Intent for '{message.text}': {intent}")

    if 'QUERY' in intent:
        handle_query(message)
    else:
        handle_log(message)
