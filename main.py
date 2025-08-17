#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
בוט תיעוד רומי - AI-Powered Baby Tracker
גרסת Webhook לשרתים חינמיים (Render/Railway/etc)
"""
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
from collections import defaultdict
import pytz
from dataclasses import dataclass

# ספריות חיצוניות
import gspread
from google.oauth2.service_account import Credentials
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes

# ספריות לשרת Web
from aiohttp import web
import google.generativeai as genai

# הגדרות גלובליות
TIMEZONE = pytz.timezone('Asia/Jerusalem')
DEBUG_MODE = os.getenv('DEBUG_MODE', 'false').lower() == 'true'
PORT = int(os.getenv('PORT', 8080))
WEBHOOK_URL = os.getenv('WEBHOOK_URL', '')

# רמת לוגים
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.DEBUG if DEBUG_MODE else logging.INFO
)
logger = logging.getLogger(__name__)

@dataclass
class ParsedMessage:
    """מבנה נתונים לביאור הודעות AI"""
    category: str
    confidence: float
    item: Optional[str] = None
    qty_value: Optional[float] = None
    qty_unit: Optional[str] = None
    method: Optional[str] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    duration_min: Optional[int] = None
    intensity_1_5: Optional[int] = None
    description: Optional[str] = None
    notes: Optional[str] = None

class RateLimiter:
    """מגביל קצב בקשות למשתמש"""
    def __init__(self, max_requests=10, window_seconds=60):
        self.requests = defaultdict(list)
        self.max_requests = max_requests
        self.window_seconds = window_seconds
    
    def is_allowed(self, user_id: str) -> bool:
        now = time.time()
        user_requests = self.requests[user_id]
        
        # ניקוי בקשות ישנות
        user_requests[:] = [req_time for req_time in user_requests if now - req_time < self.window_seconds]
        
        if len(user_requests) >= self.max_requests:
            return False
            
        user_requests.append(now)
        return True

class ConfigManager:
    """מנהל קונפיגורציה ומשתנים סביבתיים"""

    @staticmethod
    def get_telegram_token() -> str:
        token = os.getenv('TELEGRAM_TOKEN')
        if not token:
            raise ValueError("❌ TELEGRAM_TOKEN חסר במשתנים סביבתיים")
        return token

    @staticmethod
    def get_gemini_key() -> str:
        key = os.getenv('GEMINI_API_KEY')
        if not key:
            raise ValueError("❌ GEMINI_API_KEY חסר במשתנים סביבתיים")
        return key

    @staticmethod
    def get_google_credentials() -> Dict[str, Any]:
        """קורא נתוני גישה לגוגל ממשתנה סביבתי"""
        creds_json = os.getenv('GOOGLE_CREDENTIALS')
        if creds_json:
            return json.loads(creds_json)

        # fallback לקובץ מקומי (לפיתוח)
        if os.path.exists('google_credentials.json'):
            with open('google_credentials.json', 'r') as f:
                return json.load(f)

        raise ValueError("❌ נתוני הגישה לגוגל חסרים")

    @staticmethod
    def get_sheet_id() -> str:
        sheet_id = os.getenv('GOOGLE_SHEET_ID')
        if not sheet_id:
            raise ValueError("❌ GOOGLE_SHEET_ID חסר במשתנים סביבתיים")
        return sheet_id

class GoogleSheetsManager:
    """מנהל התחברות וכתיבה לגוגל שיטס"""

    def __init__(self):
        self.creds_dict = ConfigManager.get_google_credentials()
        self.sheet_id = ConfigManager.get_sheet_id()
        self._client = None
        self._spreadsheet = None

    @property
    def client(self):
        if not self._client:
            scope = [
                'https://spreadsheets.google.com/feeds',
                'https://www.googleapis.com/auth/drive'
            ]
            creds = Credentials.from_service_account_info(self.creds_dict, scopes=scope)
            self._client = gspread.authorize(creds)
        return self._client

    @property
    def spreadsheet(self):
        if not self._spreadsheet:
            self._spreadsheet = self.client.open_by_key(self.sheet_id)
        return self._spreadsheet

    def ensure_worksheet(self, name: str, headers: List[str]) -> gspread.Worksheet:
        """מוודא שהגיליון קיים ויוצר אותו במידת הצורך"""
        try:
            worksheet = self.spreadsheet.worksheet(name)
            # בדיקה שהכותרות קיימות
            if worksheet.row_count == 0 or not worksheet.row_values(1):
                worksheet.insert_row(headers, 1)
        except gspread.WorksheetNotFound:
            worksheet = self.spreadsheet.add_worksheet(title=name, rows=1000, cols=len(headers))
            worksheet.insert_row(headers, 1)
        return worksheet

    def is_authorized_user(self, chat_id: str) -> tuple[bool, str]:
        """בודק אם המשתמש מורשה ומחזיר שם תצוגה"""
        try:
            users_ws = self.spreadsheet.worksheet('Users')
            users_data = users_ws.get_all_records()

            for user in users_data:
                if str(user.get('chat_id', '')).strip() == str(chat_id).strip():
                    display_name = user.get('display_name', f'משתמש{chat_id[-4:]}')
                    return True, display_name

            return False, ""
        except Exception as e:
            logger.error(f"שגיאה בבדיקת הרשאות: {e}")
            return False, ""

    def get_admin_chat_ids(self) -> List[str]:
        """מחזיר רשימת chat_id של מנהלים"""
        try:
            users_ws = self.spreadsheet.worksheet('Users')
            users_data = users_ws.get_all_records()

            admins = []
            for user in users_data:
                if str(user.get('is_admin', '')).lower() in ['true', '1', 'yes']:
                    chat_id = str(user.get('chat_id', '')).strip()
                    if chat_id.isdigit():
                        admins.append(chat_id)

            return admins
        except Exception as e:
            logger.error(f"שגיאה בהבאת מנהלים: {e}")
            return []

    def save_food(self, user_name: str, parsed: ParsedMessage, original_text: str, chat_id: str):
        """שומר נתוני אוכל"""
        headers = ['timestamp', 'user', 'category', 'item', 'qty_value', 'qty_unit', 'method', 'source', 'notes']
        ws = self.ensure_worksheet('Food', headers)

        now = datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M')
        category = 'solid' if 'מוצק' in (parsed.method or '') else 'liquid'
        source = parsed.method if parsed.method in ['solids', 'bottle', 'breast'] else ''

        row = [
            now, user_name, category, parsed.item or '',
            parsed.qty_value or '', parsed.qty_unit or '', 
            parsed.method or '', source, parsed.notes or original_text
        ]
        ws.append_row(row)

    def save_sleep(self, user_name: str, parsed: ParsedMessage, original_text: str, chat_id: str):
        """שומר נתוני שינה"""
        headers = ['timestamp', 'user', 'start', 'end', 'duration_min', 'kind', 'notes']
        ws = self.ensure_worksheet('Sleep', headers)

        now = datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M')

        # חישוב משך זמן אם יש שעות
        duration = None
        if parsed.start_time and parsed.end_time:
            try:
                start = datetime.strptime(parsed.start_time, '%H:%M')
                end = datetime.strptime(parsed.end_time, '%H:%M')
                if end < start:  # עבר לחצות
                    end += timedelta(days=1)
                duration = int((end - start).total_seconds() / 60)
            except:
                pass

        kind = 'נמנום' if duration and duration < 120 else 'שינה'

        row = [
            now, user_name, parsed.start_time or '',
            parsed.end_time or '',
            duration or '', kind, parsed.notes or original_text
        ]
        ws.append_row(row)

    def save_behavior(self, user_name: str, parsed: ParsedMessage, original_text: str, chat_id: str):
        """שומר נתוני התנהגות/בכי"""
        headers = ['timestamp', 'user', 'category', 'intensity_1_5', 'description']
        ws = self.ensure_worksheet('Behavior', headers)

        now = datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M')
        category_map = {'cry': 'בכי', 'behavior': 'התנהגות', 'other': 'אחר'}
        category_he = category_map.get(parsed.category, 'אחר')

        row = [
            now, user_name, category_he,
            parsed.intensity_1_5 or '',
            parsed.description or original_text
        ]
        ws.append_row(row)

    def save_qa_log(self, user_name: str, question: str, answer: str, backed_by_data: bool = False):
        """שומר שאלות ותשובות"""
        headers = ['timestamp', 'user', 'question', 'answer', 'backed_by_data']
        ws = self.ensure_worksheet('Q&A_Log', headers)

        now = datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M')
        row = [now, user_name, question, answer, 'TRUE' if backed_by_data else 'FALSE']
        ws.append_row(row)

class AIProcessor:
    """מעבד AI לפענוח הודעות באמצעות Gemini"""

    def __init__(self):
        genai.configure(api_key=ConfigManager.get_gemini_key())
        self.model = genai.GenerativeModel('gemini-1.5-flash')
        self.timeout = 30

    def _create_fallback_response(self, text: str) -> ParsedMessage:
        """יוצר תגובת fallback כשה-AI נכשל"""
        return ParsedMessage(
            category='other',
            confidence=0.3,
            description=text,
            notes=text
        )

    def parse_message(self, text: str) -> ParsedMessage:
        """מפענח הודעת משתמש ומחזיר ParsedMessage"""

        prompt = f"""
אתה מומחה לתיעוד תינוקות. נתח את ההודעה הבאה והחזר JSON בפורמט הבא בלבד:
{{
    "category": "food|sleep|cry|behavior|question|other",
    "confidence": 0.0-1.0,
    "item": "שם הפריט או null",
    "qty_value": מספר או null,
    "qty_unit": "ml|teaspoon|tablespoon|grams|minutes|hours|count או null",
    "method": "bottle|breast|solids|spoon או null",
    "start_time": "HH:MM או null",
    "end_time": "HH:MM או null", 
    "duration_min": מספר דקות או null,
    "intensity_1_5": 1-5 או null,
    "description": "תיאור או null",
    "notes": "הערות או null"
}}

כללים:
- food: אוכל, שתייה, בקבוק, תמ"ל, בננה, מרק וכו'
- sleep: שינה, נמנום, זמני שינה
- cry: בכי, צעקות
- behavior: התנהגות, מצב רוח, פעילות
- question: שאלות שמתחילות ב"איך", "מה", "מתי" וכו'
- other: כל דבר אחר

אם יש טווח זמן (כמו 13:10-14:30), חלץ start_time ו end_time.
confidence גבוה (0.8+) רק אם אתה בטוח.

הודעה לניתוח: "{text}"

החזר רק JSON תקין ללא טקסט נוסף.
"""

        # ניסיון עם retry mechanism
        for attempt in range(3):
            try:
                response = self.model.generate_content(
                    prompt,
                    generation_config=genai.types.GenerationConfig(
                        temperature=0.1,
                        top_p=0.8,
                        max_output_tokens=1024
                    )
                )
                result_text = response.text.strip()

                # ניקוי טקסט - חיפוש JSON בתוך התגובה
                json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
                if json_match:
                    result_text = json_match.group()

                data = json.loads(result_text)

                # וולידציה בסיסית
                if data.get('category') not in ['food', 'sleep', 'cry', 'behavior', 'question', 'other']:
                    data['category'] = 'other'

                if not isinstance(data.get('confidence'), (int, float)) or not 0 <= data['confidence'] <= 1:
                    data['confidence'] = 0.5

                return ParsedMessage(**{k: v for k, v in data.items() if hasattr(ParsedMessage, k)})

            except Exception as e:
                if attempt == 2:  # ניסיון אחרון
                    logger.error(f"שגיאה בעיבוד AI אחרי 3 ניסיונות: {e}")
                    return self._create_fallback_response(text)
                time.sleep(1)  # המתנה קצרה בין ניסיונות

        return self._create_fallback_response(text)

class RomiBot:
    """הבוט הראשי - גרסת Webhook"""

    def __init__(self):
        self.sheets = GoogleSheetsManager()
        self.ai = AIProcessor()
        self.token = ConfigManager.get_telegram_token()
        self.rate_limiter = RateLimiter()

        # יצירת אפליקציה עם Webhook
        self.app = Application.builder().token(self.token).build()

        # רישום handlers
        self.app.add_handler(CommandHandler("start", self.cmd_start))
        self.app.add_handler(CommandHandler("help", self.cmd_help))
        self.app.add_handler(CommandHandler("today", self.cmd_today))
        self.app.add_handler(CommandHandler("week", self.cmd_week))
        self.app.add_handler(CommandHandler("export", self.cmd_export))
        self.app.add_handler(CommandHandler("testai", self.cmd_test_ai))
        self.app.add_handler(CallbackQueryHandler(self.handle_callback))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))

        # שרת Web
        self.web_app = web.Application()
        self.setup_routes()

    def setup_routes(self):
        """הגדרת נתיבי ה-Web server"""
        self.web_app.router.add_get('/health', self.health_check)
        self.web_app.router.add_get('/', self.home_page)

        # נתיב ל-Webhook של טלגרם
        webhook_path = f'/telegram-webhook/{self.token}'
        self.web_app.router.add_post(webhook_path, self.handle_webhook)

        logger.info(f"Webhook path configured: {webhook_path}")

    async def health_check(self, request):
        """בדיקת בריאות השרת"""
        return web.json_response({
            'status': 'healthy',
            'bot': 'RomiBot',
            'timestamp': datetime.now(TIMEZONE).isoformat()
        })

    async def home_page(self, request):
        """דף בית פשוט"""
        html = """
<!DOCTYPE html>
<html dir="rtl" lang="he">
<head>
    <meta charset="UTF-8">
    <title>בוט תיעוד רומי</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
        }
        .container {
            text-align: center;
            padding: 2rem;
            background: rgba(255,255,255,0.1);
            border-radius: 20px;
        }
        h1 { font-size: 3rem; margin-bottom: 1rem; }
        p { font-size: 1.2rem; }
        .status { 
            background: #4CAF50;
            display: inline-block;
            padding: 10px 20px;
            border-radius: 20px;
            margin-top: 1rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🍼 בוט תיעוד רומי</h1>
        <p>תיעוד חכם לתינוקות באמצעות AI</p>
        <div class="status">✅ השרת פעיל</div>
    </div>
</body>
</html>
"""
        return web.Response(text=html, content_type='text/html')

    async def handle_webhook(self, request):
        """טיפול בעדכונים מטלגרם דרך Webhook"""
        try:
            data = await request.json()
            update = Update.de_json(data, self.app.bot)

            # עיבוד אסינכרוני של העדכון
            asyncio.create_task(self.app.process_update(update))

            return web.Response(status=200)
        except Exception as e:
            logger.error(f"Error processing webhook: {e}")
            return web.Response(status=500)

    async def setup_webhook(self):
        """הגדרת Webhook בטלגרם"""
        if not WEBHOOK_URL:
            logger.warning("WEBHOOK_URL not set, skipping webhook setup")
            return

        webhook_url = f"{WEBHOOK_URL}/telegram-webhook/{self.token}"

        try:
            # מחיקת webhook ישן
            await self.app.bot.delete_webhook(drop_pending_updates=True)

            # הגדרת webhook חדש
            success = await self.app.bot.set_webhook(
                url=webhook_url,
                allowed_updates=["message", "callback_query"]
            )

            if success:
                logger.info(f"✅ Webhook set successfully: {webhook_url}")
            else:
                logger.error("❌ Failed to set webhook")

        except Exception as e:
            logger.error(f"Error setting webhook: {e}")

    async def on_startup(self, app):
        """פעולות בהפעלת השרת"""
        await self.app.initialize()
        await self.setup_webhook()
        logger.info("🚀 Bot initialized and webhook configured")

    async def on_cleanup(self, app):
        """פעולות בכיבוי השרת"""
        await self.app.shutdown()
        logger.info("👋 Bot shutdown complete")

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת התחלה"""
        chat_id = str(update.effective_chat.id)
        is_auth, display_name = self.sheets.is_authorized_user(chat_id)

        if not is_auth:
            await update.message.reply_text(
                f"⛔ אין לך הרשאה להשתמש בבוט.\n"
                f"מספר הצ'אט שלך: `{chat_id}`\n"
                f"בקש מהמנהל להוסיף אותך לגיליון Users.",
                parse_mode='Markdown'
            )
            return

        welcome_text = f"""
👋 שלום {display_name}! ברוך/ה הבא/ה לבוט תיעוד רומי!

🤖 הבוט מתעד באמצעות AI:
🍼 **אוכל ושתייה** - כמות, סוג, שיטה
😴 **שינה** - זמני התחלה וסיום
😢 **בכי** - משך ועוצמה
📝 **התנהגות** - מצב רוח ופעילויות

**דוגמאות לשימוש:**
• אכלה 120 מ"ל תמ"ל מבקבוק
• טעמה בננה כ-3 כפיות
• שינה 13:10-14:30 נמנום יפה
• בכי חזק 10 דקות אחרי רחצה
• שמחה ומחייכת הרבה היום

**פקודות שימושיות:**
/today - סיכום היום
/week - סיכום שבוע
/export - קישור לגיליון
/testai - בדיקת AI

תתחיל לתעד? פשוט כתוב מה קרה! 😊
"""
        await update.message.reply_text(welcome_text, parse_mode='Markdown')

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הדפסת עזרה"""
        await self.cmd_start(update, context)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """סיכום היום"""
        await update.message.reply_text("📅 סיכום היום בפיתוח...")

    async def cmd_week(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """סיכום השבוע"""
        await update.message.reply_text("📈 סיכום שבועי בפיתוח...")

    async def cmd_export(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """קישור לגיליון"""
        sheet_url = f"https://docs.google.com/spreadsheets/d/{self.sheets.sheet_id}"
        await update.message.reply_text(f"🔗 **קישור לגיליון:**\n{sheet_url}", parse_mode='Markdown')

    async def cmd_test_ai(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """בדיקת AI"""
        if not context.args:
            await update.message.reply_text("💡 שימוש: `/testai טקסט לבדיקה`", parse_mode='Markdown')
            return

        test_text = ' '.join(context.args)
        try:
            parsed = self.ai.parse_message(test_text)
            result = f"""
🧠 **תוצאה מ-AI:**
📝 **טקסט מקורי:** {test_text}
🏷️ **קטגוריה:** {parsed.category}
📊 **ביטחון:** {parsed.confidence:.1%}
📦 **פריט:** {parsed.item or 'N/A'}
🔢 **כמות:** {parsed.qty_value or 'N/A'} {parsed.qty_unit or ''}
⚡ **שיטה:** {parsed.method or 'N/A'}
💭 **הערות:** {parsed.notes or 'N/A'}
"""
            await update.message.reply_text(result, parse_mode='Markdown')

        except Exception as e:
            await update.message.reply_text(f"❌ שגיאה: {str(e)}")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """עיבוד הודעות טקסט רגילות"""
        chat_id = str(update.effective_chat.id)
        text = update.message.text.strip()

        # בדיקת rate limiting
        if not self.rate_limiter.is_allowed(chat_id):
            await update.message.reply_text("⏰ יותר מדי בקשות. נסה שוב בעוד דקה.")
            return

        # בדיקת הרשאות
        is_auth, display_name = self.sheets.is_authorized_user(chat_id)
        if not is_auth:
            await update.message.reply_text(
                f"⛔ אין לך הרשאה. Chat ID שלך: `{chat_id}`",
                parse_mode='Markdown'
            )
            return

        try:
            # עיבוד AI
            parsed = self.ai.parse_message(text)

            logger.info(f"User: {display_name}, Text: {text}, Parsed: {parsed.category}, Confidence: {parsed.confidence}")

            # אם הביטחון נמוך, בקש הבהרה
            if parsed.confidence < 0.6:
                await self.ask_for_clarification(update, parsed, text)
                return

            # שמירה לפי קטגוריה
            if parsed.category == 'food':
                await self.save_and_confirm_food(update, parsed, text, display_name, chat_id)
            elif parsed.category == 'sleep':
                await self.save_and_confirm_sleep(update, parsed, text, display_name, chat_id)
            elif parsed.category in ['cry', 'behavior']:
                await self.save_and_confirm_behavior(update, parsed, text, display_name, chat_id)
            elif parsed.category == 'question':
                await self.handle_question(update, parsed, text, display_name)
            else:
                await self.save_and_confirm_behavior(update, parsed, text, display_name, chat_id)

        except Exception as e:
            logger.error(f"שגיאה בעיבוד הודעה: {e}")
            await update.message.reply_text(f"❌ שגיאה בעיבוד: {str(e)}")

            # שליחה למנהלים
            await self.notify_admins(f"שגיאה: {str(e)}\nמשתמש: {display_name}\nטקסט: {text}")

    async def save_and_confirm_food(self, update: Update, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירה ואישור אוכל"""
        self.sheets.save_food(user_name, parsed, text, chat_id)

        qty_text = f" ({parsed.qty_value} {parsed.qty_unit})" if parsed.qty_value else ""
        method_text = f" - {parsed.method}" if parsed.method else ""

        confirmation = f"🍼 **נרשם אוכל:**\n📦 {parsed.item or 'לא זוהה'}{qty_text}{method_text}\n📍 נשמר בגיליון Food"
        await update.message.reply_text(confirmation, parse_mode='Markdown')

    async def save_and_confirm_sleep(self, update: Update, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירה ואישור שינה"""
        self.sheets.save_sleep(user_name, parsed, text, chat_id)

        time_text = ""
        if parsed.start_time and parsed.end_time:
            time_text = f" {parsed.start_time}-{parsed.end_time}"
        elif parsed.duration_min:
            time_text = f" ({parsed.duration_min} דקות)"

        confirmation = f"😴 **נרשמה שינה:**{time_text}\n📍 נשמר בגיליון Sleep"
        await update.message.reply_text(confirmation, parse_mode='Markdown')

    async def save_and_confirm_behavior(self, update: Update, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירה ואישור התנהגות"""
        self.sheets.save_behavior(user_name, parsed, text, chat_id)

        category_names = {'cry': 'בכי', 'behavior': 'התנהגות', 'other': 'אחר'}
        category_name = category_names.get(parsed.category, 'אחר')

        confirmation = f"📝 **נרשם {category_name}**\n📍 נשמר בגיליון Behavior"
        await update.message.reply_text(confirmation, parse_mode='Markdown')

    async def handle_question(self, update: Update, parsed: ParsedMessage, text: str, user_name: str):
        """טיפול בשאלות"""
        answer = "🤖 השאלה שלך נשמרה. בקרוב יהיה כאן מענה חכם על בסיס הנתונים המתועדים!"
        await update.message.reply_text(answer)

        # שמירה בלוג
        self.sheets.save_qa_log(user_name, text, answer, backed_by_data=False)

    async def ask_for_clarification(self, update: Update, parsed: ParsedMessage, text: str):
        """בקש הבהרה אם הביטחון נמוך"""

        keyboard = [
            [
                InlineKeyboardButton("🍼 אוכל", callback_data=f"clarify:food:{text}"),
                InlineKeyboardButton("😴 שינה", callback_data=f"clarify:sleep:{text}")
            ],
            [
                InlineKeyboardButton("😢 בכי", callback_data=f"clarify:cry:{text}"),
                InlineKeyboardButton("📝 התנהגות", callback_data=f"clarify:behavior:{text}")
            ],
            [
                InlineKeyboardButton("❓ שאלה", callback_data=f"clarify:question:{text}"),
                InlineKeyboardButton("🤷 אחר", callback_data=f"clarify:other:{text}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            f"🤔 **לא בטוח מה התכוונת...**\n\n"
            f"📝 כתבת: _{text}_\n"
            f"🎯 AI ניחש: {parsed.category} (ביטחון: {parsed.confidence:.0%})\n\n"
            f"אוכל להבין טוב יותר אם תבחר קטגוריה:",
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול בלחיצות על כפתורים"""
        query = update.callback_query
        await query.answer()

        if query.data.startswith('clarify:'):
            parts = query.data.split(':', 2)
            category = parts[1]
            original_text = parts

            # יצירת ParsedMessage מתוקן
            corrected_parsed = ParsedMessage(
                category=category,
                confidence=1.0,  # המשתמש בחר בעצמו
                description=original_text,
                notes=original_text
            )

            chat_id = str(query.from_user.id)
            is_auth, display_name = self.sheets.is_authorized_user(chat_id)

            if not is_auth:
                await query.edit_message_text("⛔ אין לך הרשאה")
                return

            # שמירה לפי הקטגוריה המתוקנת
            try:
                if category == 'food':
                    await self.save_and_confirm_food_from_callback(query, corrected_parsed, original_text, display_name, chat_id)
                elif category == 'sleep':
                    await self.save_and_confirm_sleep_from_callback(query, corrected_parsed, original_text, display_name, chat_id)
                elif category in ['cry', 'behavior', 'other']:
                    await self.save_and_confirm_behavior_from_callback(query, corrected_parsed, original_text, display_name, chat_id)
                elif category == 'question':
                    await self.handle_question_from_callback(query, corrected_parsed, original_text, display_name)

            except Exception as e:
                logger.error(f"שגיאה בעיבוד callback: {e}")
                await query.edit_message_text(f"❌ שגיאה: {str(e)}")

    async def save_and_confirm_food_from_callback(self, query, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירת אוכל מתוך callback"""
        self.sheets.save_food(user_name, parsed, text, chat_id)
        await query.edit_message_text(f"🍼 **נרשם כאוכל** ✅\n📍 נשמר בגיליון Food", parse_mode='Markdown')

    async def save_and_confirm_sleep_from_callback(self, query, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירת שינה מתוך callback"""
        self.sheets.save_sleep(user_name, parsed, text, chat_id)
        await query.edit_message_text(f"😴 **נרשם כשינה** ✅\n📍 נשמר בגיליון Sleep", parse_mode='Markdown')

    async def save_and_confirm_behavior_from_callback(self, query, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירת התנהגות מתוך callback"""
        self.sheets.save_behavior(user_name, parsed, text, chat_id)
        category_names = {'cry': 'בכי', 'behavior': 'התנהגות', 'other': 'אחר'}
        category_name = category_names.get(parsed.category, 'אחר')
        await query.edit_message_text(f"📝 **נרשם כ{category_name}** ✅\n📍 נשמר בגיליון Behavior", parse_mode='Markdown')

    async def handle_question_from_callback(self, query, parsed: ParsedMessage, text: str, user_name: str):
        """טיפול בשאלה מתוך callback"""
        answer = "🤖 השאלה שלך נשמרה ותענה בקרוב!"
        self.sheets.save_qa_log(user_name, text, answer, backed_by_data=False)
        await query.edit_message_text(f"❓ **נרשם כשאלה** ✅\n📍 נשמר בגיליון Q&A_Log", parse_mode='Markdown')

    async def notify_admins(self, message: str):
        """שליחת התראה למנהלים"""
        try:
            admin_ids = self.sheets.get_admin_chat_ids()
            for admin_id in admin_ids:
                try:
                    await self.app.bot.send_message(
                        chat_id=admin_id,
                        text=f"🚨 **התראת מנהל:**\n{message}",
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logger.error(f"שגיאה בשליחה למנהל {admin_id}: {e}")
        except Exception as e:
            logger.error(f"שגיאה בהתראה למנהלים: {e}")

    def run(self):
        """הפעלת השרת"""
        logger.info("🤖 מפעיל את בוט תיעוד רומי (גרסת Webhook)...")

        # הוספת lifecycle hooks
        self.web_app.on_startup.append(self.on_startup)
        self.web_app.on_cleanup.append(self.on_cleanup)

        # הפעלת השרת
        web.run_app(
            self.web_app,
            host='0.0.0.0',
            port=PORT
        )

# הרצת הבוט
if __name__ == '__main__':
    try:
        bot = RomiBot()
        bot.run()
    except Exception as e:
        logger.error(f"שגיאה קריטית: {e}")
        print(f"❌ שגיאה קריטית: {e}")
        print("ודא שכל משתני הסביבה מוגדרים נכון")
