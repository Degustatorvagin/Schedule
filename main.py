import asyncio
import datetime
import io
import json
import logging
import os
import re
import sqlite3
import urllib.parse
from aiohttp import web, ClientSession
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- КОНФИГУРАЦИЯ ---
WEB_APP_URL = "https://degustatorvagin.github.io/Schedule/"
TOKEN = os.getenv("BOT_TOKEN", "8918873090:AAEVDb3_ExuDvy38GHEEczEurX7Puu0M0Rk")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8537137900"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "KEY")
PORT = int(os.getenv("PORT", 3000))
SUPPORT_USERNAME = "@AvaUtility_support"

AI_SYSTEM_PROMPT = """Ты — учебный ассистент «Совёнок AI» в НЧИ КФУ.
Твоя задача — сжимать длинные лекции и статьи в компактный конспект, который студент успеет переписать в тетрадь за пару.
Правила:
1. Пиши кратко, по делу, без воды и вводных фраз.
2. Выделяй 3-4 главных тезиса и ключевые термины.
3. Форматируй списки БЕЗ использования двойных звездочек (**), используй дефисы и эмодзи (📌, 💡, ⚡).
4. Объем результата должен быть сжатым, легко умещающимся на 2-3 страницы тетради."""

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

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

BELLS_TABLE = {
    "08:00": ("08:00", "09:30", 1),
    "09:40": ("09:40", "11:10", 2),
    "11:50": ("11:50", "13:20", 3),
    "13:30": ("13:30", "15:00", 4),
    "15:40": ("15:40", "17:10", 5),
    "17:20": ("17:20", "18:50", 6),
    "19:00": ("19:00", "20:30", 7),
    "08:30": ("08:30", "10:00", 1),
    "10:20": ("10:20", "11:50", 2),
    "12:30": ("12:30", "14:00", 3),
    "14:20": ("14:20", "15:50", 4),
    "16:00": ("16:00", "17:30", 5),
    "17:40": ("17:40", "19:10", 6),
    "19:20": ("19:20", "20:50", 7)
}

class Form(StatesGroup):
    waiting_for_group = State()

def normalize_type(typ: str) -> str:
    t = str(typ).lower().strip()
    if 'лек' in t: return "Лекция"
    elif 'пр' in t or 'сем' in t: return "Практика"
    elif 'лаб' in t: return "Лабораторная"
    return str(typ).capitalize() if typ else ""

def get_slot_info(time_str: str):
    prefix = str(time_str)[:5]
    if prefix in BELLS_TABLE:
        s_str, e_str, slot = BELLS_TABLE[prefix]
        st = datetime.time(int(s_str[:2]), int(s_str[3:]))
        et = datetime.time(int(e_str[:2]), int(e_str[3:]))
        return slot, s_str, e_str, st, et
    return None, str(time_str), "", None, None

def calculate_break_or_window(prev_end_str, curr_start_str, prev_slot=None, curr_slot=None):
    try:
        p_h, p_m = map(int, prev_end_str.split(':'))
        c_h, c_m = map(int, curr_start_str.split(':'))
        diff_m = (c_h * 60 + c_m) - (p_h * 60 + p_m)
        if diff_m <= 0: return ""
        h, m = diff_m // 60, diff_m % 60
        time_txt = f"{h} ч {m} мин" if (h > 0 and m > 0) else (f"{h} ч" if h > 0 else f"{m} мин")
        if (prev_end_str == "11:50" and curr_start_str == "12:30") or (prev_end_str == "11:10" and curr_start_str == "11:50"):
            return f"\n🥪 <i>Обед {time_txt} ({prev_end_str} – {curr_start_str})</i>\n"
        if diff_m >= 45 or (prev_slot and curr_slot and curr_slot - prev_slot > 1):
            return f"\n🕳 <b>Окно {time_txt}</b> ({prev_end_str} – {curr_start_str})\n"
        else:
            return f"\n☕ <i>Перерыв {time_txt} ({prev_end_str} – {curr_start_str})</i>\n"
    except Exception:
        return ""

def get_cyrillic_font(size=13):
    candidate_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        os.path.join(DATA_DIR, "font.ttf"),
        "font.ttf"
    ]
    for p in candidate_paths:
        if os.path.exists(p) and os.path.getsize(p) > 10000:
            try: return ImageFont.truetype(p, size)
            except Exception: pass
    return None

# --- БАЗА ДАННЫХ И МИГРАЦИИ ---
def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
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
            c.execute("""
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    auth_code TEXT PRIMARY KEY,
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    group_name TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    note_key TEXT,
                    note_text TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, note_key)
                )
            """)
            conn.commit()

            # Проверка и добавление недостающих колонок для исключения OperationalError
            c.execute("PRAGMA table_info(auth_sessions)")
            cols = [col[1] for col in c.fetchall()]
            if 'group_name' not in cols:
                try:
                    c.execute("ALTER TABLE auth_sessions ADD COLUMN group_name TEXT DEFAULT '7241452'")
                    conn.commit()
                except Exception: pass
            if 'first_name' not in cols:
                try:
                    c.execute("ALTER TABLE auth_sessions ADD COLUMN first_name TEXT DEFAULT ''")
                    conn.commit()
                except Exception: pass
            if 'status' not in cols:
                try:
                    c.execute("ALTER TABLE auth_sessions ADD COLUMN status TEXT DEFAULT 'pending'")
                    conn.commit()
                except Exception: pass

    except Exception as e:
        logging.error(f"БД ошибка: {e}")

def get_user(user_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, username, group_name, notify_morning, notify_remind, notify_hw, view_type FROM users WHERE user_id = ?", (user_id,))
            return c.fetchone()
    except Exception: return None

def register_user(user_id: int, username: str, group_name: str = DEFAULT_GROUP):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO users (user_id, username, group_name, notify_morning, notify_remind, notify_hw, view_type)
                VALUES (?, ?, ?, 
                    COALESCE((SELECT notify_morning FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT notify_remind FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT notify_hw FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT view_type FROM users WHERE user_id = ?), 'text')
                )
            """, (user_id, username, group_name, user_id, user_id, user_id, user_id))
            conn.commit()
    except Exception as e: logging.error(e)

def update_user_field(user_id: int, field: str, value):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
            conn.commit()
    except Exception as e: logging.error(e)

def get_subscribers(field: str = "notify_morning"):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute(f"SELECT user_id, group_name FROM users WHERE {field} = 1")
            return c.fetchall()
    except Exception: return []

def get_stats():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT count(*), sum(notify_morning) FROM users")
            row = c.fetchone()
            return (row[0] or 0), (row[1] or 0)
    except Exception: return 0, 0

def load_schedule() -> dict:
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception: pass
    return {}

SCHEDULE_DB = load_schedule()

def find_group(query: str):
    clean_q = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', query).lower()
    if not clean_q: return None
    for grp in SCHEDULE_DB.keys():
        if clean_q == re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower():
            return grp
    for grp in SCHEDULE_DB.keys():
        if clean_q in re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower():
            return grp
    return None

def get_group_schedule(group_name: str) -> dict:
    data = SCHEDULE_DB.get(group_name)
    if not data:
        matched = find_group(group_name)
        if matched: data = SCHEDULE_DB.get(matched)
    if isinstance(data, dict) and "schedule" in data: return data["schedule"]
    return {"в": {d: [] for d in DAYS_ORDER}, "н": {d: [] for d in DAYS_ORDER}}

def get_week_info(target_date: datetime.date = None):
    if target_date is None: target_date = datetime.datetime.now(MSK_TZ).date()
    weeks_diff = (target_date - ANCHOR_MONDAY).days // 7
    return ('в', 'Верхняя неделя 🔼') if weeks_diff % 2 == 0 else ('н', 'Нижняя неделя 🔽')

def render_table_png(lessons: list, day_name: str, group_name: str):
    if not HAS_PILLOW: return None
    font_header = get_cyrillic_font(13)
    font_bold = get_cyrillic_font(13)
    font_cell = get_cyrillic_font(12)
    font_time = get_cyrillic_font(12)
    font_sub = get_cyrillic_font(11)
    if not font_header: return None

    width, header_h, row_h, padding = 760, 44, 76, 14
    total_h = padding * 2 + header_h + max(1, len(lessons)) * row_h
    img = Image.new('RGB', (width, total_h), color='#0e1621')
    draw = ImageDraw.Draw(img)
    
    x0, y0, x1, y1 = padding, padding, width - padding, total_h - padding
    draw.rectangle([x0, y0, x1, y1], fill='#17212b', outline='#242f3d', width=2)
    cols = [x0, x0 + 130, x0 + 410, x0 + 590, x1]
    draw.rectangle([x0, y0, x1, y0 + header_h], fill='#1c2736', outline='#242f3d', width=1)
    
    headers = ["Пара", "Предмет", "Преподаватель", "Аудитория"]
    for i, h in enumerate(headers):
        draw.text((cols[i] + 12, y0 + 14), h, fill='#7f91a4', font=font_header)
        if i > 0: draw.line([(cols[i], y0), (cols[i], y1)], fill='#242f3d', width=1)
            
    cy = y0 + header_h
    for idx, l in enumerate(lessons):
        draw.line([(x0, cy), (x1, cy)], fill='#242f3d', width=1)
        slot, s_str, e_str, _, _ = get_slot_info(l.get('time', ''))
        slot_str = str(slot or idx + 1)
        t_range = f"{s_str} — {e_str}" if e_str else l.get('time', '')
        
        draw.text((cols[0] + 12, cy + 17), slot_str, fill='#ffffff', font=font_bold)
        draw.text((cols[0] + 12, cy + 39), t_range, fill='#40a7e3', font=font_time)
        
        subj = l.get('subject', '')
        typ = normalize_type(l.get('type', ''))
        if len(subj) > 34: subj = subj[:32] + "..."
        draw.text((cols[1] + 12, cy + 17), subj, fill='#ffffff', font=font_bold)
        if typ:
            type_color = '#4ade80' if 'практ' in typ.lower() else '#60a5fa'
            draw.text((cols[1] + 12, cy + 39), f"({typ})", fill=type_color, font=font_sub)
            
        teach = l.get('teacher', '—')
        if len(teach) > 22: teach = teach[:20] + "..."
        draw.text((cols[2] + 12, cy + 27), teach, fill='#cbd5e1', font=font_cell)
        
        place = f"{l.get('building','')}, {l.get('room','')}" if l.get('room') else l.get('building','')
        draw.text((cols[3] + 12, cy + 27), place[:18], fill='#94a3b8', font=font_cell)
        cy += row_h
        
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

def format_day_text(day_name: str, wn_code: str, lessons: list, group_name: str = "", date_str: str = "") -> str:
    wn_label = "Верхняя неделя 🔼" if wn_code == 'в' else "Нижняя неделя 🔽"
    header_title = f"📅 <b>{day_name}</b>" + (f" ({date_str})" if date_str else "") + f" — <i>{wn_label}</i>"
    lines = [header_title]
    if group_name: lines.append(f"👥 Группа: <code>{group_name}</code>")
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
            if break_or_window: lines.append(break_or_window)

        time_range = f"{l['s_str']} – {l['e_str']}" if l.get('e_str') else l.get('time')
        typ_full = normalize_type(l.get('type', ''))
        type_badge = f" | <b>{typ_full}</b>" if typ_full else ""
        lines.append(f"\n⏰ <b>{time_range}</b>{type_badge}")
        lines.append(f"📘 <b>{l['subject']}</b>")

        place_parts = []
        if l.get('building'): place_parts.append(f"<b>{l['building']}</b>")
        if l.get('room'): place_parts.append(f"ауд. <b>{l['room']}</b>")
        if place_parts: lines.append(f"📍 {', '.join(place_parts)}")
        if l.get('teacher'): lines.append(f"👤 <i>{l['teacher']}</i>")

    return "\n".join(lines).strip()

def get_now_status(lessons: list, check_dt: datetime.datetime, group_name: str = "") -> str:
    if not lessons: return "🎉 <b>Сегодня занятий нет!</b> Можно отдыхать."
    enriched = []
    for l in lessons:
        slot, s_str, e_str, st, et = get_slot_info(l.get('time', ''))
        if st and et: enriched.append({**l, "st": st, "et": et, "s_str": s_str, "e_str": e_str, "slot": slot})
    if not enriched: return "🎉 <b>Сегодня пар нет!</b>"

    enriched.sort(key=lambda x: x["st"])
    curr_t = check_dt.time()
    first_st, last_et = enriched[0]["st"], enriched[-1]["et"]

    if curr_t < first_st:
        diff_m = (first_st.hour * 60 + first_st.minute) - (curr_t.hour * 60 + curr_t.minute)
        f = enriched[0]
        return f"⏰ <b>Пары ещё не начались</b>\n\n⏳ До первой пары: <b>{diff_m} мин</b>\nВ <b>{f['s_str']}</b> — <b>{f['subject']}</b>\n📍 {f.get('building', '')} {f.get('room', '')}"
    if curr_t > last_et:
        return "🎉 <b>Все пары на сегодня завершились!</b> Можно отдыхать."

    for i, l in enumerate(enriched):
        if l["st"] <= curr_t <= l["et"]:
            diff_m = (l["et"].hour * 60 + l["et"].minute) - (curr_t.hour * 60 + curr_t.minute)
            nxt = enriched[i+1] if i + 1 < len(enriched) else None
            nxt_str = f"\n➡️ Следующая в <b>{nxt['s_str']}</b>: {nxt['subject']} ({nxt['room']})" if nxt else "\n🏁 Это последняя пара на сегодня!"
            return f"⚡ <b>Сейчас идёт пара:</b>\n\n⏰ <b>{l['s_str']} – {l['e_str']}</b>\n📘 <b>{l['subject']}</b>\n📍 {l.get('building', '')} {l.get('room', '')}\n\n⏳ До конца: <b>{diff_m} мин</b>{nxt_str}"

        if i + 1 < len(enriched):
            nxt = enriched[i+1]
            if l["et"] < curr_t < nxt["st"]:
                diff_m = (nxt["st"].hour * 60 + nxt["st"].minute) - (curr_t.hour * 60 + curr_t.minute)
                return f"☕ <b>Сейчас перерыв</b> ({l['e_str']} – {nxt['s_str']})\n\n⏳ До звонка: <b>{diff_m} мин</b>\n➡️ В <b>{nxt['s_str']}</b>: <b>{nxt['subject']}</b>"

    return "ℹ️ Нет информации о текущей паре."

# --- КЛАВИАТУРЫ БОТА ---
def main_keyboard(user_group: str = DEFAULT_GROUP, is_admin: bool = False) -> ReplyKeyboardMarkup:
    web_url = f"{WEB_APP_URL}?group={user_group}"
    top_button = [KeyboardButton(text="⚡ Открыть расписание онлайн", web_app=WebAppInfo(url=web_url), style="success")]
    rows = [
        top_button,
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📅 Завтра")],
        [KeyboardButton(text="🗓 Неделя"), KeyboardButton(text="⏱ Сейчас")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🔍 Сменить группу")]
    ]
    if is_admin: rows.append([KeyboardButton(text="🌐 Веб-Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def schedule_inline_keyboard(date_str: str, wn_code: str) -> InlineKeyboardMarkup:
    d = datetime.date.fromisoformat(date_str)
    prev_d, next_d = d - datetime.timedelta(days=1), d + datetime.timedelta(days=1)
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
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nav_{date_str}_{wn_code}")]
    ])

def settings_keyboard(user_row, is_admin: bool = False) -> InlineKeyboardMarkup:
    grp = user_row[2] if user_row else DEFAULT_GROUP
    m_val, r_val, hw_val, view_val = (user_row[3] if user_row else 1), (user_row[4] if user_row else 1), (user_row[5] if user_row else 1), (user_row[6] if user_row else "text")

    kb = [
        [InlineKeyboardButton(text=f"🎨 Вид расписания: {'Таблица 📊' if view_val=='table' else 'Текст 📝'}", callback_data="toggle_view")],
        [InlineKeyboardButton(text=f"🔔 Утреннее расписание (07:30): {'Вкл' if m_val else 'Выкл'}", callback_data="toggle_morning")],
        [InlineKeyboardButton(text=f"⏰ Напоминание за 15 мин: {'Вкл' if r_val else 'Выкл'}", callback_data="toggle_remind")],
        [InlineKeyboardButton(text=f"📚 Сводка на вечер (17:00): {'Вкл' if hw_val else 'Выкл'}", callback_data="toggle_hw")],
        [InlineKeyboardButton(text=f"👥 Группа: {grp}", callback_data="change_group")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="🚀 Тест рассылки", callback_data="admin_test_push")])
        kb.append([InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

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
            else: await target.message.edit_text(text, reply_markup=reply_kb)
        else: await target.answer(text, reply_markup=reply_kb)
        return

    png_buf = None
    if view_type == 'table' and HAS_PILLOW:
        png_buf = render_table_png(lessons, day_name, grp)

    if png_buf:
        file_input = BufferedInputFile(png_buf.getvalue(), filename="schedule.png")
        if is_callback:
            if target.message.photo:
                await target.message.edit_media(InputMediaPhoto(media=file_input, caption=caption_header), reply_markup=reply_kb)
            else:
                await target.message.delete()
                await target.message.answer_photo(photo=file_input, caption=caption_header, reply_markup=reply_kb, show_caption_above_media=True)
        else:
            await target.answer_photo(photo=file_input, caption=caption_header, reply_markup=reply_kb, show_caption_above_media=True)
    else:
        text = format_day_text(day_name, wn_code, lessons, grp, date_obj.strftime('%d.%m.%Y'))
        if is_callback:
            if target.message.photo:
                await target.message.delete()
                await target.message.answer(text, reply_markup=reply_kb)
            else: await target.message.edit_text(text, reply_markup=reply_kb)
        else: await target.answer(text, reply_markup=reply_kb)

# --- ХЕНДЛЕРЫ БОТА ---
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    args = msg.text.split()[1] if len(msg.text.split()) > 1 else ""

    # Авторизация из приложения
    if args.startswith("auth_"):
        auth_code = args.replace("auth_", "").strip()
        user = get_user(msg.from_user.id)
        grp = user[2] if user else DEFAULT_GROUP
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO auth_sessions (auth_code, user_id, username, first_name, group_name, status)
                VALUES (?, ?, ?, ?, ?, 'confirmed')
            """, (auth_code, msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "", grp))
            conn.commit()
        await msg.answer(f"✅ <b>Вход подтверждён, {msg.from_user.first_name}!</b>\n\nВернитесь в приложение — Совёнок AI разблокирован, а заметки синхронизированы.")
        return

    if args == "support":
        await msg.answer(f"💬 Техподдержка бота и расписания: {SUPPORT_USERNAME}")
        return

    user = get_user(msg.from_user.id)
    is_adm = (msg.from_user.id == ADMIN_ID)
    if not user:
        await state.set_state(Form.waiting_for_group)
        await msg.answer("👋 <b>Добро пожаловать в бот расписания НЧИ КФУ!</b>\n\nНапиши номер своей группы (например: <code>7241452</code>):")
        return

    _, wn_name = get_week_info()
    text = f"👋 С возвращением! Группа: <code>{user[2]}</code>\n⚡ Сейчас идёт: <b>{wn_name}</b>\n\nИспользуй кнопки внизу экрана:"
    if is_adm: text += "\n\n👑 <i>Доступна кнопка «🌐 Веб-Админка».</i>"
    await msg.answer(text, reply_markup=main_keyboard(user[2], is_admin=is_adm))

@dp.message(Form.waiting_for_group)
async def process_custom_group(msg: Message, state: FSMContext):
    raw_query = msg.text.strip()
    matched = find_group(raw_query)

    # Строгая проверка на правильность группы
    if not matched:
        await msg.answer("❌ <b>Группа не найдена в базе НЧИ КФУ!</b>\n\nПожалуйста, проверьте номер и напишите снова (например: <code>7241452</code> или <code>18.2-545</code>):")
        return

    register_user(msg.from_user.id, msg.from_user.username or "", matched)
    await state.clear()
    is_adm = (msg.from_user.id == ADMIN_ID)
    _, wn_name = get_week_info()
    await msg.answer(f"✅ Отлично! Установлена группа: <b>{matched}</b>\n⚡ Текущая неделя: <b>{wn_name}</b>", reply_markup=main_keyboard(matched, is_admin=is_adm))

@dp.message(F.text.contains("Сменить группу"))
@dp.message(Command("setgroup"))
async def cmd_change_group(msg: Message, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    await msg.answer("✍️ Напиши номер новой группы:")

@dp.callback_query(F.data == "change_group")
async def cb_change_group(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    await call.message.answer("✍️ Напиши номер новой группы:")
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
    parts = call.data.split("_")
    user = get_user(call.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP

    if parts[1] == "today":
        today = datetime.datetime.now(MSK_TZ).date()
        wn_code, _ = get_week_info(today)
        await send_or_edit_schedule(call, today, wn_code, grp, is_callback=True)
    elif parts[1] == "week":
        wn_code = parts[2] if len(parts) > 2 else get_week_info()[0]
        wn_label = "Верхняя неделя 🔼" if wn_code == 'в' else "Нижняя неделя 🔽"
        sched = get_group_schedule(grp)
        res = [f"🗓 <b>Расписание на всю неделю ({wn_label})</b>\n👥 Группа: <code>{grp}</code>\n"]
        for day in DAYS_ORDER:
            lessons = sched.get(wn_code, {}).get(day, [])
            if lessons: res.append(format_day_text(day, wn_code, lessons))
        reply_kb = schedule_inline_keyboard(datetime.datetime.now(MSK_TZ).date().isoformat(), wn_code)
        if call.message.photo:
            await call.message.delete()
            await call.message.answer("\n\n".join(res), reply_markup=reply_kb)
        else: await call.message.edit_text("\n\n".join(res), reply_markup=reply_kb)
    else:
        target_d = datetime.date.fromisoformat(parts[1])
        wn_code = parts[2] if len(parts) > 2 else get_week_info(target_d)[0]
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
    lessons = get_group_schedule(grp).get(wn_code, {}).get(DAYS_MAP[now_msk.weekday()], [])
    await msg.answer(get_now_status(lessons, now_msk, grp))

@dp.message(F.text == "🗓 Неделя")
@dp.message(Command("week"))
async def cmd_week(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    wn_code, wn_name = get_week_info()
    sched = get_group_schedule(grp)
    res = [f"🗓 <b>Расписание на текущую неделю ({wn_name})</b>\n👥 Группа: <code>{grp}</code>\n"]
    for day in DAYS_ORDER:
        lessons = sched.get(wn_code, {}).get(day, [])
        if lessons: res.append(format_day_text(day, wn_code, lessons))
    await msg.answer("\n\n".join(res), reply_markup=schedule_inline_keyboard(datetime.datetime.now(MSK_TZ).date().isoformat(), wn_code))

@dp.message(F.text.contains("Настройки"))
@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    user = get_user(msg.from_user.id)
    if not user:
        register_user(msg.from_user.id, msg.from_user.username or "", DEFAULT_GROUP)
        user = get_user(msg.from_user.id)
    await msg.answer("⚙️ <b>Настройки уведомлений и вида расписания:</b>", reply_markup=settings_keyboard(user, is_admin=(msg.from_user.id == ADMIN_ID)))

@dp.callback_query(F.data == "toggle_view")
async def cb_toggle_view(call: CallbackQuery):
    user = get_user(call.from_user.id)
    new_v = "table" if (user[6] if user else "text") == "text" else "text"
    update_user_field(call.from_user.id, "view_type", new_v)
    await call.message.edit_reply_markup(reply_markup=settings_keyboard(get_user(call.from_user.id), is_admin=(call.from_user.id == ADMIN_ID)))
    await call.answer()

@dp.callback_query(F.data == "toggle_morning")
async def cb_toggle_morning(call: CallbackQuery):
    user = get_user(call.from_user.id)
    new_v = 0 if (user[3] if user else 1) else 1
    update_user_field(call.from_user.id, "notify_morning", new_v)
    await call.message.edit_reply_markup(reply_markup=settings_keyboard(get_user(call.from_user.id), is_admin=(call.from_user.id == ADMIN_ID)))
    await call.answer()

@dp.callback_query(F.data == "toggle_remind")
async def cb_toggle_remind(call: CallbackQuery):
    user = get_user(call.from_user.id)
    new_v = 0 if (user[4] if user else 1) else 1
    update_user_field(call.from_user.id, "notify_remind", new_v)
    await call.message.edit_reply_markup(reply_markup=settings_keyboard(get_user(call.from_user.id), is_admin=(call.from_user.id == ADMIN_ID)))
    await call.answer()

@dp.callback_query(F.data == "toggle_hw")
async def cb_toggle_hw(call: CallbackQuery):
    user = get_user(call.from_user.id)
    new_v = 0 if (user[5] if user else 1) else 1
    update_user_field(call.from_user.id, "notify_hw", new_v)
    await call.message.edit_reply_markup(reply_markup=settings_keyboard(get_user(call.from_user.id), is_admin=(call.from_user.id == ADMIN_ID)))
    await call.answer()

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    tot, sub = get_stats()
    await call.answer(f"📊 Пользователей: {tot} | Подписчиков: {sub}", show_alert=True)

@dp.callback_query(F.data == "admin_test_push")
async def cb_admin_test_push(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID: return
    user = get_user(call.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    today = datetime.datetime.now(MSK_TZ).date()
    wn_code, _ = get_week_info(today)
    lessons = get_group_schedule(grp).get(wn_code, {}).get(DAYS_MAP[today.weekday()] if today.weekday() < 6 else 'Понедельник', [])
    await call.message.answer("☀️ <b>[ТЕСТ РАССЫЛКИ] Расписание:</b>\n\n" + format_day_text("Сегодня", wn_code, lessons, grp, today.strftime('%d.%m.%Y')))
    await call.answer("Отправлено!")

@dp.message(F.text == "🌐 Веб-Админка")
async def cmd_web_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID: return
    url = f"{WEB_APP_URL}?admin=true&token={ADMIN_TOKEN}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Панель управления", web_app=WebAppInfo(url=url))]])
    await msg.answer(f"🛠 <b>Ссылка на управление:</b>\n<code>{url}</code>", reply_markup=kb)

# --- ФОНОВЫЙ ПЛАНИРОВЩИК РАССЫЛОК ---
async def background_scheduler():
    last_morning, last_evening = None, None
    sent_reminders = set()

    while True:
        try:
            now_msk = datetime.datetime.now(MSK_TZ)
            today_date = now_msk.date()
            cur_hhmm = now_msk.strftime("%H:%M")

            # Рассылка в 07:30
            if cur_hhmm == "07:30" and last_morning != today_date:
                last_morning = today_date
                if today_date.weekday() != 6:
                    wn_code, _ = get_week_info(today_date)
                    day_name = DAYS_MAP[today_date.weekday()]
                    for uid, grp in get_subscribers("notify_morning"):
                        try:
                            lessons = get_group_schedule(grp).get(wn_code, {}).get(day_name, [])
                            await bot.send_message(uid, "☀️ <b>Доброе утро! Расписание на сегодня:</b>\n\n" + format_day_text(day_name, wn_code, lessons, grp, today_date.strftime('%d.%m.%Y')))
                            await asyncio.sleep(0.05)
                        except Exception: pass

            # Сводка в 17:00
            if cur_hhmm == "17:00" and last_evening != today_date:
                last_evening = today_date
                tom_date = today_date + datetime.timedelta(days=1)
                if tom_date.weekday() != 6:
                    wn_code, _ = get_week_info(tom_date)
                    day_name = DAYS_MAP[tom_date.weekday()]
                    for uid, grp in get_subscribers("notify_hw"):
                        try:
                            lessons = get_group_schedule(grp).get(wn_code, {}).get(day_name, [])
                            if lessons:
                                await bot.send_message(uid, "📚 <b>Вечерняя сводка расписания на завтра:</b>\n\n" + format_day_text(day_name, wn_code, lessons, grp, tom_date.strftime('%d.%m.%Y')))
                                await asyncio.sleep(0.05)
                        except Exception: pass

            # Напоминания за 15 минут до пары
            if today_date.weekday() != 6:
                wn_code, _ = get_week_info(today_date)
                day_name = DAYS_MAP[today_date.weekday()]
                for uid, grp in get_subscribers("notify_remind"):
                    lessons = get_group_schedule(grp).get(wn_code, {}).get(day_name, [])
                    if not lessons: continue
                    targets = [lessons[0]] if len(lessons) > 0 else []
                    for idx_l in range(1, len(lessons)):
                        p_l, c_l = lessons[idx_l-1], lessons[idx_l]
                        if ("11:50" in p_l.get('time', '') and "12:30" in c_l.get('time', '')) or ("11:10" in p_l.get('time', '') and "11:50" in c_l.get('time', '')):
                            targets.append(c_l)
                    for t_l in targets:
                        st_str = t_l.get('time', '')[:5]
                        try:
                            h, m = map(int, st_str.split(':'))
                            rem_str = f"{(h*60 + m - 15)//60:02d}:{(h*60 + m - 15)%60:02d}"
                            rem_key = f"{uid}_{today_date}_{st_str}"
                            if cur_hhmm == rem_str and rem_key not in sent_reminders:
                                sent_reminders.add(rem_key)
                                await bot.send_message(uid, f"⏰ <b>Через 15 минут пара!</b>\n\nВ <b>{st_str}</b>: <b>{t_l.get('subject')}</b>\n📍 {t_l.get('building')} ауд. <b>{t_l.get('room')}</b>")
                        except Exception: pass
        except Exception as e: logging.error(e)
        await asyncio.sleep(25)

# --- AIOHTTP ВЕБ-СЕРВЕР И API ---
def cors_response(data: dict):
    return web.json_response(data, headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})

async def handle_options(request):
    return web.Response(headers={"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Methods": "GET, POST, OPTIONS", "Access-Control-Allow-Headers": "Content-Type"})

async def handle_api_auth_poll(request):
    code = request.query.get('code', '')
    if not code: return cors_response({"status": "error"})
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, username, first_name, group_name, status FROM auth_sessions WHERE auth_code = ?", (code,))
        row = c.fetchone()
    if row and row[4] == 'confirmed':
        return cors_response({"status": "confirmed", "user_id": row[0], "username": row[1], "first_name": row[2], "target": row[3]})
    return cors_response({"status": "pending"})

async def handle_api_ai_compress(request):
    try:
        data = await request.json()
        raw_text = data.get("text", "").strip()
        custom_task = data.get("task", "summary")
        if not raw_text: return cors_response({"status": "error", "message": "Пустой текст"})

        active_key = os.getenv("GROQ_API_KEY", "").strip()
        if active_key:
            instruction = AI_SYSTEM_PROMPT
            if custom_task == 'cards': instruction += "\nСделай шпаргалку в формате Вопрос — Ответ."
            elif custom_task == 'simple': instruction += "\nОбъясни материал простыми словами."

            payload = {
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": f"Лекция:\n{raw_text}"}],
                "temperature": 0.3, "max_tokens": 1200
            }
            async with ClientSession() as session:
                async with session.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {active_key}", "Content-Type": "application/json"}, json=payload, timeout=25) as resp:
                    if resp.status == 200:
                        res_json = await resp.json()
                        ai_res = res_json['choices'][0]['message']['content'].replace('**', '')
                        return cors_response({"status": "ok", "result": ai_res})
                    else:
                        logging.error(f"Groq API status: {resp.status}")

        sentences = [s.strip() for s in re.split(r'[.!?]\s+', raw_text) if len(s.strip()) > 5]
        return cors_response({"status": "ok", "result": "📌 Главные тезисы:\n\n" + "\n".join([f"- {s}." for s in sentences[:5]])})
    except Exception as e:
        return cors_response({"status": "error", "message": str(e)})

async def handle_api_save_note(request):
    try:
        data = await request.json()
        uid, n_key, n_text = int(data.get("user_id", 0)), data.get("note_key", ""), data.get("note_text", "").strip()
        if not uid or not n_key: return cors_response({"status": "error"})
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            if n_text: c.execute("INSERT OR REPLACE INTO user_notes (user_id, note_key, note_text) VALUES (?, ?, ?)", (uid, n_key, n_text))
            else: c.execute("DELETE FROM user_notes WHERE user_id = ? AND note_key = ?", (uid, n_key))
            conn.commit()
        return cors_response({"status": "ok"})
    except Exception: return cors_response({"status": "error"})

def create_web_app():
    app = web.Application()
    app.router.add_get('/api/auth_poll', handle_api_auth_poll)
    app.router.add_post('/api/ai_compress', handle_api_ai_compress)
    app.router.add_post('/api/save_note', handle_api_save_note)
    app.router.add_route('OPTIONS', '/{tail:.*}', handle_options)
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
        logging.info(f"Веб-сервер запущен на порту {PORT}")
    except Exception as e: logging.warning(e)

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
