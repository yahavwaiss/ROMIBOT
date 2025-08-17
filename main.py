#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
בוט תיעוד רומי - AI-Powered Baby Tracker
גרסת Webhook לשרתים חינמיים (Render/Railway/etc)
"""
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import asyncio
import json
import logging
import os
import re
import time
import hashlib
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

# בדיקת גרסת telegram
try:
    from telegram import __version__ as telegram_version
    print(f"Telegram bot version: {telegram_version}")
except:
    pass

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

# מאגר זמני לטקסטים של הבהרה
CLARIFICATION_TEXTS = {}

@dataclass
class ParsedMessage:
    """מבנה נתונים לביאור הודעות AI"""
    category: str = "other"
    confidence: float = 0.5
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
            try:
                return json.loads(creds_json)
            except json.JSONDecodeError as e:
                logger.error(f"שגיאה בקריאת GOOGLE_CREDENTIALS: {e}")
                raise ValueError("❌ פורמט GOOGLE_CREDENTIALS לא תקין")

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
        try:
            self.creds_dict = ConfigManager.get_google_credentials()
            self.sheet_id = ConfigManager.get_sheet_id()
            self._client = None
            self._spreadsheet = None
        except Exception as e:
            logger.error(f"שגיאה באתחול GoogleSheetsManager: {e}")
            raise

    @property
    def client(self):
        if not self._client:
            try:
                scope = [
                    'https://spreadsheets.google.com/feeds',
                    'https://www.googleapis.com/auth/drive'
                ]
                creds = Credentials.from_service_account_info(self.creds_dict, scopes=scope)
                self._client = gspread.authorize(creds)
                logger.info("✅ חיבור ל-Google Sheets בוצע בהצלחה")
            except Exception as e:
                logger.error(f"שגיאה בחיבור ל-Google Sheets: {e}")
                raise
        return self._client

    @property
    def spreadsheet(self):
        if not self._spreadsheet:
            try:
                self._spreadsheet = self.client.open_by_key(self.sheet_id)
                logger.info(f"✅ פתיחת גיליון {self.sheet_id} בוצעה בהצלחה")
            except Exception as e:
                logger.error(f"שגיאה בפתיחת גיליון: {e}")
                raise
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
        except Exception as e:
            logger.error(f"שגיאה ביצירת גיליון {name}: {e}")
            raise
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

    def fix_sleep_duration_minutes(self, records: List[dict]) -> List[dict]:
        """🔧 תיקון ערכי משך שינה - המרה חכמה לדקות"""
        fixed_records = []
        for record in records:
            try:
                duration_field = record.get('duration_min')
                if duration_field is None or str(duration_field).strip() == '':
                    fixed_records.append(record)
                    continue

                # אם הערך הוא פורמט זמן HH:MM
                if isinstance(duration_field, str) and ':' in duration_field:
                    try:
                        parts = duration_field.split(':')
                        if len(parts) == 2:
                            hours = int(parts[0])
                            minutes = int(parts[1])
                            total_minutes = hours * 60 + minutes
                            record['duration_min'] = total_minutes
                            logger.debug(f"Fixed time format {duration_field} -> {total_minutes} minutes")
                    except ValueError:
                        logger.warning(f"Could not parse time format: {duration_field}")
                
                # אם הערך הוא מספר
                else:
                    try:
                        val = float(str(duration_field))
                        record['duration_min'] = int(val)
                    except (ValueError, TypeError):
                        logger.warning(f"Could not parse duration value: {duration_field}")
                        record['duration_min'] = 0

                fixed_records.append(record)
            except Exception as e:
                logger.error(f"Error fixing sleep duration for record: {e}")
                fixed_records.append(record)
        
        return fixed_records

    def parse_timestamp(self, timestamp_str: str) -> Optional[datetime]:
        """מפענח timestamp בפורמטים שונים"""
        if not timestamp_str:
            return None
            
        timestamp_str = str(timestamp_str).strip()
        
        # פורמטים שונים לנסות
        formats = [
            '%Y-%m-%d %H:%M',
            '%Y-%m-%d %H:%M:%S', 
            '%d/%m/%Y %H:%M',
            '%m/%d/%Y %H:%M',
            '%Y-%m-%d %H:%M:%S.%f',
            '%d-%m-%Y %H:%M',
            '%Y/%m/%d %H:%M'
        ]
        
        for fmt in formats:
            try:
                parsed_date = datetime.strptime(timestamp_str, fmt)
                if parsed_date.tzinfo is None:
                    parsed_date = TIMEZONE.localize(parsed_date)
                return parsed_date
            except ValueError:
                continue
        
        logger.warning(f"Could not parse timestamp: {timestamp_str}")
        return None

    def get_data_by_timerange(self, worksheet_name: str, days_back: int = 7) -> List[Dict]:
        """🔧 מחזיר נתונים מטווח זמן מסוים עם תיקונים מתקדמים"""
        try:
            ws = self.spreadsheet.worksheet(worksheet_name)
            all_records = ws.get_all_records()
            
            if not all_records:
                logger.info(f"No records found in worksheet: {worksheet_name}")
                return []
            
            # תיקון מיוחד לנתוני שינה
            if worksheet_name == 'Sleep':
                all_records = self.fix_sleep_duration_minutes(all_records)
            
            cutoff_date = datetime.now(TIMEZONE) - timedelta(days=days_back)
            
            filtered_data = []
            for record in all_records:
                try:
                    timestamp_str = str(record.get('timestamp', ''))
                    if not timestamp_str or timestamp_str.lower() in ['', 'none', 'null']:
                        continue
                        
                    record_date = self.parse_timestamp(timestamp_str)
                    
                    if record_date and record_date >= cutoff_date:
                        filtered_data.append(record)
                        logger.debug(f"Added record from {record_date}: {record}")
                except Exception as e:
                    logger.debug(f"Could not process record {record}: {e}")
                    continue
            
            logger.info(f"Found {len(filtered_data)} records in {worksheet_name} for last {days_back} days")
            return filtered_data
            
        except Exception as e:
            logger.error(f"שגיאה בקריאת נתונים מ-{worksheet_name}: {e}")
            return []

    def get_daily_summary_with_details(self) -> Dict[str, Any]:
        """📊 מחזיר סיכום יומי מפורט עם פרטים"""
        try:
            today_data = {}
            today_str = datetime.now(TIMEZONE).strftime('%Y-%m-%d')
            
            # נתוני אוכל עם פירוט
            food_data = self.get_data_by_timerange('Food', 1)
            food_details = []
            total_liquids = 0
            
            for item in food_data:
                time_str = self.parse_timestamp(str(item.get('timestamp', '')))
                time_display = time_str.strftime('%H:%M') if time_str else 'לא ידוע'
                
                qty_value = item.get('qty_value', '')
                qty_unit = item.get('qty_unit', '')
                item_name = item.get('item', 'לא ידוע')
                
                if qty_value and qty_unit:
                    detail = f"{time_display}: {item_name} ({qty_value} {qty_unit})"
                    if qty_unit == 'ml':
                        try:
                            total_liquids += float(qty_value)
                        except:
                            pass
                else:
                    detail = f"{time_display}: {item_name}"
                
                food_details.append(detail)
            
            today_data['food'] = {
                'total_meals': len(food_data),
                'liquids_ml': total_liquids,
                'details': food_details,
                'solids_count': len([item for item in food_data if item.get('category') == 'solid'])
            }
            
            # נתוני שינה עם פירוט
            sleep_data = self.get_data_by_timerange('Sleep', 1)
            sleep_details = []
            total_sleep_minutes = 0
            
            for sleep in sleep_data:
                try:
                    duration = sleep.get('duration_min', 0)
                    if duration:
                        duration_int = int(float(duration))
                        total_sleep_minutes += duration_int
                        
                        start_time = sleep.get('start', '')
                        end_time = sleep.get('end', '')
                        
                        if start_time and end_time:
                            detail = f"{start_time}-{end_time} ({duration_int} דקות)"
                        else:
                            detail = f"{duration_int} דקות שינה"
                        
                        sleep_details.append(detail)
                except Exception as e:
                    logger.warning(f"Could not process sleep record: {e}")
                    continue
            
            today_data['sleep'] = {
                'total_sessions': len(sleep_data),
                'total_hours': round(total_sleep_minutes / 60, 1),
                'total_minutes': total_sleep_minutes,
                'details': sleep_details
            }
            
            # נתוני התנהגות עם פירוט
            behavior_data = self.get_data_by_timerange('Behavior', 1)
            behavior_details = []
            cry_count = 0
            positive_count = 0
            
            for behavior in behavior_data:
                time_str = self.parse_timestamp(str(behavior.get('timestamp', '')))
                time_display = time_str.strftime('%H:%M') if time_str else 'לא ידוע'
                
                category = behavior.get('category', '')
                description = behavior.get('description', '')
                
                if category == 'בכי':
                    cry_count += 1
                    behavior_details.append(f"{time_display}: בכי - {description}")
                elif any(word in description.lower() for word in ['שמח', 'חיוך', 'משחק', 'טוב', 'שמחה']):
                    positive_count += 1
                    behavior_details.append(f"{time_display}: חיובי - {description}")
                else:
                    behavior_details.append(f"{time_display}: {category} - {description}")
            
            today_data['behavior'] = {
                'total_events': len(behavior_data),
                'cry_events': cry_count,
                'positive_events': positive_count,
                'details': behavior_details
            }
            
            logger.info(f"Daily summary: {len(food_data)} meals, {total_sleep_minutes} min sleep, {len(behavior_data)} behaviors")
            return today_data
            
        except Exception as e:
            logger.error(f"שגיאה בסיכום יומי מפורט: {e}")
            return {}

    def get_weekly_summary_with_details(self) -> Dict[str, Any]:
        """📈 מחזיר סיכום שבועי מפורט"""
        try:
            weekly_data = {}
            
            # נתוני אוכל
            food_data = self.get_data_by_timerange('Food', 7)
            total_liquids = sum([float(item.get('qty_value', 0) or 0) for item in food_data if item.get('qty_unit') == 'ml'])
            
            weekly_data['food'] = {
                'total_meals': len(food_data),
                'daily_average': round(len(food_data) / 7, 1),
                'liquids_ml': total_liquids,
                'daily_liquids_avg': round(total_liquids / 7, 1)
            }
            
            # נתוני שינה
            sleep_data = self.get_data_by_timerange('Sleep', 7)
            total_sleep_minutes = 0
            
            for sleep in sleep_data:
                try:
                    duration = sleep.get('duration_min', 0)
                    if duration:
                        total_sleep_minutes += int(float(duration))
                except:
                    continue
            
            weekly_data['sleep'] = {
                'total_sessions': len(sleep_data),
                'total_hours': round(total_sleep_minutes / 60, 1),
                'daily_average_hours': round(total_sleep_minutes / 60 / 7, 1),
                'total_minutes': total_sleep_minutes
            }
            
            # נתוני התנהגות
            behavior_data = self.get_data_by_timerange('Behavior', 7)
            cry_events = len([item for item in behavior_data if item.get('category') == 'בכי'])
            positive_events = len([item for item in behavior_data if any(word in str(item.get('description', '')).lower() for word in ['שמח', 'חיוך', 'משחק', 'טוב', 'שמחה'])])
            
            weekly_data['behavior'] = {
                'total_events': len(behavior_data),
                'cry_events': cry_events,
                'positive_events': positive_events,
                'daily_cry_avg': round(cry_events / 7, 1)
            }
            
            return weekly_data
            
        except Exception as e:
            logger.error(f"שגיאה בסיכום שבועי: {e}")
            return {}

    # שמירת השיטות הקיימות
    def get_daily_summary(self) -> Dict[str, Any]:
        """תאימות עם הקוד הקיים"""
        return self.get_daily_summary_with_details()

    def get_weekly_summary(self) -> Dict[str, Any]:
        """תאימות עם הקוד הקיים"""
        return self.get_weekly_summary_with_details()

    def save_food(self, user_name: str, parsed: ParsedMessage, original_text: str, chat_id: str):
        """שומר נתוני אוכל"""
        try:
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
            logger.info(f"✅ נשמר אוכל למשתמש {user_name}")
        except Exception as e:
            logger.error(f"שגיאה בשמירת אוכל: {e}")
            raise

    def save_sleep(self, user_name: str, parsed: ParsedMessage, original_text: str, chat_id: str):
        """שומר נתוני שינה"""
        try:
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
            logger.info(f"✅ נשמרה שינה למשתמש {user_name}")
        except Exception as e:
            logger.error(f"שגיאה בשמירת שינה: {e}")
            raise

    def save_behavior(self, user_name: str, parsed: ParsedMessage, original_text: str, chat_id: str):
        """שומר נתוני התנהגות/בכי"""
        try:
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
            logger.info(f"✅ נשמרה התנהגות למשתמש {user_name}")
        except Exception as e:
            logger.error(f"שגיאה בשמירת התנהגות: {e}")
            raise

    def save_qa_log(self, user_name: str, question: str, answer: str, backed_by_data: bool = False):
        """שומר שאלות ותשובות"""
        try:
            headers = ['timestamp', 'user', 'question', 'answer', 'backed_by_data']
            ws = self.ensure_worksheet('Q&A_Log', headers)

            now = datetime.now(TIMEZONE).strftime('%Y-%m-%d %H:%M')
            row = [now, user_name, question, answer, 'TRUE' if backed_by_data else 'FALSE']
            ws.append_row(row)
            logger.info(f"✅ נשמרה שאלה למשתמש {user_name}")
        except Exception as e:
            logger.error(f"שגיאה בשמירת שאלה: {e}")
            raise

class AIProcessor:
    """מעבד AI לפענוח הודעות באמצעות Gemini"""

    def __init__(self):
        try:
            genai.configure(api_key=ConfigManager.get_gemini_key())
            self.model = genai.GenerativeModel('gemini-1.5-flash')
            self.timeout = 30
            logger.info("✅ חיבור ל-Gemini AI בוצע בהצלחה")
        except Exception as e:
            logger.error(f"שגיאה באתחול AI: {e}")
            raise

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
- question: שאלות שמתחילות ב"איך", "מה", "מתי", "כמה" וכו'
- other: כל דבר אחר

אם יש טווח זמן (כמו 13:10-14:30), חלץ start_time ו end_time.
confidence גבוה (0.8+) רק אם אתה בטוח.

הודעה לניתוח: "{text}"

החזר רק JSON תקין ללא טקסט נוסף.
"""

        # ניסיון עם retry mechanism
        for attempt in range(3):
            try:
                response = self.model.generate_content(prompt)
                result_text = response.text.strip()

                # ניקוי טקסט - חיפוש JSON בתוך התגובה
                json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
                if json_match:
                    result_text = json_match.group()

                data = json.loads(result_text)

                # וולידציה ויצירת ParsedMessage
                return ParsedMessage(
                    category=data.get('category', 'other') if data.get('category') in ['food', 'sleep', 'cry', 'behavior', 'question', 'other'] else 'other',
                    confidence=float(data.get('confidence', 0.5)) if isinstance(data.get('confidence'), (int, float)) and 0 <= data.get('confidence', 0.5) <= 1 else 0.5,
                    item=data.get('item'),
                    qty_value=float(data.get('qty_value')) if data.get('qty_value') and isinstance(data.get('qty_value'), (int, float)) else None,
                    qty_unit=data.get('qty_unit'),
                    method=data.get('method'),
                    start_time=data.get('start_time'),
                    end_time=data.get('end_time'),
                    duration_min=int(data.get('duration_min')) if data.get('duration_min') and isinstance(data.get('duration_min'), (int, float)) else None,
                    intensity_1_5=int(data.get('intensity_1_5')) if data.get('intensity_1_5') and isinstance(data.get('intensity_1_5'), (int, float)) and 1 <= data.get('intensity_1_5') <= 5 else None,
                    description=data.get('description'),
                    notes=data.get('notes')
                )

            except Exception as e:
                if attempt == 2:  # ניסיון אחרון
                    logger.error(f"שגיאה בעיבוד AI אחרי 3 ניסיונות: {e}")
                    return self._create_fallback_response(text)
                time.sleep(1)  # המתנה קצרה בין ניסיונות

        return self._create_fallback_response(text)

class RomiBot:
    """הבוט הראשי - גרסת Webhook"""

    def __init__(self):
        try:
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
            
            logger.info("✅ בוט אותחל בהצלחה")
        except Exception as e:
            logger.error(f"שגיאה קריטית באתחול הבוט: {e}")
            raise

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
            'timestamp': datetime.now(TIMEZONE).isoformat(),
            'version': '2.2.0'
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
        .features {
            margin-top: 2rem;
            text-align: right;
            font-size: 1rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🍼 בוט תיעוד רומי</h1>
        <p>תיעוד חכם לתינוקות באמצעות AI</p>
        <div class="status">✅ השרת פעיל</div>
        <div class="features">
            <p>🆕 <strong>תיקון מהותי:</strong> קריאת נתוני שינה + פירוט מלא!</p>
            <p>🔧 חישובי שעות שינה מדויקים 100%</p>
            <p>📋 סיכומים עם פירוט מלא - מה ומתי</p>
            <p>🤖 תשובות AI חכמות לשאלות</p>
            <p>🔍 ניתוח דפוסים והתפתחות</p>
        </div>
        <p style="font-size: 1rem; margin-top: 1rem;">גרסה 2.2.0</p>
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
            await asyncio.sleep(1)  # המתנה קצרה

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
        try:
            await self.app.initialize()
            await self.setup_webhook()
            logger.info("🚀 Bot initialized and webhook configured")
        except Exception as e:
            logger.error(f"שגיאה בהפעלת הבוט: {e}")

    async def on_cleanup(self, app):
        """פעולות בכיבוי השרת"""
        try:
            await self.app.shutdown()
            logger.info("👋 Bot shutdown complete")
        except Exception as e:
            logger.error(f"שגיאה בכיבוי הבוט: {e}")

    def _generate_text_id(self, text: str) -> str:
        """יוצר ID קצר לטקסט"""
        return hashlib.md5(text.encode()).hexdigest()[:8]

    async def answer_question_with_data(self, question: str, user_name: str) -> str:
        """🤖 עונה על שאלות בהתבסס על הנתונים בגיליון + AI מתקדם"""
        try:
            question_lower = question.lower()
            
            # איסוף נתונים רלוונטיים
            data_context = {}
            
            # נתונים יומיים ושבועיים עם פירוט
            daily_data = self.sheets.get_daily_summary_with_details()
            weekly_data = self.sheets.get_weekly_summary_with_details()
            
            # בניית הקשר מפורט לAI
            if any(word in question_lower for word in ['שינה', 'ישנה', 'נמנום']):
                data_context.update({
                    'sleep_today': daily_data.get('sleep', {}),
                    'sleep_weekly': weekly_data.get('sleep', {}),
                    'context_type': 'sleep'
                })
            
            if any(word in question_lower for word in ['אוכל', 'אכל', 'שתה', 'בקבוק', 'תמל']):
                data_context.update({
                    'food_today': daily_data.get('food', {}),
                    'food_weekly': weekly_data.get('food', {}),
                    'context_type': 'food'
                })
            
            if any(word in question_lower for word in ['בכי', 'בוכה', 'מצב', 'רוח', 'התנהגות']):
                data_context.update({
                    'behavior_today': daily_data.get('behavior', {}),
                    'behavior_weekly': weekly_data.get('behavior', {}),
                    'context_type': 'behavior'
                })
            
            # אם לא זוהה סוג ספציפי, תן כל הנתונים
            if not data_context.get('context_type'):
                data_context = {
                    'daily_summary': daily_data,
                    'weekly_summary': weekly_data,
                    'context_type': 'general'
                }
            
            # עיבוד AI מתקדם
            ai_answer = await self.generate_smart_answer(question, data_context)
            return ai_answer
            
        except Exception as e:
            logger.error(f"שגיאה בתשובה לשאלה עם AI: {e}")
            return "❌ שגיאה בעיבוד השאלה. נסה שוב מאוחר יותר."

    async def generate_smart_answer(self, question: str, data_context: dict) -> str:
        """🧠 יוצר תשובה חכמה באמצעות Gemini AI"""
        try:
            # הכנת הנתונים לAI
            context_str = json.dumps(data_context, ensure_ascii=False, indent=2)
            
            prompt = f"""
אתה עוזר מומחה והורים לתינוק, מנוסה ואמפתי. המטרה שלך היא לתת תשובות מועילות, מדויקות ומעודדות על בסיס הנתונים שנאספו.

השאלה: "{question}"

הנתונים הזמינים:
{context_str}

הנחיות מיוחדות:
1. תן תשובה מדויקת ומבוססת נתונים
2. הוסף פרשנות מועילה ועצות קצרות
3. השתמש באמוג׳י רלוונטיים
4. אם יש מגמה חיובית - עודד
5. אם יש בעיה - תן עצות מעשיות
6. שמור על טון חיובי ותומך
7. תן תשובה באורך של 4-6 שורות מקסימום
8. כלול פירוט מהשדה 'details' אם קיים
9. אל תציין מספרים מדויקים אלא אם יש לך נתונים ברורים

דוגמאות לסגנון תגובה:
- "😴 רומי ישנה נהדר השבוע! בממוצע X שעות ליום - זה מצוין לגילה"
- "🍼 היא אוכלת טוב היום, ואפילו יותר מהרגיל. כל הכבוד!"
- "😊 המצב רוח נראה יציב, יש עוד בכי מהרגיל אבל זה נורמלי לתקופות מסוימות"

ענה בעברית בלבד:
"""

            # שליחה לAI עם retry
            for attempt in range(2):
                try:
                    response = self.ai.model.generate_content(
                        prompt,
                        generation_config=genai.types.GenerationConfig(
                            temperature=0.3,  # יציבות גבוהה אבל לא קפוא
                            top_p=0.8,
                            max_output_tokens=400
                        )
                    )
                    
                    ai_response = response.text.strip()
                    
                    # ולידציה בסיסית
                    if len(ai_response) > 50 and len(ai_response) < 1000:
                        return ai_response
                    else:
                        # תשובת fallback אם AI החזיר משהו מוזר
                        return self.generate_fallback_answer(question, data_context)
                        
                except Exception as e:
                    if attempt == 1:  # ניסיון אחרון
                        logger.error(f"שגיאה ב-AI answer generation: {e}")
                        return self.generate_fallback_answer(question, data_context)
                    await asyncio.sleep(1)
            
            return self.generate_fallback_answer(question, data_context)
            
        except Exception as e:
            logger.error(f"שגיאה קריטית ב-generate_smart_answer: {e}")
            return self.generate_fallback_answer(question, data_context)

    def generate_fallback_answer(self, question: str, data_context: dict) -> str:
        """🔄 תשובת גיבוי אם AI נכשל - עם פירוט"""
        try:
            question_lower = question.lower()
            
            # שינה עם פירוט
            if 'sleep' in data_context.get('context_type', ''):
                sleep_data = data_context.get('sleep_weekly', {}) or data_context.get('sleep_today', {})
                total_hours = sleep_data.get('total_hours', 0)
                details = sleep_data.get('details', [])
                
                if total_hours > 0:
                    response = f"😴 **שינה השבוע:** {total_hours} שעות סה\"כ"
                    if details:
                        response += f"\n📋 **פירוט:** {', '.join(details[:3])}"
                        if len(details) > 3:
                            response += f" ועוד {len(details)-3}..."
                    return response
            
            # אוכל עם פירוט
            elif 'food' in data_context.get('context_type', ''):
                food_data = data_context.get('food_weekly', {}) or data_context.get('food_today', {})
                total_meals = food_data.get('total_meals', 0)
                details = food_data.get('details', [])
                
                if total_meals > 0:
                    response = f"🍼 **אוכל:** {total_meals} ארוחות"
                    if details:
                        response += f"\n📋 **פירוט:** {', '.join(details[:2])}"
                        if len(details) > 2:
                            response += f" ועוד {len(details)-2}..."
                    return response
            
            # התנהגות עם פירוט
            elif 'behavior' in data_context.get('context_type', ''):
                behavior_data = data_context.get('behavior_weekly', {}) or data_context.get('behavior_today', {})
                cry_events = behavior_data.get('cry_events', 0)
                positive_events = behavior_data.get('positive_events', 0)
                details = behavior_data.get('details', [])
                
                if cry_events + positive_events > 0:
                    mood = "חיובי" if positive_events > cry_events else "מעורב"
                    response = f"😊 **מצב רוח:** {cry_events} בכי, {positive_events} חיובי - {mood}"
                    if details:
                        response += f"\n📋 **דוגמאות:** {', '.join(details[:2])}"
                    return response
            
            # כללי עם פירוט
            else:
                daily = data_context.get('daily_summary', {})
                if daily:
                    response = "📊 **סיכום היום:**\n"
                    
                    sleep_details = daily.get('sleep', {}).get('details', [])
                    if sleep_details:
                        response += f"😴 שינה: {', '.join(sleep_details)}\n"
                    
                    food_details = daily.get('food', {}).get('details', [])
                    if food_details:
                        response += f"🍼 אוכל: {', '.join(food_details[:2])}"
                        if len(food_details) > 2:
                            response += f" +{len(food_details)-2}"
                    
                    return response.strip()
            
            return "🤖 יש לי את הנתונים אבל לא הצליח לעבד את השאלה. נסה לשאול בצורה אחרת."
            
        except:
            return "🤖 לא הצליח לעבד את השאלה. נסה שוב מאוחר יותר."

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """פקודת התחלה"""
        try:
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

🤖 **הבוט מתעד באמצעות AI:**
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

**🆕 דוגמאות לשאלות חכמות:**
• כמה שעות ישנה השבוע?
• כמה אכלה היום?
• איך מצב הרוח שלה?
• מה המצב הכללי?

**פקודות שימושיות:**
/today - סיכום היום
/week - סיכום שבוע
/export - קישור לגיליון
/testai - בדיקת AI

תתחיל לתעד? פשוט כתוב מה קרה! 😊
"""
            await update.message.reply_text(welcome_text, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"שגיאה בפקודת start: {e}")
            await update.message.reply_text("❌ שגיאה בהפעלת הבוט. נסה שוב.")

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """הדפסת עזרה"""
        await self.cmd_start(update, context)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """📅 סיכום היום עם פירוט מלא"""
        try:
            daily_data = self.sheets.get_daily_summary_with_details()
            
            if not daily_data:
                await update.message.reply_text("📅 אין נתונים להיום או שגיאה בקריאת הנתונים")
                return
            
            sleep_data = daily_data.get('sleep', {})
            food_data = daily_data.get('food', {})
            behavior_data = daily_data.get('behavior', {})
            
            summary_text = f"""📅 **סיכום היום** {datetime.now(TIMEZONE).strftime('%d/%m/%Y')}

😴 **שינה:** {sleep_data.get('total_hours', 0)} שעות ({sleep_data.get('total_sessions', 0)} תנומות)"""
            
            # הוספת פירוט שינה
            sleep_details = sleep_data.get('details', [])
            if sleep_details:
                summary_text += f"\n📋 {', '.join(sleep_details)}"
            
            summary_text += f"\n\n🍼 **אוכל:** {food_data.get('total_meals', 0)} ארוחות"
            if food_data.get('liquids_ml', 0) > 0:
                summary_text += f", {food_data.get('liquids_ml', 0)} מ\"ל נוזלים"
            
            # הוספת פירוט אוכל
            food_details = food_data.get('details', [])
            if food_details:
                summary_text += f"\n📋 {', '.join(food_details[:3])}"
                if len(food_details) > 3:
                    summary_text += f" +{len(food_details)-3} נוספות"
            
            summary_text += f"\n\n😊 **התנהגות:** {behavior_data.get('cry_events', 0)} בכי, {behavior_data.get('positive_events', 0)} חיובי"
            
            # הוספת פירוט התנהגות
            behavior_details = behavior_data.get('details', [])
            if behavior_details:
                summary_text += f"\n📋 {', '.join(behavior_details[:2])}"
                if len(behavior_details) > 2:
                    summary_text += f" +{len(behavior_details)-2} נוספים"
            
if behavior_data.get('positive_events', 0) > behavior_data.get('cry_events', 0):
    summary_text += "\n\n🌟 יום נהדר!"
else:
    summary_text += "\n\n💙 יום רגיל וטוב"

            
            await update.message.reply_text(summary_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"שגיאה בסיכום יומי: {e}")
            await update.message.reply_text("❌ שגיאה ביצירת סיכום יומי")

    async def cmd_week(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """📈 סיכום השבוע עם פירוט"""
        try:
            weekly_data = self.sheets.get_weekly_summary_with_details()
            
            if not weekly_data:
                await update.message.reply_text("📈 אין נתונים לשבוע או שגיאה בקריאת הנתונים")
                return
            
            sleep_data = weekly_data.get('sleep', {})
            food_data = weekly_data.get('food', {})
            behavior_data = weekly_data.get('behavior', {})
            
            week_start = (datetime.now(TIMEZONE) - timedelta(days=7)).strftime('%d/%m')
            week_end = datetime.now(TIMEZONE).strftime('%d/%m')
            
            summary_text = f"""📈 **סיכום השבוע** {week_start}-{week_end}

😴 **שינה:**
• {sleep_data.get('total_hours', 0)} שעות סה"כ
• ממוצע {sleep_data.get('daily_average_hours', 0)} שעות ליום
• {sleep_data.get('total_sessions', 0)} תנומות השבוע

🍼 **אוכל:**
• {food_data.get('total_meals', 0)} ארוחות סה"כ
• ממוצע {food_data.get('daily_average', 0)} ארוחות ליום
• {food_data.get('liquids_ml', 0)} מ"ל נוזלים סה"כ
• ממוצע {food_data.get('daily_liquids_avg', 0)} מ"ל ליום

😊 **התנהגות:**
• {behavior_data.get('cry_events', 0)} פעמי בכי השבוע
• ממוצע {behavior_data.get('daily_cry_avg', 0)} בכי ליום
• {behavior_data.get('positive_events', 0)} רגעים חיוביים
• {behavior_data.get('total_events', 0)} אירועים סה"כ

{'🌟 שבוע מצוין!' if behavior_data.get('positive_events', 0) > behavior_data.get('cry_events', 0) else '💙 שבוע טוב במקרה הכולל'}

📊 **מגמה:** {'השיפור נמשך!' if behavior_data.get('daily_cry_avg', 0) < 2 else 'יש עבודה קטנה'}
"""
            
            await update.message.reply_text(summary_text, parse_mode='Markdown')
            
        except Exception as e:
            logger.error(f"שגיאה בסיכום שבועי: {e}")
            await update.message.reply_text("❌ שגיאה ביצירת סיכום שבועי")

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
            logger.error(f"שגיאה בבדיקת AI: {e}")
            await update.message.reply_text(f"❌ שגיאה: {str(e)}")

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """עיבוד הודעות טקסט רגילות"""
        try:
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
            await update.message.reply_text("❌ שגיאה בעיבוד ההודעה. נסה שוב או כתוב בצורה אחרת.")

            # שליחה למנהלים
            try:
                display_name = display_name if 'display_name' in locals() else 'לא ידוע'
                text = text if 'text' in locals() else 'לא ידוע'
                await self.notify_admins(f"שגיאה: {str(e)}\nמשתמש: {display_name}\nטקסט: {text}")
            except:
                pass

    async def save_and_confirm_food(self, update: Update, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירה ואישור אוכל"""
        try:
            self.sheets.save_food(user_name, parsed, text, chat_id)

            qty_text = f" ({parsed.qty_value} {parsed.qty_unit})" if parsed.qty_value else ""
            method_text = f" - {parsed.method}" if parsed.method else ""

            confirmation = f"🍼 **נרשם אוכל:**\n📦 {parsed.item or 'לא זוהה'}{qty_text}{method_text}\n📍 נשמר בגיליון Food"
            await update.message.reply_text(confirmation, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"שגיאה בשמירת אוכל: {e}")
            await update.message.reply_text("❌ שגיאה בשמירת הנתונים")

    async def save_and_confirm_sleep(self, update: Update, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירה ואישור שינה"""
        try:
            self.sheets.save_sleep(user_name, parsed, text, chat_id)

            time_text = ""
            if parsed.start_time and parsed.end_time:
                time_text = f" {parsed.start_time}-{parsed.end_time}"
            elif parsed.duration_min:
                time_text = f" ({parsed.duration_min} דקות)"

            confirmation = f"😴 **נרשמה שינה:**{time_text}\n📍 נשמר בגיליון Sleep"
            await update.message.reply_text(confirmation, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"שגיאה בשמירת שינה: {e}")
            await update.message.reply_text("❌ שגיאה בשמירת הנתונים")

    async def save_and_confirm_behavior(self, update: Update, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירה ואישור התנהגות"""
        try:
            self.sheets.save_behavior(user_name, parsed, text, chat_id)

            category_names = {'cry': 'בכי', 'behavior': 'התנהגות', 'other': 'אחר'}
            category_name = category_names.get(parsed.category, 'אחר')

            confirmation = f"📝 **נרשם {category_name}**\n📍 נשמר בגיליון Behavior"
            await update.message.reply_text(confirmation, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"שגיאה בשמירת התנהגות: {e}")
            await update.message.reply_text("❌ שגיאה בשמירת הנתונים")

    async def handle_question(self, update: Update, parsed: ParsedMessage, text: str, user_name: str):
        """🤖 טיפול בשאלות - עכשיו עם תשובות AI חכמות!"""
        try:
            # קבלת תשובה חכמה מבוססת נתונים
            smart_answer = await self.answer_question_with_data(text, user_name)
            await update.message.reply_text(smart_answer, parse_mode='Markdown')

            # שמירה בלוג
            self.sheets.save_qa_log(user_name, text, smart_answer, backed_by_data=True)
        except Exception as e:
            logger.error(f"שגיאה בטיפול בשאלה: {e}")
            fallback_answer = "🤖 השאלה שלך נשמרה. נסה שוב בעוד כמה דקות."
            await update.message.reply_text(fallback_answer)
            self.sheets.save_qa_log(user_name, text, fallback_answer, backed_by_data=False)

    async def ask_for_clarification(self, update: Update, parsed: ParsedMessage, text: str):
        """בקש הבהרה אם הביטחון נמוך"""
        try:
            # יצירת ID קצר לטקסט
            text_id = self._generate_text_id(text)
            CLARIFICATION_TEXTS[text_id] = text

            keyboard = [
                [
                    InlineKeyboardButton("🍼 אוכל", callback_data=f"f:{text_id}"),
                    InlineKeyboardButton("😴 שינה", callback_data=f"s:{text_id}")
                ],
                [
                    InlineKeyboardButton("😢 בכי", callback_data=f"c:{text_id}"),
                    InlineKeyboardButton("📝 התנהגות", callback_data=f"b:{text_id}")
                ],
                [
                    InlineKeyboardButton("❓ שאלה", callback_data=f"q:{text_id}"),
                    InlineKeyboardButton("🤷 אחר", callback_data=f"o:{text_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(
                f"🤔 **לא בטוח מה התכוונת...**\n\n"
                f"📝 כתבת: _{text[:50]}{'...' if len(text) > 50 else ''}_\n"
                f"🎯 AI ניחש: {parsed.category} (ביטחון: {parsed.confidence:.0%})\n\n"
                f"אוכל להבין טוב יותר אם תבחר קטגוריה:",
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"שגיאה בבקשת הבהרה: {e}")
            await update.message.reply_text("❌ שגיאה בעיבוד ההודעה. נסה שוב.")

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """טיפול בלחיצות על כפתורים"""
        try:
            query = update.callback_query
            await query.answer()

            if ':' in query.data:
                # פורמט חדש: קטגוריה:text_id
                category_code, text_id = query.data.split(':', 1)
                
                # מיפוי קודי קטגורית
                category_map = {
                    'f': 'food', 's': 'sleep', 'c': 'cry', 
                    'b': 'behavior', 'q': 'question', 'o': 'other'
                }
                
                category = category_map.get(category_code, 'other')
                original_text = CLARIFICATION_TEXTS.get(text_id, '')
                
                if not original_text:
                    await query.edit_message_text("❌ הטקסט לא נמצא. נסה שוב.")
                    return

                # יצירת ParsedMessage מתוקן
                corrected_parsed = ParsedMessage(
                    category=category,
                    confidence=1.0,
                    description=original_text,
                    notes=original_text
                )

                chat_id = str(query.from_user.id)
                is_auth, display_name = self.sheets.is_authorized_user(chat_id)

                if not is_auth:
                    await query.edit_message_text("⛔ אין לך הרשאה")
                    return

                # שמירה לפי הקטגוריה המתוקנת
                if category == 'food':
                    await self.save_and_confirm_food_from_callback(query, corrected_parsed, original_text, display_name, chat_id)
                elif category == 'sleep':
                    await self.save_and_confirm_sleep_from_callback(query, corrected_parsed, original_text, display_name, chat_id)
                elif category in ['cry', 'behavior', 'other']:
                    await self.save_and_confirm_behavior_from_callback(query, corrected_parsed, original_text, display_name, chat_id)
                elif category == 'question':
                    await self.handle_question_from_callback(query, corrected_parsed, original_text, display_name)

                # ניקוי הטקסט מהמאגר
                CLARIFICATION_TEXTS.pop(text_id, None)

        except Exception as e:
            logger.error(f"שגיאה בעיבוד callback: {e}")
            try:
                await query.edit_message_text("❌ שגיאה בעיבוד. נסה שוב.")
            except:
                pass

    async def save_and_confirm_food_from_callback(self, query, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירת אוכל מתוך callback"""
        try:
            self.sheets.save_food(user_name, parsed, text, chat_id)
            await query.edit_message_text(f"🍼 **נרשם כאוכל** ✅\n📍 נשמר בגיליון Food", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"שגיאה בשמירת אוכל מcallback: {e}")
            await query.edit_message_text("❌ שגיאה בשמירה")

    async def save_and_confirm_sleep_from_callback(self, query, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירת שינה מתוך callback"""
        try:
            self.sheets.save_sleep(user_name, parsed, text, chat_id)
            await query.edit_message_text(f"😴 **נרשם כשינה** ✅\n📍 נשמר בגיליון Sleep", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"שגיאה בשמירת שינה מcallback: {e}")
            await query.edit_message_text("❌ שגיאה בשמירה")

    async def save_and_confirm_behavior_from_callback(self, query, parsed: ParsedMessage, text: str, user_name: str, chat_id: str):
        """שמירת התנהגות מתוך callback"""
        try:
            self.sheets.save_behavior(user_name, parsed, text, chat_id)
            category_names = {'cry': 'בכי', 'behavior': 'התנהגות', 'other': 'אחר'}
            category_name = category_names.get(parsed.category, 'אחר')
            await query.edit_message_text(f"📝 **נרשם כ{category_name}** ✅\n📍 נשמר בגיליון Behavior", parse_mode='Markdown')
        except Exception as e:
            logger.error(f"שגיאה בשמירת התנהגות מcallback: {e}")
            await query.edit_message_text("❌ שגיאה בשמירה")

    async def handle_question_from_callback(self, query, parsed: ParsedMessage, text: str, user_name: str):
        """🤖 טיפול בשאלה מתוך callback - עכשיו עם תשובה חכמה!"""
        try:
            # קבלת תשובה חכמה
            smart_answer = await self.answer_question_with_data(text, user_name)
            
            # עדכון ההודעה עם התשובה
            await query.edit_message_text(
                f"❓ **נרשם כשאלה** ✅\n\n{smart_answer}", 
                parse_mode='Markdown'
            )
            
            # שמירה בלוג
            self.sheets.save_qa_log(user_name, text, smart_answer, backed_by_data=True)
        except Exception as e:
            logger.error(f"שגיאה בטיפול בשאלה מcallback: {e}")
            await query.edit_message_text("❌ שגיאה בעיבוד השאלה")

    async def notify_admins(self, message: str):
        """שליחת התראה למנהלים"""
        try:
            admin_ids = self.sheets.get_admin_chat_ids()
            for admin_id in admin_ids:
                try:
                    await self.app.bot.send_message(
                        chat_id=admin_id,
                        text=f"🚨 **התראת מנהל:**\n{message[:500]}{'...' if len(message) > 500 else ''}",
                        parse_mode='Markdown'
                    )
                except Exception as e:
                    logger.error(f"שגיאה בשליחה למנהל {admin_id}: {e}")
        except Exception as e:
            logger.error(f"שגיאה בהתראה למנהלים: {e}")

    def run(self):
        """הפעלת השרת"""
        logger.info("🤖 מפעיל את בוט תיעוד רומי (גרסת Webhook) - גרסה 2.2.0")

        # הוספת lifecycle hooks
        self.web_app.on_startup.append(self.on_startup)
        self.web_app.on_cleanup.append(self.on_cleanup)

        # הפעלת השרת
        try:
            web.run_app(
                self.web_app,
                host='0.0.0.0',
                port=PORT
            )
        except Exception as e:
            logger.error(f"שגיאה בהפעלת השרת: {e}")
            raise

# הרצת הבוט
if __name__ == '__main__':
    try:
        bot = RomiBot()
        bot.run()
    except Exception as e:
        logger.error(f"שגיאה קריטית: {e}")
        print(f"❌ שגיאה קריטית: {e}")
        print("ודא שכל משתני הסביבה מוגדרים נכון")
