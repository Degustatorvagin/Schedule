import asyncio
import datetime
import io
import json
import logging
import os
import re
import sqlite3
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message,
    BufferedInputFile,
    InputMediaPhoto,
    WebAppInfo
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("BOT_TOKEN", "8918873090:AAFL5x_T3O5yr5swc5GUJKygjUsDqDEdpZQ")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8537137900"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "key")
WEB_DOMAIN = os.getenv("WEB_DOMAIN", "https://degustatorvagin.github.io/Schedule").rstrip("/")
PORT = int(os.getenv("PORT", 3000))

DATA_DIR = os.getenv("DATA_DIR", ".")
if not os.path.exists(DATA_DIR):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
    except Exception:
        DATA_DIR = "."

DB_FILE = os.path.join(DATA_DIR, "users.db")
JSON_FILE = "schedule.json"

DEFAULT_GROUP = "7241452"
ANCHOR_MONDAY = datetime.date(2026, 8, 31)
MSK_TZ = datetime.timezone(datetime.timedelta(hours=3))

DAYS_ORDER = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
DAYS_MAP = {0: 'Понедельник', 1: 'Вторник', 2: 'Среда', 3: 'Четверг', 4: 'Пятница', 5: 'Суббота'}

# --- СЕТКА ЗВОНКОВ ---
BELLS_TABLE = {
    # Высшая школа (ВШЭиП)
    "08:00": ("08:00", "09:30", 1),
    "09:40": ("09:40", "11:10", 2),
    "11:50": ("11:50", "13:20", 3),
    "13:30": ("13:30", "15:00", 4),
    "15:40": ("15:40", "17:10", 5),
    "17:20": ("17:20", "18:50", 6),
    "19:00": ("19:00", "20:30", 7),
    # Колледж (ИЭК)
    "08:30": ("08:30", "10:00", 1),
    "10:20": ("10:20", "11:50", 2),
    "12:30": ("12:30", "14:00", 3),
    "14:20": ("14:20", "15:50", 4),
    "16:00": ("16:00", "17:30", 5),
    "17:40": ("17:40", "19:10", 6),
    "19:20": ("19:20", "20:50", 7)
}

def normalize_type(typ: str) -> str:
    t = typ.lower().strip()
    if 'лек' in t:
        return "Лекция"
    elif 'пр' in t or 'сем' in t:
        return "Практика"
    elif 'лаб' in t:
        return "Лабораторная"
    return typ.capitalize() if typ else ""

def get_slot_info(time_str: str):
    prefix = time_str[:5]
    if prefix in BELLS_TABLE:
        s_str, e_str, slot = BELLS_TABLE[prefix]
        st = datetime.time(int(s_str[:2]), int(s_str[3:]))
        et = datetime.time(int(e_str[:2]), int(e_str[3:]))
        return slot, s_str, e_str, st, et
    return None, time_str, "", None, None

def calculate_break_or_window(prev_end_str, curr_start_str, prev_slot=None, curr_slot=None):
    try:
        p_h, p_m = map(int, prev_end_str.split(':'))
        c_h, c_m = map(int, curr_start_str.split(':'))
        diff_m = (c_h * 60 + c_m) - (p_h * 60 + p_m)
        if diff_m <= 0:
            return ""
        h = diff_m // 60
        m = diff_m % 60
        time_txt = f"{h} ч {m} мин" if (h > 0 and m > 0) else (f"{h} ч" if h > 0 else f"{m} мин")
        if (prev_end_str == "11:50" and curr_start_str == "12:30") or (prev_end_str == "11:10" and curr_start_str == "11:50"):
            return f"\n🥪 <i>Обед {time_txt} ({prev_end_str} – {curr_start_str})</i>\n"
        if diff_m >= 45 or (prev_slot and curr_slot and curr_slot - prev_slot > 1):
            return f"\n🕳 <b>Окно {time_txt}</b> ({prev_end_str} – {curr_start_str})\n"
        else:
            return f"\n☕ <i>Перерыв {time_txt} ({prev_end_str} – {curr_start_str})</i>\n"
    except Exception:
        return ""

# --- ВСТРОЕННАЯ БАЗА ---
BUILTIN_SCHEDULES = {
    "18.2-545": {
        "spec": "Филология (ВШЭиП, УЛК-7)",
        "schedule": {
            "в": {
                "Понедельник": [
                    {"time": "11:50", "subject": "Практический курс английского языка", "building": "УЛК-7", "room": "306", "type": "пр", "teacher": "Хузин И.Р."},
                    {"time": "13:30", "subject": "Практический курс второго иностранного языка", "building": "УЛК-7", "room": "306", "type": "пр", "teacher": "Лядова О.Н."}
                ],
                "Вторник": [
                    {"time": "09:40", "subject": "Элективные курсы по физической культуре", "building": "сп.комп-кс", "room": "сп.зал", "type": "пр", "teacher": "Нихорошкина А.В."},
                    {"time": "11:50", "subject": "Иностранный язык", "building": "УЛК-7", "room": "307", "type": "пр", "teacher": "Петунина А.Р."},
                    {"time": "13:30", "subject": "Практический курс английского языка", "building": "УЛК-7", "room": "302", "type": "пр", "teacher": "Хузин И.Р."}
                ],
                "Среда": [
                    {"time": "08:00", "subject": "Теоретическая и практическая фонетика", "building": "УЛК-7", "room": "204", "type": "пр", "teacher": "Айдарова А.М."},
                    {"time": "09:40", "subject": "Практический курс английского языка", "building": "УЛК-7", "room": "201", "type": "пр", "teacher": "Хузин И.Р."},
                    {"time": "11:50", "subject": "Практический курс второго иностранного языка", "building": "УЛК-7", "room": "202", "type": "пр", "teacher": "Лядова О.Н."}
                ],
                "Четверг": [
                    {"time": "08:00", "subject": "Теоретическая и практическая фонетика", "building": "УЛК-7", "room": "204", "type": "лек", "teacher": "Айдарова А.М."},
                    {"time": "09:40", "subject": "Теоретическая и практическая фонетика", "building": "УЛК-7", "room": "204", "type": "пр", "teacher": "Айдарова А.М."},
                    {"time": "11:50", "subject": "Практический курс английского языка", "building": "УЛК-7", "room": "312", "type": "пр", "teacher": "Хузин И.Р."},
                    {"time": "13:30", "subject": "Русский язык", "building": "УЛК-7", "room": "204", "type": "пр", "teacher": "Родионова Н.Л."}
                ],
                "Пятница": [
                    {"time": "09:40", "subject": "Элективные курсы по физической культуре", "building": "сп.комп-кс", "room": "сп.зал", "type": "пр", "teacher": "Нихорошкина А.В."},
                    {"time": "13:30", "subject": "Практический курс второго иностранного языка", "building": "УЛК-7", "room": "306", "type": "пр", "teacher": "Лядова О.Н."}
                ],
                "Суббота": []
            },
            "н": {
                "Понедельник": [
                    {"time": "11:50", "subject": "Практический курс английского языка", "building": "УЛК-7", "room": "306", "type": "пр", "teacher": "Хузин И.Р."},
                    {"time": "13:30", "subject": "Практический курс второго иностранного языка", "building": "УЛК-7", "room": "306", "type": "пр", "teacher": "Лядова О.Н."}
                ],
                "Вторник": [
                    {"time": "09:40", "subject": "Элективные курсы по физической культуре", "building": "сп.комп-кс", "room": "сп.зал", "type": "пр", "teacher": "Нихорошкина А.В."},
                    {"time": "11:50", "subject": "Иностранный язык", "building": "УЛК-7", "room": "307", "type": "пр", "teacher": "Петунина А.Р."},
                    {"time": "13:30", "subject": "Практический курс английского языка", "building": "УЛК-7", "room": "302", "type": "пр", "teacher": "Хузин И.Р."}
                ],
                "Среда": [
                    {"time": "08:00", "subject": "Теоретическая и практическая фонетика", "building": "УЛК-7", "room": "204", "type": "пр", "teacher": "Айдарова А.М."},
                    {"time": "09:40", "subject": "Практический курс английского языка", "building": "УЛК-7", "room": "201", "type": "пр", "teacher": "Хузин И.Р."},
                    {"time": "11:50", "subject": "Практический курс второго иностранного языка", "building": "УЛК-7", "room": "202", "type": "пр", "teacher": "Лядова О.Н."}
                ],
                "Четверг": [
                    {"time": "08:00", "subject": "Теоретическая и практическая фонетика", "building": "УЛК-7", "room": "204", "type": "лек", "teacher": "Айдарова А.М."},
                    {"time": "09:40", "subject": "Теоретическая и практическая фонетика", "building": "УЛК-7", "room": "204", "type": "пр", "teacher": "Айдарова А.М."},
                    {"time": "11:50", "subject": "Практический курс английского языка", "building": "УЛК-7", "room": "312", "type": "пр", "teacher": "Хузин И.Р."},
                    {"time": "13:30", "subject": "Русский язык", "building": "УЛК-7", "room": "204", "type": "пр", "teacher": "Родионова Н.Л."}
                ],
                "Пятница": [
                    {"time": "09:40", "subject": "Элективные курсы по физической культуре", "building": "сп.комп-кс", "room": "сп.зал", "type": "пр", "teacher": "Нихорошкина А.В."},
                    {"time": "13:30", "subject": "Практический курс второго иностранного языка", "building": "УЛК-7", "room": "306", "type": "пр", "teacher": "Лядова О.Н."}
                ],
                "Суббота": []
            }
        }
    },
    "7241452": {
        "spec": "ИСиП (Колледж)",
        "schedule": {
            "в": {
                "Понедельник": [
                    {"time": "16:00", "subject": "Численные Методы", "building": "УЛК-1", "room": "405", "type": "лек", "teacher": "Рязанова А.Н."}
                ],
                "Вторник": [
                    {"time": "08:30", "subject": "Архитектура аппаратных средств", "building": "УЛК-1", "room": "405", "type": "лек", "teacher": "Волкова А.А."},
                    {"time": "10:20", "subject": "Системное программирование", "building": "УЛК-1", "room": "363", "type": "пр", "teacher": "Сайханов М.А."},
                    {"time": "12:30", "subject": "Системное программирование", "building": "УЛК-1", "room": "314", "type": "пр", "teacher": "Сайханов М.А."}
                ],
                "Среда": [
                    {"time": "08:30", "subject": "Архитектура аппаратных средств", "building": "УЛК-1", "room": "360", "type": "пр", "teacher": "Волкова А.А."},
                    {"time": "10:20", "subject": "Архитектура аппаратных средств", "building": "УЛК-1", "room": "360", "type": "пр", "teacher": "Волкова А.А."},
                    {"time": "12:30", "subject": "Численные Методы", "building": "УЛК-1", "room": "350", "type": "пр", "teacher": "Рязанова А.Н."}
                ],
                "Четверг": [
                    {"time": "08:30", "subject": "Разработка мобильных приложений", "building": "УЛК-1", "room": "314", "type": "пр", "teacher": "Сулейманов А.И."},
                    {"time": "10:20", "subject": "Разработка мобильных приложений", "building": "УЛК-1", "room": "417", "type": "лек", "teacher": "Сулейманов А.И."},
                    {"time": "12:30", "subject": "Физическая культура", "building": "УЛК-6/Спортманеж", "room": "", "type": "пр", "teacher": "Фатыхов И.Ф."}
                ],
                "Пятница": [
                    {"time": "10:20", "subject": "Иностранный язык", "building": "УЛК-1", "room": "109", "type": "пр", "teacher": "Кошенкова А.А."},
                    {"time": "12:30", "subject": "Разработка мобильных приложений", "building": "УЛК-1", "room": "421", "type": "лек", "teacher": "Сулейманов А.И."},
                    {"time": "14:20", "subject": "Системное программирование", "building": "УЛК-1", "room": "417", "type": "лек", "teacher": "Сайханов М.А."},
                    {"time": "16:00", "subject": "Системное программирование", "building": "УЛК-1", "room": "417", "type": "лек", "teacher": "Сайханов М.А."}
                ],
                "Суббота": []
            },
            "н": {
                "Понедельник": [
                    {"time": "14:20", "subject": "Численные Методы", "building": "УЛК-1", "room": "405", "type": "лек", "teacher": "Рязанова А.Н."},
                    {"time": "16:00", "subject": "Численные Методы", "building": "УЛК-1", "room": "405", "type": "лек", "teacher": "Рязанова А.Н."},
                    {"time": "17:40", "subject": "Иностранный язык", "building": "УЛК-1", "room": "109", "type": "пр", "teacher": "Кошенкова А.А."}
                ],
                "Вторник": [
                    {"time": "08:30", "subject": "Архитектура аппаратных средств", "building": "УЛК-1", "room": "405", "type": "лек", "teacher": "Волкова А.А."},
                    {"time": "10:20", "subject": "Архитектура аппаратных средств", "building": "УЛК-1", "room": "417", "type": "лек", "teacher": "Волкова А.А."},
                    {"time": "12:30", "subject": "Системное программирование", "building": "УЛК-1", "room": "314", "type": "пр", "teacher": "Сайханов М.А."}
                ],
                "Среда": [
                    {"time": "08:30", "subject": "Архитектура аппаратных средств", "building": "УЛК-1", "room": "360", "type": "пр", "teacher": "Волкова А.А."},
                    {"time": "10:20", "subject": "Иностранный язык", "building": "УЛК-1", "room": "109", "type": "пр", "teacher": "Кошенкова А.А."},
                    {"time": "12:30", "subject": "Численные Методы", "building": "УЛК-1", "room": "350", "type": "пр", "teacher": "Рязанова А.Н."}
                ],
                "Четверг": [
                    {"time": "08:30", "subject": "Разработка мобильных приложений", "building": "УЛК-1", "room": "417", "type": "лек", "teacher": "Сулейманов А.И."},
                    {"time": "14:20", "subject": "Физическая культура", "building": "УЛК-6/Спортманеж", "room": "", "type": "пр", "teacher": "Фатыхов И.Ф."}
                ],
                "Пятница": [
                    {"time": "10:20", "subject": "Разработка мобильных приложений", "building": "УЛК-1", "room": "316", "type": "пр", "teacher": "Сулейманов А.И."},
                    {"time": "12:30", "subject": "Разработка мобильных приложений", "building": "УЛК-1", "room": "316", "type": "пр", "teacher": "Сулейманов А.И."},
                    {"time": "14:20", "subject": "Численные Методы", "building": "УЛК-1", "room": "363", "type": "пр", "teacher": "Рязанова А.Н."},
                    {"time": "16:00", "subject": "Системное программирование", "building": "УЛК-1", "room": "421", "type": "лек", "teacher": "Сайханов М.А."}
                ],
                "Суббота": [
                    {"time": "10:20", "subject": "Физическая культура", "building": "Спорткомплекс", "room": "", "type": "пр", "teacher": "Фатыхов И.Ф."}
                ]
            }
        }
    }
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class Form(StatesGroup):
    waiting_for_group = State()

def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    group_name TEXT DEFAULT '7241452',
                    notify_morning INTEGER DEFAULT 1,
                    notify_remind INTEGER DEFAULT 1,
                    notify_hw INTEGER DEFAULT 1,
                    view_type TEXT DEFAULT 'text',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            for col, col_type, default_val in [
                ("notify_morning", "INTEGER", "1"),
                ("notify_remind", "INTEGER", "1"),
                ("notify_hw", "INTEGER", "1"),
                ("view_type", "TEXT", "'text'")
            ]:
                try:
                    cursor.execute(f"ALTER TABLE users ADD COLUMN {col} {col_type} DEFAULT {default_val}")
                    conn.commit()
                except Exception:
                    pass
    except Exception as e:
        logging.error(f"Ошибка БД: {e}")

def get_user(user_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, group_name, notify_morning, notify_remind, notify_hw, view_type FROM users WHERE user_id = ?", (user_id,))
            return cursor.fetchone()
    except Exception:
        return None

def register_user(user_id: int, username: str, group_name: str = DEFAULT_GROUP):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO users (user_id, username, group_name, notify_morning, notify_remind, notify_hw, view_type)
                VALUES (?, ?, ?, 
                    COALESCE((SELECT notify_morning FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT notify_remind FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT notify_hw FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT view_type FROM users WHERE user_id = ?), 'text')
                )
            """, (user_id, username, group_name, user_id, user_id, user_id, user_id))
            conn.commit()
    except Exception as e:
        logging.error(f"Ошибка регистрации: {e}")

def update_user_field(user_id: int, field: str, value):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
            conn.commit()
    except Exception as e:
        logging.error(f"Ошибка обновления {field}: {e}")

def get_subscribers(field: str = "notify_morning"):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT user_id, group_name FROM users WHERE {field} = 1")
            return cursor.fetchall()
    except Exception:
        return []

def get_stats():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*), sum(notify_morning) FROM users")
            row = cursor.fetchone()
            return (row[0] or 0), (row[1] or 0)
    except Exception:
        return 0, 0

def load_schedule() -> dict:
    db = dict(BUILTIN_SCHEDULES)
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                disk_db = json.load(f)
                db.update(disk_db)
        except Exception:
            pass
    return db

SCHEDULE_DB = load_schedule()

def find_group(query: str):
    clean_q = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', query).lower()
    if not clean_q:
        return None
    for grp in SCHEDULE_DB.keys():
        if clean_q == re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower():
            return grp
    if "545" in query:
        return "18.2-545"
    if "452" in query or "исип" in query.lower():
        return "7241452"
    for grp in SCHEDULE_DB.keys():
        if clean_q in re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower():
            return grp
    return None

def get_group_schedule(group_name: str) -> dict:
    data = SCHEDULE_DB.get(group_name)
    if not data:
        matched = find_group(group_name)
        if matched:
            data = SCHEDULE_DB.get(matched)
    if isinstance(data, dict) and "schedule" in data:
        return data["schedule"]
    return {"в": {d: [] for d in DAYS_ORDER}, "н": {d: [] for d in DAYS_ORDER}}

def get_week_info(target_date: datetime.date = None):
    if target_date is None:
        target_date = datetime.datetime.now(MSK_TZ).date()
    weeks_diff = (target_date - ANCHOR_MONDAY).days // 7
    if weeks_diff % 2 == 0:
        return 'в', 'Верхняя неделя 🔼'
    return 'н', 'Нижняя неделя 🔽'

# --- ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ ТАБЛИЦЫ (КАК НА СКРИНЕ 15) ---
def render_table_png(lessons: list, day_name: str, group_name: str):
    if not HAS_PILLOW:
        return None
    width = 750
    header_h = 46
    row_h = 80
    padding = 14
    total_h = padding * 2 + header_h + max(1, len(lessons)) * row_h
    
    img = Image.new('RGB', (width, total_h), color='#0e1621')
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    
    x0, y0 = padding, padding
    x1, y1 = width - padding, total_h - padding
    draw.rectangle([x0, y0, x1, y1], fill='#17212b', outline='#242f3d', width=2)
    
    cols = [x0, x0 + 130, x0 + 410, x0 + 590, x1]
    draw.rectangle([x0, y0, x1, y0 + header_h], fill='#1c2736', outline='#242f3d', width=1)
    
    headers = ["Пара", "Предмет", "Преподаватель", "Аудитория"]
    for i, h in enumerate(headers):
        draw.text((cols[i] + 12, y0 + 16), h, fill='#7f91a4', font=font)
        if i > 0:
            draw.line([(cols[i], y0), (cols[i], y1)], fill='#242f3d', width=1)
            
    cy = y0 + header_h
    for idx, l in enumerate(lessons):
        draw.line([(x0, cy), (x1, cy)], fill='#242f3d', width=1)
        slot, s_str, e_str, _, _ = get_slot_info(l.get('time', ''))
        slot_str = str(slot or idx + 1)
        t_range = f"{s_str} — {e_str}" if e_str else l.get('time', '')
        
        draw.text((cols[0] + 12, cy + 18), slot_str, fill='#ffffff', font=font)
        draw.text((cols[0] + 12, cy + 38), t_range, fill='#40a7e3', font=font)
        
        subj = l.get('subject', '')
        typ_full = normalize_type(l.get('type', ''))
        draw.text((cols[1] + 12, cy + 18), subj[:35], fill='#ffffff', font=font)
        if typ_full:
            draw.text((cols[1] + 12, cy + 38), f"({typ_full})", fill='#22c55e' if 'Практ' in typ_full else '#38bdf8', font=font)
            
        teach = l.get('teacher', '—')
        draw.text((cols[2] + 12, cy + 28), teach[:22], fill='#cbd5e1', font=font)
        
        bld = l.get('building', '')
        room = l.get('room', '')
        place = f"{bld}, {room}" if room else bld
        draw.text((cols[3] + 12, cy + 28), place[:18], fill='#94a3b8', font=font)
        cy += row_h
        
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

# --- ТЕКСТОВОЕ ОФОРМЛЕНИЕ РАСПИСАНИЯ ---
def format_day_text(day_name: str, wn_code: str, lessons: list, group_name: str = "", date_str: str = "") -> str:
    wn_label = "Верхняя неделя 🔼" if wn_code == 'в' else "Нижняя неделя 🔽"
    header_title = f"📅 <b>{day_name}</b>"
    if date_str:
        header_title += f" ({date_str})"
    header_title += f" — <i>{wn_label}</i>"

    lines = [header_title]
    if group_name:
        lines.append(f"👥 Группа: <code>{group_name}</code>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not lessons:
        lines.append("\n🎉 <b>Пар нет! Можно отдыхать.</b>")
        return "\n".join(lines)

    enriched = []
    for l in lessons:
        slot, s_str, e_str, st, et = get_slot_info(l.get('time', ''))
        enriched.append({**l, "slot": slot, "s_str": s_str, "e_str": e_str, "st": st, "et": et})

    enriched.sort(key=lambda x: x["st"] if x["st"] else datetime.time(0, 0))

    for i, l in enumerate(enriched):
        if i > 0:
            prev = enriched[i-1]
            break_or_window = calculate_break_or_window(prev["e_str"], l["s_str"], prev["slot"], l["slot"])
            if break_or_window:
                lines.append(break_or_window)

        time_range = f"{l['s_str']} – {l['e_str']}" if l.get('e_str') else l.get('time')
        typ_full = normalize_type(l.get('type', ''))
        type_badge = f" | <b>{typ_full}</b>" if typ_full else ""

        sub_emoji = "📘"
        if "практ" in typ_full.lower():
            sub_emoji = "📗"
        elif "физ" in l.get('subject', '').lower():
            sub_emoji = "🏃"

        lines.append(f"\n⏰ <b>{time_range}</b>{type_badge}")
        lines.append(f"{sub_emoji} <b>{l['subject']}</b>")

        place_parts = []
        if l.get('building'):
            place_parts.append(f"<b>{l['building']}</b>")
        if l.get('room'):
            place_parts.append(f"ауд. <b>{l['room']}</b>")
        if place_parts:
            lines.append(f"📍 {', '.join(place_parts)}")
        if l.get('teacher'):
            lines.append(f"👤 <i>{l['teacher']}</i>")
        lines.append("")

    return "\n".join(lines).strip()

def get_now_status(lessons: list, check_dt: datetime.datetime, group_name: str = "") -> str:
    if not lessons:
        return "🎉 <b>Сегодня занятий нет!</b> Можно отдыхать."

    enriched = []
    for l in lessons:
        slot, s_str, e_str, st, et = get_slot_info(l.get('time', ''))
        if st and et:
            enriched.append({**l, "st": st, "et": et, "s_str": s_str, "e_str": e_str, "slot": slot})

    if not enriched:
        return "🎉 <b>Сегодня пар нет!</b>"

    enriched.sort(key=lambda x: x["st"])
    curr_t = check_dt.time()
    first_st = enriched[0]["st"]
    last_et = enriched[-1]["et"]

    if curr_t < first_st:
        diff_m = (first_st.hour * 60 + first_st.minute) - (curr_t.hour * 60 + curr_t.minute)
        f = enriched[0]
        return (f"⏰ <b>Пары ещё не начались</b>\n\n"
                f"⏳ До первой пары осталось: <b>{diff_m} мин</b>\n"
                f"В <b>{f['s_str']}</b> — <b>{f['subject']}</b>\n"
                f"📍 {f.get('building', '')} {f.get('room', '')}")

    if curr_t > last_et:
        return "🎉 <b>Все пары на сегодня завершились!</b> Можно отдыхать."

    for i, l in enumerate(enriched):
        if l["st"] <= curr_t <= l["et"]:
            diff_m = (l["et"].hour * 60 + l["et"].minute) - (curr_t.hour * 60 + curr_t.minute)
            nxt = enriched[i+1] if i + 1 < len(enriched) else None
            nxt_str = f"\n➡️ Следующая в <b>{nxt['s_str']}</b>: {nxt['subject']} ({nxt['room']})" if nxt else "\n🏁 Это последняя пара на сегодня!"
            typ_f = normalize_type(l.get('type', ''))
            return (f"⚡ <b>Сейчас идёт занятие:</b>\n\n"
                    f"⏰ <b>{l['s_str']} – {l['e_str']}</b>\n"
                    f"📘 <b>{l['subject']}</b> ({typ_f})\n"
                    f"📍 {l.get('building', '')} {l.get('room', '')} | 👤 <i>{l.get('teacher', '')}</i>\n\n"
                    f"⏳ До конца пары: <b>{diff_m} мин</b>{nxt_str}")

        if i + 1 < len(enriched):
            nxt = enriched[i+1]
            if l["et"] < curr_t < nxt["st"]:
                diff_m = (nxt["st"].hour * 60 + nxt["st"].minute) - (curr_t.hour * 60 + curr_t.minute)
                is_lunch = (l["e_str"] == "11:50" and nxt["s_str"] == "12:30") or (l["e_str"] == "11:10" and nxt["s_str"] == "11:50")
                break_title = "🥪 <b>Сейчас обеденный перерыв (40 мин)</b>" if is_lunch else f"☕ <b>Сейчас перерыв ({l['e_str']} – {nxt['s_str']})</b>"
                return (f"{break_title}\n\n"
                        f"⏳ До звонка на пару осталось: <b>{diff_m} мин</b>\n"
                        f"➡️️ В <b>{nxt['s_str']}</b>: <b>{nxt['subject']}</b>\n"
                        f"📍 {nxt.get('building', '')} {nxt.get('room', '')}")

    return "ℹ️ Нет информации о текущей паре."

# --- КЛАВИАТУРЫ ---
def main_keyboard(user_group: str = DEFAULT_GROUP, is_admin: bool = False) -> ReplyKeyboardMarkup:
    # Передаем точную группу в WebApp URL
    web_url = f"{WEB_DOMAIN}?group={user_group}"
    top_button = [KeyboardButton(text="⚡ Открыть расписание онлайн", web_app=WebAppInfo(url=web_url), style="success")]
    rows = [
        top_button,
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📅 Завтра")],
        [KeyboardButton(text="🗓 Неделя"), KeyboardButton(text="⏱ Сейчас")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🔍 Сменить группу")]
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🌐 Веб-Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def schedule_inline_keyboard(date_str: str, wn_code: str) -> InlineKeyboardMarkup:
    d = datetime.date.fromisoformat(date_str)
    prev_d = d - datetime.timedelta(days=1)
    next_d = d + datetime.timedelta(days=1)
    
    # Кнопка быстрой смены недели на противоположную прямо под расписанием
    opp_wn = 'в' if wn_code == 'н' else 'н'
    opp_label = "Верхняя 🔼" if opp_wn == 'в' else "Нижняя 🔽"

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️ Вчера", callback_data=f"nav_{prev_d.isoformat()}_{wn_code}"),
            InlineKeyboardButton(text="📅 Сегодня", callback_data="nav_today"),
            InlineKeyboardButton(text="Завтра ▶️", callback_data=f"nav_{next_d.isoformat()}_{wn_code}")
        ],
        [
            InlineKeyboardButton(text=f"🔄 Сменить: {opp_label}", callback_data=f"nav_{date_str}_{opp_wn}"),
            InlineKeyboardButton(text="🗓 Вся неделя", callback_data=f"nav_week_{wn_code}")
        ],
        [
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nav_{date_str}_{wn_code}")
        ]
    ])

def settings_keyboard(user_row, is_admin: bool = False) -> InlineKeyboardMarkup:
    grp = user_row[2] if user_row else DEFAULT_GROUP
    m_val = user_row[3] if user_row else 1
    r_val = user_row[4] if user_row else 1
    hw_val = user_row[5] if user_row else 1
    view_val = user_row[6] if user_row else "text"

    view_txt = "Таблица 📊" if view_val == "table" else "Текст 📝"
    m_txt = "Вкл" if m_val else "Выкл"
    r_txt = "Вкл" if r_val else "Выкл"
    hw_txt = "Вкл" if hw_val else "Выкл"

    kb = [
        [InlineKeyboardButton(text=f"🎨 Вид расписания: {view_txt}", callback_data="toggle_view")],
        [InlineKeyboardButton(text=f"🔔 Утреннее расписание (07:30): {m_txt}", callback_data="toggle_morning")],
        [InlineKeyboardButton(text=f"⏰ Напоминание (1-я пара и после обеда): {r_txt}", callback_data="toggle_remind")],
        [InlineKeyboardButton(text=f"📚 Напоминание о ДЗ (17:00): {hw_txt}", callback_data="toggle_hw")],
        [InlineKeyboardButton(text=f"👥 Группа: {grp}", callback_data="change_group")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="🚀 Тест рассылки (мне)", callback_data="admin_test_push")])
        kb.append([InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- ОТПРАВКА И ОБНОВЛЕНИЕ РАСПИСАНИЯ ---
async def send_or_edit_schedule(target, date_obj: datetime.date, wn_code: str, grp: str, is_callback: bool = False):
    day_name = DAYS_MAP[date_obj.weekday()] if date_obj.weekday() < 6 else 'Понедельник'
    sched = get_group_schedule(grp)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    user_row = get_user(target.from_user.id)
    view_type = user_row[6] if user_row else 'text'
    reply_kb = schedule_inline_keyboard(date_obj.isoformat(), wn_code)
    
    caption_header = f"📅 <b>Расписание на {date_obj.strftime('%d.%m.%Y')}, {day_name}</b>\n👥 <i>Группа: <code>{grp}</code></i>"

    if date_obj.weekday() == 6:
        text = f"📅 <b>Воскресенье</b> ({date_obj.strftime('%d.%m.%Y')})\n👥 Группа: <code>{grp}</code>\n━━━━━━━━━━━━━━━━━━━━\n\n🎉 <b>Выходной день! Пар нет.</b>"
        if is_callback:
            if target.message.photo:
                await target.message.delete()
                await target.message.answer(text, reply_markup=reply_kb)
            else:
                await target.message.edit_text(text, reply_markup=reply_kb)
        else:
            await target.answer(text, reply_markup=reply_kb)
        return

    # РЕЖИМ 1: ТАБЛИЦА (КАРТИНКА)
    if view_type == 'table' and HAS_PILLOW:
        png_buf = render_table_png(lessons, day_name, grp)
        file_input = BufferedInputFile(png_buf.getvalue(), filename="schedule.png")
        if is_callback:
            if target.message.photo:
                await target.message.edit_media(InputMediaPhoto(media=file_input, caption=caption_header), reply_markup=reply_kb)
            else:
                await target.message.delete()
                await target.message.answer_photo(photo=file_input, caption=caption_header, reply_markup=reply_kb, show_caption_above_media=True)
        else:
            await target.answer_photo(photo=file_input, caption=caption_header, reply_markup=reply_kb, show_caption_above_media=True)
    # РЕЖИМ 2: ТЕКСТ
    else:
        text = format_day_text(day_name, wn_code, lessons, grp, date_obj.strftime('%d.%m.%Y'))
        if is_callback:
            if target.message.photo:
                await target.message.delete()
                await target.message.answer(text, reply_markup=reply_kb)
            else:
                await target.message.edit_text(text, reply_markup=reply_kb)
        else:
            await target.answer(text, reply_markup=reply_kb)

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    user = get_user(msg.from_user.id)
    is_adm = (msg.from_user.id == ADMIN_ID)
    if not user:
        await state.set_state(Form.waiting_for_group)
        text = (
            "👋 <b>Добро пожаловать в бот расписания НЧИ КФУ!</b>\n\n"
            "Напиши номер своей группы (например: <code>7241452</code> или <code>18.2-545</code>):"
        )
        await msg.answer(text)
        return

    _, wn_name = get_week_info()
    text = (
        f"👋 С возвращением! Группа: <code>{user[2]}</code>\n"
        f"⚡ Сейчас идет: <b>{wn_name}</b>\n\n"
        f"Используй кнопки внизу экрана:"
    )
    if is_adm:
        text += "\n\n👑 <i>Ты администратор. Доступна кнопка «🌐 Веб-Админка».</i>"
    await msg.answer(text, reply_markup=main_keyboard(user[2], is_admin=is_adm))

@dp.message(Form.waiting_for_group)
async def process_custom_group(msg: Message, state: FSMContext):
    raw_query = msg.text.strip()
    matched = find_group(raw_query)
    final_grp = matched if matched else raw_query
    register_user(msg.from_user.id, msg.from_user.username or "", final_grp)
    await state.clear()
    is_adm = (msg.from_user.id == ADMIN_ID)
    _, wn_name = get_week_info()
    await msg.answer(
        f"✅ Отлично! Установлена группа: <b>{final_grp}</b>\n"
        f"🔔 Утреннее расписание в 07:30: <b>Включено</b>\n"
        f"⚡ Текущая неделя: <b>{wn_name}</b>",
        reply_markup=main_keyboard(final_grp, is_admin=is_adm)
    )

@dp.message(F.text.contains("Сменить группу"))
@dp.message(Command("setgroup"))
async def cmd_change_group(msg: Message, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    await msg.answer("✍️ Напиши номер новой группы (например: <code>7241452</code> или <code>18.2-545</code>):")

@dp.callback_query(F.data == "change_group")
async def cb_change_group(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    await call.message.answer("✍️ Напиши номер новой группы (например: <code>7241452</code> или <code>18.2-545</code>):")
    await call.answer()

@dp.message(F.text == "📅 Сегодня")
@dp.message(Command("today"))
async def cmd_today(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    today = datetime.datetime.now(MSK_TZ).date()
    wn_code, _ = get_week_info(today)
    await send_or_edit_schedule(msg, today, wn_code, grp, is_callback=False)

@dp.message(F.text == "📅 Завтра")
@dp.message(Command("tomorrow"))
async def cmd_tomorrow(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    tomorrow = datetime.datetime.now(MSK_TZ).date() + datetime.timedelta(days=1)
    wn_code, _ = get_week_info(tomorrow)
    await send_or_edit_schedule(msg, tomorrow, wn_code, grp, is_callback=False)

@dp.callback_query(F.data.startswith("nav_"))
async def cb_nav_schedule(call: CallbackQuery):
    data_parts = call.data.split("_")
    user = get_user(call.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP

    if data_parts[1] == "today":
        target_d = datetime.datetime.now(MSK_TZ).date()
        wn_code, _ = get_week_info(target_d)
        await send_or_edit_schedule(call, target_d, wn_code, grp, is_callback=True)
    elif data_parts[1] == "week":
        wn_code = data_parts[2] if len(data_parts) > 2 else get_week_info()[0]
        wn_label = "Верхняя неделя 🔼" if wn_code == 'в' else "Нижняя неделя 🔽"
        sched = get_group_schedule(grp)
        parts = [f"🗓 <b>Расписание на всю неделю ({wn_label})</b>\n👥 Группа: <code>{grp}</code>\n"]
        for day in DAYS_ORDER:
            lessons = sched.get(wn_code, {}).get(day, [])
            if lessons:
                parts.append(format_day_text(day, wn_code, lessons))
        reply_kb = schedule_inline_keyboard(datetime.datetime.now(MSK_TZ).date().isoformat(), wn_code)
        if call.message.photo:
            await call.message.delete()
            await call.message.answer("\n\n".join(parts), reply_markup=reply_kb)
        else:
            await call.message.edit_text("\n\n".join(parts), reply_markup=reply_kb)
    else:
        target_d = datetime.date.fromisoformat(data_parts[1])
        wn_code = data_parts[2] if len(data_parts) > 2 else get_week_info(target_d)[0]
        await send_or_edit_schedule(call, target_d, wn_code, grp, is_callback=True)
    await call.answer()

@dp.message(F.text == "⏱ Сейчас")
@dp.message(Command("now"))
async def cmd_now(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    now_msk = datetime.datetime.now(MSK_TZ)
    if now_msk.weekday() == 6:
        await msg.answer("🎉 Сегодня воскресенье! Пар нет.")
        return

    wn_code, _ = get_week_info(now_msk.date())
    day_name = DAYS_MAP[now_msk.weekday()]
    sched = get_group_schedule(grp)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    res = get_now_status(lessons, now_msk, grp)
    await msg.answer(res)

@dp.message(F.text == "🗓 Неделя")
@dp.message(Command("week"))
async def cmd_week(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    wn_code, wn_name = get_week_info()
    sched = get_group_schedule(grp)
    parts = [f"🗓 <b>Расписание на текущую неделю ({wn_name})</b>\n👥 Группа: <code>{grp}</code>\n"]
    for day in DAYS_ORDER:
        lessons = sched.get(wn_code, {}).get(day, [])
        if lessons:
            parts.append(format_day_text(day, wn_code, lessons))
    await msg.answer("\n\n".join(parts), reply_markup=schedule_inline_keyboard(datetime.datetime.now(MSK_TZ).date().isoformat(), wn_code))

# --- ОБРАБОТЧИКИ НАСТРОЕК (ТУМБЛЕРЫ) ---
@dp.message(F.text.contains("Настройки"))
@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    user = get_user(msg.from_user.id)
    if not user:
        register_user(msg.from_user.id, msg.from_user.username or "", DEFAULT_GROUP)
        user = get_user(msg.from_user.id)

    is_adm = (msg.from_user.id == ADMIN_ID)
    text = (
        "<b>Настройки</b>\n\n"
        "<blockquote>"
        "• 🎨 <b>Вид расписания</b> — переключение между таблицей и простым текстом\n"
        "• 🔔 <b>Утреннее расписание (07:30)</b> — рассылка расписания каждое утро\n"
        "• ⏰ <b>Напоминание за 15 минут</b> — перед первой парой и занятием после обеда\n"
        "• 📚 <b>Напоминание о ДЗ (17:00)</b> — вечерняя сводка заданий на завтра"
        "</blockquote>\n\n"
        "Нажимайте на кнопки для переключения:"
    )
    await msg.answer(text, reply_markup=settings_keyboard(user, is_admin=is_adm))

@dp.callback_query(F.data == "toggle_view")
async def cb_toggle_view(call: CallbackQuery):
    user = get_user(call.from_user.id)
    cur = user[6] if user else "text"
    new_v = "table" if cur == "text" else "text"
    update_user_field(call.from_user.id, "view_type", new_v)
    user = get_user(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=settings_keyboard(user, is_admin=(call.from_user.id == ADMIN_ID)))
    await call.answer()

@dp.callback_query(F.data == "toggle_morning")
async def cb_toggle_morning(call: CallbackQuery):
    user = get_user(call.from_user.id)
    cur = user[3] if user else 1
    new_v = 0 if cur else 1
    update_user_field(call.from_user.id, "notify_morning", new_v)
    user = get_user(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=settings_keyboard(user, is_admin=(call.from_user.id == ADMIN_ID)))
    await call.answer()

@dp.callback_query(F.data == "toggle_remind")
async def cb_toggle_remind(call: CallbackQuery):
    user = get_user(call.from_user.id)
    cur = user[4] if user else 1
    new_v = 0 if cur else 1
    update_user_field(call.from_user.id, "notify_remind", new_v)
    user = get_user(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=settings_keyboard(user, is_admin=(call.from_user.id == ADMIN_ID)))
    await call.answer()

@dp.callback_query(F.data == "toggle_hw")
async def cb_toggle_hw(call: CallbackQuery):
    user = get_user(call.from_user.id)
    cur = user[5] if user else 1
    new_v = 0 if cur else 1
    update_user_field(call.from_user.id, "notify_hw", new_v)
    user = get_user(call.from_user.id)
    await call.message.edit_reply_markup(reply_markup=settings_keyboard(user, is_admin=(call.from_user.id == ADMIN_ID)))
    await call.answer()

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещен", show_alert=True)
        return
    total, active_notify = get_stats()
    await call.answer(f"📊 Пользователей: {total}\n🔔 Подписчиков: {active_notify}\n📚 Всего групп в базе: {len(SCHEDULE_DB)}", show_alert=True)

@dp.callback_query(F.data == "admin_test_push")
async def cb_admin_test_push(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещен", show_alert=True)
        return
    user = get_user(call.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    today = datetime.datetime.now(MSK_TZ).date()
    wn_code, _ = get_week_info(today)
    day_name = DAYS_MAP[today.weekday()] if today.weekday() < 6 else 'Понедельник'
    sched = get_group_schedule(grp)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    demo_text = "☀️ <b>[ТЕСТ РАССЫЛКИ] Расписание на сегодня:</b>\n\n" + format_day_text(day_name, wn_code, lessons, grp, today.strftime('%d.%m.%Y'))
    await call.message.answer(demo_text)
    await call.answer("Тестовое уведомление отправлено!")

@dp.message(F.text == "🌐 Веб-Админка")
@dp.message(Command("web"))
async def cmd_web_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.answer("⛔ Доступ только для администратора.")
        return
    admin_url = f"{WEB_DOMAIN}?admin=true&token={ADMIN_TOKEN}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть в браузере", url=admin_url)],
        [InlineKeyboardButton(text="📱 Открыть как Mini App", web_app=WebAppInfo(url=admin_url))]
    ])
    await msg.answer(f"🛠 <b>Панель управления расписанием:</b>\n\n🔗 <code>{admin_url}</code>", reply_markup=kb)

# --- АВТОМАТИЧЕСКИЕ НАПОМИНАНИЯ И РАССЫЛКИ ---
async def background_scheduler():
    last_morning_date = None
    last_evening_date = None
    sent_reminders = set()

    while True:
        try:
            now_msk = datetime.datetime.now(MSK_TZ)
            today_date = now_msk.date()
            cur_hhmm = now_msk.strftime("%H:%M")

            # 1. Утренняя рассылка в 07:30
            if cur_hhmm == "07:30" and last_morning_date != today_date:
                last_morning_date = today_date
                if today_date.weekday() != 6:
                    wn_code, _ = get_week_info(today_date)
                    day_name = DAYS_MAP[today_date.weekday()]
                    for uid, grp in get_subscribers("notify_morning"):
                        try:
                            sched = get_group_schedule(grp)
                            lessons = sched.get(wn_code, {}).get(day_name, [])
                            msg_text = "☀️ <b>Доброе утро! Расписание на сегодня:</b>\n\n" + format_day_text(day_name, wn_code, lessons, grp, today_date.strftime('%d.%m.%Y'))
                            await bot.send_message(uid, msg_text)
                            await asyncio.sleep(0.05)
                        except Exception:
                            pass

            # 2. Вечерняя сводка ДЗ в 17:00
            if cur_hhmm == "17:00" and last_evening_date != today_date:
                last_evening_date = today_date
                tom_date = today_date + datetime.timedelta(days=1)
                if tom_date.weekday() != 6:
                    wn_code, _ = get_week_info(tom_date)
                    day_name = DAYS_MAP[tom_date.weekday()]
                    for uid, grp in get_subscribers("notify_hw"):
                        try:
                            sched = get_group_schedule(grp)
                            lessons = sched.get(wn_code, {}).get(day_name, [])
                            if lessons:
                                msg_text = "📚 <b>Вечерняя сводка: расписание на завтра:</b>\n\n" + format_day_text(day_name, wn_code, lessons, grp, tom_date.strftime('%d.%m.%Y'))
                                await bot.send_message(uid, msg_text)
                                await asyncio.sleep(0.05)
                        except Exception:
                            pass

            # 3. Напоминания за 15 минут до первой пары и после обеда
            if today_date.weekday() != 6:
                wn_code, _ = get_week_info(today_date)
                day_name = DAYS_MAP[today_date.weekday()]
                
                # Проверяем для каждого подписчика
                for uid, grp in get_subscribers("notify_remind"):
                    sched = get_group_schedule(grp)
                    lessons = sched.get(wn_code, {}).get(day_name, [])
                    if not lessons:
                        continue
                    
                    # Ищем 1-ю пару и пару после обеда
                    targets = []
                    if len(lessons) > 0:
                        targets.append(lessons[0])
                    for idx_l in range(1, len(lessons)):
                        prev_l = lessons[idx_l - 1]
                        cur_l = lessons[idx_l]
                        if ("11:50" in prev_l.get('time', '') and "12:30" in cur_l.get('time', '')) or \
                           ("11:10" in prev_l.get('time', '') and "11:50" in cur_l.get('time', '')):
                            targets.append(cur_l)
                            
                    for t_l in targets:
                        start_str = t_l.get('time', '')[:5]
                        try:
                            h, m = map(int, start_str.split(':'))
                            rem_m = h * 60 + m - 15
                            rem_str = f"{rem_m // 60:02d}:{rem_m % 60:02d}"
                            rem_key = f"{uid}_{today_date}_{start_str}"
                            if cur_hhmm == rem_str and rem_key not in sent_reminders:
                                sent_reminders.add(rem_key)
                                typ_f = normalize_type(t_l.get('type', ''))
                                alert_msg = (
                                    f"⏰ <b>Напоминание: через 15 минут пара!</b>\n\n"
                                    f"В <b>{start_str}</b>: <b>{t_l.get('subject')}</b> ({typ_f})\n"
                                    f"📍 {t_l.get('building')}, ауд. <b>{t_l.get('room')}</b>\n"
                                    f"👤 <i>{t_l.get('teacher')}</i>"
                                )
                                await bot.send_message(uid, alert_msg)
                        except Exception:
                            pass
        except Exception as e:
            logging.error(f"Ошибка в планировщике: {e}")
        await asyncio.sleep(25)

# --- ВЕБ-СЕРВЕР ---
async def handle_index(request):
    return web.Response(text=f"<h1>Расписание НЧИ КФУ онлайн</h1><p>Групп в базе: {len(SCHEDULE_DB)}</p>", content_type='text/html')

def create_web_app():
    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/admin', handle_index)
    return app

async def main():
    init_db()
    asyncio.create_task(background_scheduler())

    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logging.info(f"Веб-сервер запущен на 0.0.0.0:{PORT}")
    except Exception as e:
        logging.warning(f"Порт не поднят: {e}")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
