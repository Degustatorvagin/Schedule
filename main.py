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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# --- ССЫЛКА НА САЙТ MINI APP ---
WEB_APP_URL = "https://degustatorvagin.github.io/Schedule/"

# Токен берется из переменных окружения Bothost
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8537137900"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "KEY")
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

def normalize_type(typ: str) -> str:
    t = str(typ).lower().strip()
    if 'лек' in t:
        return "Лекция"
    elif 'пр' in t or 'сем' in t:
        return "Практика"
    elif 'лаб' in t:
        return "Лабораторная"
    return str(typ).capitalize() if typ else ""

def get_slot_info(time_str: str):
    prefix = str(time_str)[:5]
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
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return None

# --- БАЗА ДАННЫХ SQLITE ---
def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    role TEXT DEFAULT 'student',
                    group_name TEXT DEFAULT '7241452',
                    teacher_name TEXT DEFAULT '',
                    notify_morning INTEGER DEFAULT 1,
                    notify_remind INTEGER DEFAULT 1,
                    notify_hw INTEGER DEFAULT 1,
                    view_type TEXT DEFAULT 'text',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            for col, col_type, default_val in [
                ("role", "TEXT", "'student'"),
                ("teacher_name", "TEXT", "''"),
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
            cursor.execute("""
                SELECT user_id, username, group_name, notify_morning, notify_remind, 
                       notify_hw, view_type, role, teacher_name 
                FROM users WHERE user_id = ?
            """, (user_id,))
            return cursor.fetchone()
    except Exception:
        return None

def register_user(user_id: int, username: str, role: str = "student", group_name: str = DEFAULT_GROUP, teacher_name: str = ""):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO users (user_id, username, role, group_name, teacher_name, notify_morning, notify_remind, notify_hw, view_type)
                VALUES (?, ?, ?, ?, ?,
                    COALESCE((SELECT notify_morning FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT notify_remind FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT notify_hw FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT view_type FROM users WHERE user_id = ?), 'text')
                )
            """, (user_id, username, role, group_name, teacher_name, user_id, user_id, user_id, user_id))
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
            cursor.execute(f"SELECT user_id, role, group_name, teacher_name FROM users WHERE {field} = 1")
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

# --- РАСПИСАНИЕ И ПОИСК ---
def load_schedule() -> dict:
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

SCHEDULE_DB = load_schedule()

def get_all_teachers_list():
    teachers = set()
    for grp, data in SCHEDULE_DB.items():
        sched = data.get("schedule", {})
        for wn in ["в", "н"]:
            for day, lessons in sched.get(wn, {}).items():
                for l in lessons:
                    raw_t = l.get("teacher", "").strip()
                    if not raw_t or raw_t == "—":
                        continue
                    parts = re.split(r'\s{2,}|\n|,', raw_t)
                    for p in parts:
                        p = p.strip()
                        if p and len(p) > 3:
                            teachers.add(p)
    return sorted(list(teachers))

TEACHERS_LIST = get_all_teachers_list()

def find_teachers(query: str):
    q = query.lower().strip()
    if not q:
        return []
    exact = [t for t in TEACHERS_LIST if q == t.lower()]
    if exact:
        return exact
    return [t for t in TEACHERS_LIST if q in t.lower()]

def find_group_strict(query: str):
    clean_q = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', query).lower()
    if not clean_q:
        return None
    for grp in SCHEDULE_DB.keys():
        if clean_q == re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower():
            return grp
    if "545" in clean_q:
        if "182" in clean_q or "филолог" in query.lower():
            return "18.2-545" if "18.2-545" in SCHEDULE_DB else None
        if "1803" in clean_q or "юр" in query.lower():
            return "18.03-545" if "18.03-545" in SCHEDULE_DB else None
        return "18.2-545" if "18.2-545" in SCHEDULE_DB else ("18.03-545" if "18.03-545" in SCHEDULE_DB else None)
    if "452" in clean_q:
        if "7241452" in SCHEDULE_DB:
            return "7241452"
    if len(clean_q) >= 4:
        for grp in SCHEDULE_DB.keys():
            clean_grp = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower()
            if clean_q in clean_grp:
                return grp
    return None

def get_group_schedule(group_name: str) -> dict:
    data = SCHEDULE_DB.get(group_name)
    if isinstance(data, dict) and "schedule" in data:
        return data["schedule"]
    return {"в": {d: [] for d in DAYS_ORDER}, "н": {d: [] for d in DAYS_ORDER}}

def get_teacher_schedule(target_teacher: str) -> dict:
    t_sched = {
        "в": {d: [] for d in DAYS_ORDER},
        "н": {d: [] for d in DAYS_ORDER}
    }
    t_clean = target_teacher.lower()
    for grp, g_data in SCHEDULE_DB.items():
        g_sched = g_data.get("schedule", {})
        for wn in ["в", "н"]:
            for day, lessons in g_sched.get(wn, {}).items():
                for l in lessons:
                    teacher_val = l.get("teacher", "")
                    if t_clean in teacher_val.lower():
                        lesson_copy = dict(l)
                        lesson_copy["group"] = grp
                        t_sched[wn][day].append(lesson_copy)
    for wn in ["в", "н"]:
        for day in t_sched[wn]:
            t_sched[wn][day].sort(key=lambda x: x.get("time", ""))
            combined = []
            for item in t_sched[wn][day]:
                matched_existing = False
                for c in combined:
                    if c["time"] == item["time"] and c["subject"] == item["subject"] and c.get("room") == item.get("room"):
                        if item["group"] not in c["group"]:
                            c["group"] += f", {item['group']}"
                        matched_existing = True
                        break
                if not matched_existing:
                    combined.append(dict(item))
            t_sched[wn][day] = combined
    return t_sched

def get_week_info(target_date: datetime.date = None):
    if target_date is None:
        target_date = datetime.datetime.now(MSK_TZ).date()
    weeks_diff = (target_date - ANCHOR_MONDAY).days // 7
    if weeks_diff % 2 == 0:
        return 'в', 'Верхняя неделя 🔼'
    return 'н', 'Нижняя неделя 🔽'

# --- ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ ТАБЛИЦЫ ---
def render_table_png(lessons: list, day_name: str, target_name: str, role: str = "student"):
    if not HAS_PILLOW:
        return None
    font_header = get_cyrillic_font(13)
    font_bold = get_cyrillic_font(13)
    font_cell = get_cyrillic_font(12)
    font_time = get_cyrillic_font(12)
    font_sub = get_cyrillic_font(11)

    if not font_header:
        return None

    width = 760
    header_h = 44
    row_h = 76
    padding = 14
    total_h = padding * 2 + header_h + max(1, len(lessons)) * row_h

    img = Image.new('RGB', (width, total_h), color='#0e1621')
    draw = ImageDraw.Draw(img)

    x0, y0 = padding, padding
    x1, y1 = width - padding, total_h - padding
    draw.rectangle([x0, y0, x1, y1], fill='#17212b', outline='#242f3d', width=2)

    cols = [x0, x0 + 130, x0 + 410, x0 + 590, x1]
    draw.rectangle([x0, y0, x1, y0 + header_h], fill='#1c2736', outline='#242f3d', width=1)

    col2_title = "Группа" if role == "teacher" else "Преподаватель"
    headers = ["Пара", "Предмет", col2_title, "Аудитория"]
    for i, h in enumerate(headers):
        draw.text((cols[i] + 12, y0 + 14), h, fill='#7f91a4', font=font_header)
        if i > 0:
            draw.line([(cols[i], y0), (cols[i], y1)], fill='#242f3d', width=1)

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
        if len(subj) > 34:
            subj = subj[:32] + "..."
        draw.text((cols[1] + 12, cy + 17), subj, fill='#ffffff', font=font_bold)
        if typ:
            type_color = '#4ade80' if 'практ' in typ.lower() else '#60a5fa'
            draw.text((cols[1] + 12, cy + 39), f"({typ})", fill=type_color, font=font_sub)

        info_col2 = l.get('group', '—') if role == "teacher" else l.get('teacher', '—')
        if len(info_col2) > 22:
            info_col2 = info_col2[:20] + "..."
        draw.text((cols[2] + 12, cy + 27), info_col2, fill='#cbd5e1', font=font_cell)

        bld = l.get('building', '')
        room = l.get('room', '')
        place = f"{bld}, {room}" if room else bld
        draw.text((cols[3] + 12, cy + 27), place[:18], fill='#94a3b8', font=font_cell)
        cy += row_h

    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf

# --- ТЕКСТОВОЕ ОФОРМЛЕНИЕ ---
def format_day_text(day_name: str, wn_code: str, lessons: list, target_name: str = "", role: str = "student", date_str: str = "") -> str:
    wn_label = "Верхняя неделя 🔼" if wn_code == 'в' else "Нижняя неделя 🔽"
    header_title = f"📅 <b>{day_name}</b>"
    if date_str:
        header_title += f" ({date_str})"
    header_title += f" — <i>{wn_label}</i>"

    lines = [header_title]
    if role == "teacher":
        lines.append(f"👨‍🏫 Преподаватель: <code>{target_name}</code>")
    else:
        lines.append(f"👥 Группа: <code>{target_name}</code>")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    if not lessons:
        lines.append("\n🎉 <b>В этот день пар нет! Можно отдыхать.</b>")
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

        if role == "teacher":
            lines.append(f"👥 Группа: <code>{l.get('group', '—')}</code>")
        else:
            if l.get('teacher'):
                lines.append(f"👤 <i>{l['teacher']}</i>")
        lines.append("")

    return "\n".join(lines).strip()

def get_now_status(lessons: list, check_dt: datetime.datetime, target_name: str = "", role: str = "student") -> str:
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
        extra = f"\n👥 Группа: <code>{f.get('group', '')}</code>" if role == "teacher" else ""
        return (f"⏰ <b>Пары ещё не начались</b>\n\n"
                f"⏳ До первой пары осталось: <b>{diff_m} мин</b>\n"
                f"В <b>{f['s_str']}</b> — <b>{f['subject']}</b>\n"
                f"📍 {f.get('building', '')} {f.get('room', '')}{extra}")

    if curr_t > last_et:
        return "🎉 <b>Все пары на сегодня завершились!</b> Можно отдыхать."

    for i, l in enumerate(enriched):
        if l["st"] <= curr_t <= l["et"]:
            diff_m = (l["et"].hour * 60 + l["et"].minute) - (curr_t.hour * 60 + curr_t.minute)
            nxt = enriched[i+1] if i + 1 < len(enriched) else None
            nxt_str = f"\n➡️ Следующая в <b>{nxt['s_str']}</b>: {nxt['subject']} ({nxt['room']})" if nxt else "\n🏁 Это последняя пара на сегодня!"
            typ_f = normalize_type(l.get('type', ''))
            extra = f"\n👥 Группа: <code>{l.get('group', '')}</code>" if role == "teacher" else f"\n👤 <i>{l.get('teacher', '')}</i>"
            return (f"⚡ <b>Сейчас идёт занятие:</b>\n\n"
                    f"⏰ <b>{l['s_str']} – {l['e_str']}</b>\n"
                    f"📘 <b>{l['subject']}</b> ({typ_f})\n"
                    f"📍 {l.get('building', '')} {l.get('room', '')}{extra}\n\n"
                    f"⏳ До конца пары: <b>{diff_m} мин</b>{nxt_str}")

        if i + 1 < len(enriched):
            nxt = enriched[i+1]
            if l["et"] < curr_t < nxt["st"]:
                diff_m = (nxt["st"].hour * 60 + nxt["st"].minute) - (curr_t.hour * 60 + curr_t.minute)
                is_lunch = (l["e_str"] == "11:50" and nxt["s_str"] == "12:30") or (l["e_str"] == "11:10" and nxt["s_str"] == "11:50")
                break_title = "🥪 <b>Сейчас обеденный перерыв (40 мин)</b>" if is_lunch else f"☕ <b>Сейчас перерыв ({l['e_str']} – {nxt['s_str']})</b>"
                return (f"{break_title}\n\n"
                        f"⏳ До звонка на пару осталось: <b>{diff_m} мин</b>\n"
                        f"➡️ В <b>{nxt['s_str']}</b>: <b>{nxt['subject']}</b>\n"
                        f"📍 {nxt.get('building', '')} {nxt.get('room', '')}")

    return "ℹ️ Нет информации о текущей паре."

# --- КЛАВИАТУРЫ ---
def role_choice_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎓 Я студент", callback_data="choose_role_student")],
        [InlineKeyboardButton(text="👨‍🏫 Я преподаватель", callback_data="choose_role_teacher")]
    ])

def main_keyboard(role: str = "student", target_name: str = DEFAULT_GROUP, is_admin: bool = False) -> ReplyKeyboardMarkup:
    top_button = [KeyboardButton(text="⚡ Открыть расписание онлайн", web_app=WebAppInfo(url=f"{WEB_APP_URL}?group={target_name}"), style="success")] if role == "student" else [KeyboardButton(text="⏱ Сейчас", style="success")]
    change_btn_text = "🔍 Сменить группу" if role == "student" else "🔍 Сменить преподавателя"
    rows = [
        top_button,
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📅 Завтра")],
        [KeyboardButton(text="🗓 Неделя"), KeyboardButton(text="⏱ Сейчас")] if role == "student" else [KeyboardButton(text="🗓 Неделя")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text=change_btn_text)]
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🌐 Веб-Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def schedule_inline_keyboard(date_str: str, wn_code: str) -> InlineKeyboardMarkup:
    d = datetime.date.fromisoformat(date_str)
    prev_d = d - datetime.timedelta(days=1)
    next_d = d + datetime.timedelta(days=1)
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
    role = user_row[7] if user_row and len(user_row) > 7 else "student"
    grp = user_row[2] if user_row else DEFAULT_GROUP
    teacher = user_row[8] if user_row and len(user_row) > 8 else ""
    m_val = user_row[3] if user_row else 1
    r_val = user_row[4] if user_row else 1
    hw_val = user_row[5] if user_row else 1
    view_val = user_row[6] if user_row else "text"

    view_txt = "Таблица 📊" if view_val == "table" else "Текст 📝"
    m_txt = "Вкл" if m_val else "Выкл"
    r_txt = "Вкл" if r_val else "Выкл"
    hw_txt = "Вкл" if hw_val else "Выкл"

    target_label = f"👥 Группа: {grp}" if role == "student" else f"👨‍🏫 Преподаватель: {teacher}"
    target_cb = "change_group" if role == "student" else "change_teacher"

    kb = [
        [InlineKeyboardButton(text="🔄 Сменить роль (Студент / Преподаватель)", callback_data="switch_role")],
        [InlineKeyboardButton(text=target_label, callback_data=target_cb)],
        [InlineKeyboardButton(text=f"🎨 Вид расписания: {view_txt}", callback_data="toggle_view")],
        [InlineKeyboardButton(text=f"🔔 Утреннее расписание (07:30): {m_txt}", callback_data="toggle_morning")],
        [InlineKeyboardButton(text=f"⏰ Напоминание (1-я пара и после обеда): {r_txt}", callback_data="toggle_remind")],
        [InlineKeyboardButton(text=f"📚 Напоминание о парах на завтра (17:00): {hw_txt}", callback_data="toggle_hw")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="🚀 Тест рассылки (мне)", callback_data="admin_test_push")])
        kb.append([InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- ОТПРАВКА РАСПИСАНИЯ ---
async def send_or_edit_schedule(target, date_obj: datetime.date, wn_code: str, is_callback: bool = False):
    user_row = get_user(target.from_user.id)
    role = user_row[7] if user_row and len(user_row) > 7 else "student"
    target_name = user_row[8] if role == "teacher" else (user_row[2] if user_row else DEFAULT_GROUP)

    day_name = DAYS_MAP[date_obj.weekday()] if date_obj.weekday() < 6 else 'Понедельник'
    sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    view_type = user_row[6] if user_row else 'text'
    reply_kb = schedule_inline_keyboard(date_obj.isoformat(), wn_code)

    sub_title = f"👨‍🏫 <i>Преподаватель: <code>{target_name}</code></i>" if role == "teacher" else f"👥 <i>Группа: <code>{target_name}</code></i>"
    caption_header = f"📅 <b>Расписание на {date_obj.strftime('%d.%m.%Y')}, {day_name}</b>\n{sub_title}"

    if date_obj.weekday() == 6:
        text = f"📅 <b>Воскресенье</b> ({date_obj.strftime('%d.%m.%Y')})\n{sub_title}\n━━━━━━━━━━━━━━━━━━━━\n\n🎉 <b>Выходной день! Пар нет.</b>"
        if is_callback:
            if target.message.photo:
                await target.message.delete()
                await target.message.answer(text, reply_markup=reply_kb)
            else:
                await target.message.edit_text(text, reply_markup=reply_kb)
        else:
            await target.answer(text, reply_markup=reply_kb)
        return

    png_buf = None
    if view_type == 'table' and HAS_PILLOW:
        png_buf = render_table_png(lessons, day_name, target_name, role=role)

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
        text = format_day_text(day_name, wn_code, lessons, target_name, role=role, date_str=date_obj.strftime('%d.%m.%Y'))
        if is_callback:
            if target.message.photo:
                await target.message.delete()
                await target.message.answer(text, reply_markup=reply_kb)
            else:
                await target.message.edit_text(text, reply_markup=reply_kb)
        else:
            await target.answer(text, reply_markup=reply_kb)

# --- FSM СОСТОЯНИЯ ---
class RoleForm(StatesGroup):
    waiting_for_group = State()
    waiting_for_teacher = State()

# --- ХЕНДЛЕРЫ СТАРТА И ВЫБОРА РОЛИ ---
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    user = get_user(msg.from_user.id)
    is_adm = (msg.from_user.id == ADMIN_ID)
    if not user or not (user[2] or (len(user) > 8 and user[8])):
        text = (
            "👋 <b>Добро пожаловать в бот расписания НЧИ КФУ!</b>\n\n"
            "Пожалуйста, выберите, кто вы:"
        )
        await msg.answer(text, reply_markup=role_choice_keyboard())
        return

    role = user[7] if len(user) > 7 else "student"
    target_name = user[8] if role == "teacher" else user[2]
    _, wn_name = get_week_info()
    label = f"👨‍🏫 Преподаватель: <code>{target_name}</code>" if role == "teacher" else f"👥 Группа: <code>{target_name}</code>"
    text = (
        f"👋 С возвращением!\n{label}\n"
        f"⚡ Сейчас идет: <b>{wn_name}</b>\n\n"
        f"Используй кнопки внизу экрана:"
    )
    if is_adm:
        text += "\n\n👑 <i>Статус администратора активен.</i>"
    await msg.answer(text, reply_markup=main_keyboard(role, target_name, is_admin=is_adm))

@dp.callback_query(F.data == "choose_role_student")
@dp.callback_query(F.data == "change_group")
async def cb_role_student(call: CallbackQuery, state: FSMContext):
    await state.set_state(RoleForm.waiting_for_group)
    text = (
        "🎓 <b>Режим студента</b>\n\n"
        "✍️ Напиши номер своей группы (например: <code>7241452</code> или <code>18.2-545</code>):"
    )
    await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data == "choose_role_teacher")
@dp.callback_query(F.data == "change_teacher")
async def cb_role_teacher(call: CallbackQuery, state: FSMContext):
    await state.set_state(RoleForm.waiting_for_teacher)
    text = (
        "👨‍🏫 <b>Режим преподавателя</b>\n\n"
        "✍️ Напишите вашу фамилию (например: <code>Мельников</code>, <code>Волкова</code> или <code>Хузин</code>):"
    )
    await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data == "switch_role")
async def cb_switch_role(call: CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.answer("Выберите новую роль:", reply_markup=role_choice_keyboard())
    await call.answer()

# --- СТРОГАЯ ВАЛИДАЦИЯ ГРУППЫ ---
@dp.message(RoleForm.waiting_for_group)
async def process_student_group(msg: Message, state: FSMContext):
    raw_query = msg.text.strip()
    matched = find_group_strict(raw_query)

    if not matched:
        await msg.answer(
            f"❌ Группа «{raw_query}» не найдена в расписании!\n\n"
            "Пожалуйста, введи реальный номер группы колледжа или высшей школы.\n"
            "<i>Примеры:</i> <code>7241452</code>, <code>18.2-545</code>, <code>18.03-551</code>, <code>7231405</code>."
        )
        return

    register_user(msg.from_user.id, msg.from_user.username or "", role="student", group_name=matched)
    await state.clear()
    _, wn_name = get_week_info()
    is_adm = (msg.from_user.id == ADMIN_ID)
    await msg.answer(
        f"✅ Отлично! Установлена группа: <b>{matched}</b>\n"
        f"🔔 Утреннее расписание в 07:30: <b>Включено</b>\n"
        f"⚡ Текущая неделя: <b>{wn_name}</b>",
        reply_markup=main_keyboard("student", matched, is_admin=is_adm)
    )

# --- ПОИСК И ВЫБОР ПРЕПОДАВАТЕЛЯ ---
@dp.message(RoleForm.waiting_for_teacher)
async def process_teacher_name(msg: Message, state: FSMContext):
    raw_query = msg.text.strip()
    matches = find_teachers(raw_query)

    if not matches:
        await msg.answer(
            f"❌ Преподаватель с фамилией «{raw_query}» не найден в базе расписания.\n\n"
            "Попробуйте ввести только фамилию на русском языке (например: <code>Мельников</code>, <code>Волкова</code>, <code>Хузин</code>):"
        )
        return

    if len(matches) == 1:
        chosen = matches[0]
        register_user(msg.from_user.id, msg.from_user.username or "", role="teacher", teacher_name=chosen)
        await state.clear()
        _, wn_name = get_week_info()
        is_adm = (msg.from_user.id == ADMIN_ID)
        await msg.answer(
            f"✅ Здравствуйте, <b>{chosen}</b>!\n"
            f"Расписание ваших занятий успешно подключено.\n"
            f"⚡ Текущая неделя: <b>{wn_name}</b>",
            reply_markup=main_keyboard("teacher", chosen, is_admin=is_adm)
        )
        return

    kb_rows = []
    for idx, t_name in enumerate(matches[:8]):
        try:
            real_idx = TEACHERS_LIST.index(t_name)
            kb_rows.append([InlineKeyboardButton(text=f"👤 {t_name}", callback_data=f"tchr_{real_idx}")])
        except Exception:
            pass

    await msg.answer(
        f"🔍 По запросу «{raw_query}» найдено несколько преподавателей.\nВыберите себя из списка:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )

@dp.callback_query(F.data.startswith("tchr_"))
async def cb_pick_teacher(call: CallbackQuery, state: FSMContext):
    t_idx = int(call.data.replace("tchr_", ""))
    if 0 <= t_idx < len(TEACHERS_LIST):
        chosen = TEACHERS_LIST[t_idx]
        register_user(call.from_user.id, call.from_user.username or "", role="teacher", teacher_name=chosen)
        await state.clear()
        _, wn_name = get_week_info()
        is_adm = (call.from_user.id == ADMIN_ID)
        await call.message.edit_text(f"✅ Выбран преподаватель: <b>{chosen}</b>")
        await call.message.answer(
            f"Главное меню открыто. Текущая неделя: <b>{wn_name}</b>",
            reply_markup=main_keyboard("teacher", chosen, is_admin=is_adm)
        )
    await call.answer()

# --- КНОПКИ СМЕНЫ ---
@dp.message(F.text.contains("Сменить группу"))
@dp.message(F.text.contains("Сменить преподавателя"))
@dp.message(Command("setgroup"))
async def cmd_change_profile(msg: Message, state: FSMContext):
    user = get_user(msg.from_user.id)
    role = user[7] if user and len(user) > 7 else "student"
    if role == "teacher":
        await state.set_state(RoleForm.waiting_for_teacher)
        await msg.answer("✍️ Напишите фамилию преподавателя:")
    else:
        await state.set_state(RoleForm.waiting_for_group)
        await msg.answer("✍️ Напиши номер группы (например: <code>7241452</code> или <code>18.2-545</code>):")

# --- ОСНОВНЫЕ КОМАНДЫ РАСПИСАНИЯ ---
@dp.message(F.text == "📅 Сегодня")
@dp.message(Command("today"))
async def cmd_today(msg: Message):
    today = datetime.datetime.now(MSK_TZ).date()
    wn_code, _ = get_week_info(today)
    await send_or_edit_schedule(msg, today, wn_code, is_callback=False)

@dp.message(F.text == "📅 Завтра")
@dp.message(Command("tomorrow"))
async def cmd_tomorrow(msg: Message):
    tomorrow = datetime.datetime.now(MSK_TZ).date() + datetime.timedelta(days=1)
    wn_code, _ = get_week_info(tomorrow)
    await send_or_edit_schedule(msg, tomorrow, wn_code, is_callback=False)

@dp.callback_query(F.data.startswith("nav_"))
async def cb_nav_schedule(call: CallbackQuery):
    data_parts = call.data.split("_")
    user = get_user(call.from_user.id)
    role = user[7] if user and len(user) > 7 else "student"
    target_name = user[8] if role == "teacher" else (user[2] if user else DEFAULT_GROUP)

    if data_parts[1] == "today":
        target_d = datetime.datetime.now(MSK_TZ).date()
        wn_code, _ = get_week_info(target_d)
        await send_or_edit_schedule(call, target_d, wn_code, is_callback=True)
    elif data_parts[1] == "week":
        wn_code = data_parts[2] if len(data_parts) > 2 else get_week_info()[0]
        wn_label = "Верхняя неделя 🔼" if wn_code == 'в' else "Нижняя неделя 🔽"
        sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
        sub_title = f"👨‍🏫 <i>Преподаватель: <code>{target_name}</code></i>" if role == "teacher" else f"👥 <i>Группа: <code>{target_name}</code></i>"
        parts = [f"🗓 <b>Расписание на всю неделю ({wn_label})</b>\n{sub_title}\n"]
        for day in DAYS_ORDER:
            lessons = sched.get(wn_code, {}).get(day, [])
            if lessons:
                parts.append(format_day_text(day, wn_code, lessons, target_name, role=role))
        reply_kb = schedule_inline_keyboard(datetime.datetime.now(MSK_TZ).date().isoformat(), wn_code)
        if call.message.photo:
            await call.message.delete()
            await call.message.answer("\n\n".join(parts), reply_markup=reply_kb)
        else:
            await call.message.edit_text("\n\n".join(parts), reply_markup=reply_kb)
    else:
        target_d = datetime.date.fromisoformat(data_parts[1])
        wn_code = data_parts[2] if len(data_parts) > 2 else get_week_info(target_d)[0]
        await send_or_edit_schedule(call, target_d, wn_code, is_callback=True)
    await call.answer()

@dp.message(F.text == "⏱ Сейчас")
@dp.message(Command("now"))
async def cmd_now(msg: Message):
    user = get_user(msg.from_user.id)
    role = user[7] if user and len(user) > 7 else "student"
    target_name = user[8] if role == "teacher" else (user[2] if user else DEFAULT_GROUP)

    now_msk = datetime.datetime.now(MSK_TZ)
    if now_msk.weekday() == 6:
        await msg.answer("🎉 Сегодня воскресенье! Пар нет.")
        return

    wn_code, _ = get_week_info(now_msk.date())
    day_name = DAYS_MAP[now_msk.weekday()]
    sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    res = get_now_status(lessons, now_msk, target_name, role=role)
    await msg.answer(res)

@dp.message(F.text == "🗓 Неделя")
@dp.message(Command("week"))
async def cmd_week(msg: Message):
    user = get_user(msg.from_user.id)
    role = user[7] if user and len(user) > 7 else "student"
    target_name = user[8] if role == "teacher" else (user[2] if user else DEFAULT_GROUP)

    wn_code, wn_name = get_week_info()
    sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
    sub_title = f"👨‍🏫 <i>Преподаватель: <code>{target_name}</code></i>" if role == "teacher" else f"👥 <i>Группа: <code>{target_name}</code></i>"
    parts = [f"🗓 <b>Расписание на текущую неделю ({wn_name})</b>\n{sub_title}\n"]
    for day in DAYS_ORDER:
        lessons = sched.get(wn_code, {}).get(day, [])
        if lessons:
            parts.append(format_day_text(day, wn_code, lessons, target_name, role=role))
    await msg.answer("\n\n".join(parts), reply_markup=schedule_inline_keyboard(datetime.datetime.now(MSK_TZ).date().isoformat(), wn_code))

# --- МЕНЮ НАСТРОЕК ---
@dp.message(F.text.contains("Настройки"))
@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    user = get_user(msg.from_user.id)
    if not user:
        register_user(msg.from_user.id, msg.from_user.username or "", DEFAULT_GROUP)
        user = get_user(msg.from_user.id)

    is_adm = (msg.from_user.id == ADMIN_ID)
    text = (
        "<b>Настройки профиля</b>\n\n"
        "<blockquote>"
        "• 🔄 <b>Смена роли</b> — переключение между студентом и преподавателем\n"
        "• 🎨 <b>Вид расписания</b> — таблица с аудиториями или структурированный текст\n"
        "• 🔔 <b>Утреннее расписание (07:30)</b> — сводка пар на день каждое утро\n"
        "• ⏰ <b>Напоминание за 15 минут</b> — перед первой парой и парой после обеда\n"
        "• 📚 <b>Сводка на завтра (17:00)</b> — вечернее напоминание о расписании"
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
    await call.answer(f"📊 Пользователей: {total}\n🔔 Подписчиков: {active_notify}\n📚 Всего групп в базе: {len(SCHEDULE_DB)}\n👨‍🏫 Преподавателей: {len(TEACHERS_LIST)}", show_alert=True)

@dp.callback_query(F.data == "admin_test_push")
async def cb_admin_test_push(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещен", show_alert=True)
        return
    user = get_user(call.from_user.id)
    role = user[7] if user and len(user) > 7 else "student"
    target_name = user[8] if role == "teacher" else (user[2] if user else DEFAULT_GROUP)
    today = datetime.datetime.now(MSK_TZ).date()
    wn_code, _ = get_week_info(today)
    day_name = DAYS_MAP[today.weekday()] if today.weekday() < 6 else 'Понедельник'
    sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    demo_text = "☀️ <b>[ТЕСТ РАССЫЛКИ] Расписание на сегодня:</b>\n\n" + format_day_text(day_name, wn_code, lessons, target_name, role=role, date_str=today.strftime('%d.%m.%Y'))
    await call.message.answer(demo_text)
    await call.answer("Тестовое уведомление отправлено!")

@dp.message(F.text == "🌐 Веб-Админка")
@dp.message(Command("web"))
async def cmd_web_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.answer("⛔ Доступ только для администратора.")
        return
    admin_url = f"{WEB_APP_URL}?admin=true&token={ADMIN_TOKEN}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть в браузере", url=admin_url)],
        [InlineKeyboardButton(text="📱 Открыть как Mini App", web_app=WebAppInfo(url=admin_url))]
    ])
    await msg.answer(f"🛠 <b>Панель управления расписанием:</b>\n\n🔗 <code>{admin_url}</code>", reply_markup=kb)

# --- ПЛАНИРОВЩИК УВЕДОМЛЕНИЙ ---
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
                    for uid, role, grp, teacher in get_subscribers("notify_morning"):
                        try:
                            target_name = teacher if role == "teacher" else grp
                            sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
                            lessons = sched.get(wn_code, {}).get(day_name, [])
                            msg_text = "☀️ <b>Доброе утро! Расписание на сегодня:</b>\n\n" + format_day_text(day_name, wn_code, lessons, target_name, role=role, date_str=today_date.strftime('%d.%m.%Y'))
                            await bot.send_message(uid, msg_text)
                            await asyncio.sleep(0.05)
                        except Exception:
                            pass

            # 2. Вечерняя сводка в 17:00
            if cur_hhmm == "17:00" and last_evening_date != today_date:
                last_evening_date = today_date
                tom_date = today_date + datetime.timedelta(days=1)
                if tom_date.weekday() != 6:
                    wn_code, _ = get_week_info(tom_date)
                    day_name = DAYS_MAP[tom_date.weekday()]
                    for uid, role, grp, teacher in get_subscribers("notify_hw"):
                        try:
                            target_name = teacher if role == "teacher" else grp
                            sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
                            lessons = sched.get(wn_code, {}).get(day_name, [])
                            if lessons:
                                msg_text = "📚 <b>Вечерняя сводка: пары на завтра:</b>\n\n" + format_day_text(day_name, wn_code, lessons, target_name, role=role, date_str=tom_date.strftime('%d.%m.%Y'))
                                await bot.send_message(uid, msg_text)
                                await asyncio.sleep(0.05)
                        except Exception:
                            pass

            # 3. Напоминания за 15 минут до пары
            if today_date.weekday() != 6:
                wn_code, _ = get_week_info(today_date)
                day_name = DAYS_MAP[today_date.weekday()]
                for uid, role, grp, teacher in get_subscribers("notify_remind"):
                    target_name = teacher if role == "teacher" else grp
                    sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
                    lessons = sched.get(wn_code, {}).get(day_name, [])
                    if not lessons:
                        continue
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
                                info_who = f"\n👥 Группа: <code>{t_l.get('group', '')}</code>" if role == "teacher" else f"\n👤 <i>{t_l.get('teacher', '')}</i>"
                                alert_msg = (
                                    f"⏰ <b>Напоминание: через 15 минут пара!</b>\n\n"
                                    f"В <b>{start_str}</b>: <b>{t_l.get('subject')}</b> ({typ_f})\n"
                                    f"📍 {t_l.get('building')}, ауд. <b>{t_l.get('room')}</b>{info_who}"
                                )
                                await bot.send_message(uid, alert_msg)
                        except Exception:
                            pass
        except Exception as e:
            logging.error(f"Ошибка в планировщике: {e}")
        await asyncio.sleep(25)

# --- ЗАПУСК ВЕБ-СЕРВЕРА ---
async def handle_index(request):
    return web.Response(text=f"<h1>Расписание НЧИ КФУ онлайн</h1><p>Групп: {len(SCHEDULE_DB)} | Преподавателей: {len(TEACHERS_LIST)}</p>", content_type='text/html')

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
