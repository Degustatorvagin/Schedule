import asyncio
import datetime
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
    WebAppInfo
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# --- НАСТРОЙКИ ХОСТИНГА BOTHOST ---
TOKEN = os.getenv("BOT_TOKEN", "8918873090:AAFL5x_T3O5yr5swc5GUJKygjUsDqDEdpZQ")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8537137900"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "key")
WEB_DOMAIN = os.getenv("WEB_DOMAIN", "https://bot-1791299850-3323-degustatorvagin.bothost.tech").rstrip("/")
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
ANCHOR_MONDAY = datetime.date(2026, 8, 31)  # 2 сентября 2026 — верхняя неделя
MSK_TZ = datetime.timezone(datetime.timedelta(hours=3))

DAYS_ORDER = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
DAYS_MAP = {0: 'Понедельник', 1: 'Вторник', 2: 'Среда', 3: 'Четверг', 4: 'Пятница', 5: 'Суббота'}

# --- РАСПИСАНИЕ ЗВОНКОВ (КОЛЛЕДЖ + ВЫСШАЯ ШКОЛА) ---
BELLS_TABLE = {
    # Высшая школа (ВШЭиП)
    "08:00": ("08:00", "09:30", 1, None),
    "09:40": ("09:40", "11:10", 2, "🥪 Обед 40 мин (11:10 – 11:50)"),
    "11:50": ("11:50", "13:20", 3, None),
    "13:30": ("13:30", "15:00", 4, None),
    "15:40": ("15:40", "17:10", 5, None),
    "17:20": ("17:20", "18:50", 6, None),
    "19:00": ("19:00", "20:30", 7, None),

    # Колледж (ИЭК)
    "08:30": ("08:30", "10:00", 1, None),
    "10:20": ("10:20", "11:50", 2, "🥪 Обед 40 мин (11:50 – 12:30)"),
    "12:30": ("12:30", "14:00", 3, None),
    "14:20": ("14:20", "15:50", 4, None),
    "16:00": ("16:00", "17:30", 5, None),
    "17:40": ("17:40", "19:10", 6, None),
    "19:20": ("19:20", "20:50", 7, None)
}

def get_slot_info(time_str: str):
    prefix = time_str[:5]
    if prefix in BELLS_TABLE:
        s_str, e_str, slot, lunch = BELLS_TABLE[prefix]
        st = datetime.time(int(s_str[:2]), int(s_str[3:]))
        et = datetime.time(int(e_str[:2]), int(e_str[3:]))
        return slot, s_str, e_str, st, et, lunch
    return None, time_str, "", None, None, None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

class Form(StatesGroup):
    waiting_for_group = State()

# --- БАЗА ДАННЫХ SQLITE ---
def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    group_name TEXT DEFAULT '7241452',
                    notify_enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
    except Exception as e:
        logging.error(f"Ошибка БД: {e}")

def get_user(user_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, username, group_name, notify_enabled FROM users WHERE user_id = ?", (user_id,))
            return cursor.fetchone()
    except Exception:
        return None

def register_user(user_id: int, username: str, group_name: str = DEFAULT_GROUP):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO users (user_id, username, group_name, notify_enabled)
                VALUES (?, ?, ?, COALESCE((SELECT notify_enabled FROM users WHERE user_id = ?), 1))
            """, (user_id, username, group_name, user_id))
            conn.commit()
    except Exception as e:
        logging.error(f"Ошибка регистрации: {e}")

def toggle_user_notify(user_id: int) -> int:
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET notify_enabled = 1 - notify_enabled WHERE user_id = ?", (user_id,))
            conn.commit()
            cursor.execute("SELECT notify_enabled FROM users WHERE user_id = ?", (user_id,))
            res = cursor.fetchone()
            return res[0] if res else 1
    except Exception:
        return 1

def get_subscribers():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id, group_name FROM users WHERE notify_enabled = 1")
            return cursor.fetchall()
    except Exception:
        return []

def get_stats():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT count(*), sum(notify_enabled) FROM users")
            row = cursor.fetchone()
            return (row[0] or 0), (row[1] or 0)
    except Exception:
        return 0, 0

# --- РАСПИСАНИЕ И ПОИСК ГРУПП ---
def load_schedule() -> dict:
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_schedule(data: dict):
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

SCHEDULE_DB = load_schedule()

def find_group(query: str):
    clean_q = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', query).lower()
    if not clean_q:
        return None
    for grp in SCHEDULE_DB.keys():
        clean_grp = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower()
        if clean_q == clean_grp:
            return grp
    for grp in SCHEDULE_DB.keys():
        clean_grp = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower()
        if clean_q in clean_grp:
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

# --- ФОРМАТИРОВАНИЕ РАСПИСАНИЯ (ВАРИАНТ А) ---
def format_day_variant_a(day_name: str, wn_code: str, lessons: list, group_name: str = "", date_str: str = "") -> str:
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
        slot, s_str, e_str, st, et = get_slot_by_time_str(l.get('time', ''))
        enriched.append({**l, "slot": slot, "s_str": s_str, "e_str": e_str, "st": st, "et": et})

    for i, l in enumerate(enriched):
        if i > 0:
            prev = enriched[i-1]
            if prev["slot"] and l["slot"]:
                slot_diff = l["slot"] - prev["slot"]
                if slot_diff == 1:
                    if prev["slot"] == 2 and l["slot"] == 3:
                        lines.append("\n🥪 <i>Обед 40 мин (11:50 – 12:30)</i>\n")
                elif slot_diff > 1:
                    m_diff = (l["st"].hour * 60 + l["st"].minute) - (prev["et"].hour * 60 + prev["et"].minute)
                    h = m_diff // 60
                    m = m_diff % 60
                    time_txt = f"{h} ч {m} мин" if m else f"{h} ч"
                    lines.append(f"\n🕳 <b>Окно {time_txt}</b> ({prev['e_str']} – {l['s_str']})\n")

        time_range = f"{l['s_str']} – {l['e_str']}" if l['e_str'] else l['time']
        typ = l.get('type', '')
        type_badge = f" | {typ}" if typ else ""

        sub_emoji = "📘"
        if "пр" in typ.lower():
            sub_emoji = "📗"
        elif "физ" in l.get('subject', '').lower():
            sub_emoji = "🏃"

        lines.append(f"\n⏰ <b>{time_range}</b>{type_badge}")
        lines.append(f"{sub_emoji} <b>{l['subject']}</b>")

        place_parts = []
        if l.get('building'):
            place_parts.append(l['building'])
        if l.get('room'):
            place_parts.append(f"ауд. {l['room']}")
        if place_parts:
            lines.append(f"📍 {', '.join(place_parts)}")
        if l.get('teacher'):
            lines.append(f"👤 {l['teacher']}")

    return "\n".join(lines).strip()

def get_now_status(lessons: list, check_dt: datetime.datetime, group_name: str = "") -> str:
    if not lessons:
        return "🎉 <b>Сегодня занятий нет!</b> Можно отдыхать."

    enriched = []
    for l in lessons:
        slot, s_str, e_str, st, et = get_slot_by_time_str(l.get('time', ''))
        if st and et:
            enriched.append({**l, "st": st, "et": et, "s_str": s_str, "e_str": e_str})

    if not enriched:
        return "🎉 <b>Сегодня пар нет!</b>"

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
            nxt_str = f"\n➡️ Следующая в <b>{nxt['s_str']}</b>: {nxt['subject']} (ауд. {nxt['room']})" if nxt else "\n🏁 Это последняя пара на сегодня!"
            return (f"⚡ <b>Сейчас идёт занятие:</b>\n\n"
                    f"⏰ <b>{l['s_str']} – {l['e_str']}</b>\n"
                    f"📘 <b>{l['subject']}</b> ({l.get('type', '')})\n"
                    f"📍 {l.get('building', '')} {l.get('room', '')} | 👤 {l.get('teacher', '')}\n\n"
                    f"⏳ До конца пары: <b>{diff_m} мин</b>{nxt_str}")

        if i + 1 < len(enriched):
            nxt = enriched[i+1]
            if l["et"] < curr_t < nxt["st"]:
                diff_m = (nxt["st"].hour * 60 + nxt["st"].minute) - (curr_t.hour * 60 + curr_t.minute)
                is_lunch = (l["e_str"] == "11:50" and nxt["s_str"] == "12:30")
                break_title = "🥪 <b>Сейчас обеденный перерыв (11:50 – 12:30)</b>" if is_lunch else f"☕ <b>Сейчас перерыв ({l['e_str']} – {nxt['s_str']})</b>"
                return (f"{break_title}\n\n"
                        f"⏳ До звонка на пару осталось: <b>{diff_m} мин</b>\n"
                        f"➡️ В <b>{nxt['s_str']}</b>: <b>{nxt['subject']}</b>\n"
                        f"📍 {nxt.get('building', '')} {nxt.get('room', '')}")

    return "ℹ️ Нет информации о текущей паре."

# --- КЛАВИАТУРЫ ---
def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    top_button = [KeyboardButton(text="⚡ Открыть расписание онлайн", web_app=WebAppInfo(url=WEB_DOMAIN))] if WEB_DOMAIN.startswith("https://") else [KeyboardButton(text="⏱ Сейчас")]
    rows = [
        top_button,
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📅 Завтра")],
        [KeyboardButton(text="🗓 Неделя"), KeyboardButton(text="⏱ Сейчас")] if WEB_DOMAIN.startswith("https://") else [KeyboardButton(text="🗓 Неделя")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🔍 Сменить группу")]
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🌐 Веб-Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def schedule_inline_keyboard(date_str: str) -> InlineKeyboardMarkup:
    d = datetime.date.fromisoformat(date_str)
    prev_d = d - datetime.timedelta(days=1)
    next_d = d + datetime.timedelta(days=1)
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="◀️ Вчера", callback_data=f"nav_{prev_d.isoformat()}"),
            InlineKeyboardButton(text="📅 Сегодня", callback_data="nav_today"),
            InlineKeyboardButton(text="Завтра ▶️", callback_data=f"nav_{next_d.isoformat()}")
        ],
        [
            InlineKeyboardButton(text="🗓 Вся неделя", callback_data="nav_week"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data=f"nav_{date_str}")
        ]
    ])

def settings_keyboard(notify_enabled: bool, is_admin: bool = False) -> InlineKeyboardMarkup:
    status_icon = "🔔" if notify_enabled else "🔕"
    status_text = "Вкл" if notify_enabled else "Выкл"
    kb = [
        [InlineKeyboardButton(text=f"{status_icon} Утреннее расписание (07:30): {status_text}", callback_data="toggle_notify")],
        [InlineKeyboardButton(text="🔍 Сменить группу", callback_data="change_group")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="🚀 Тест рассылки (мне)", callback_data="admin_test_push")])
        kb.append([InlineKeyboardButton(text="📊 Статистика бота", callback_data="admin_stats")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- ХЕНДЛЕРЫ TELEGRAM ---
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    user = get_user(msg.from_user.id)
    is_adm = (msg.from_user.id == ADMIN_ID)
    if not user:
        await state.set_state(Form.waiting_for_group)
        text = (
            "👋 <b>Добро пожаловать в бот расписания НЧИ КФУ!</b>\n\n"
            "Напиши номер своей группы (например: <code>7241452</code> или <code>18.03-545</code>):"
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
    await msg.answer(text, reply_markup=main_keyboard(is_admin=is_adm))

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
        reply_markup=main_keyboard(is_admin=is_adm)
    )

@dp.message(F.text == "🔍 Сменить группу")
@dp.message(Command("setgroup"))
async def cmd_change_group(msg: Message, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    await msg.answer("✍️ Напиши номер новой группы (например: <code>7241452</code> или <code>18.03-545</code>):")

@dp.callback_query(F.data == "change_group")
async def cb_change_group(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    await call.message.answer("✍️ Напиши номер новой группы (например: <code>7241452</code> или <code>18.03-545</code>):")
    await call.answer()

@dp.message(F.text == "📅 Сегодня")
@dp.message(Command("today"))
async def cmd_today(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    today = datetime.datetime.now(MSK_TZ).date()

    if today.weekday() == 6:
        text = f"📅 <b>Воскресенье</b> ({today.strftime('%d.%m.%Y')})\n👥 Группа: <code>{grp}</code>\n━━━━━━━━━━━━━━━━━━━━\n\n🎉 <b>Выходной день! Пар нет.</b>"
        await msg.answer(text, reply_markup=schedule_inline_keyboard(today.isoformat()))
        return

    wn_code, _ = get_week_info(today)
    day_name = DAYS_MAP[today.weekday()]
    sched = get_group_schedule(grp)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    text = format_day_variant_a(day_name, wn_code, lessons, grp, today.strftime('%d.%m.%Y'))
    await msg.answer(text, reply_markup=schedule_inline_keyboard(today.isoformat()))

@dp.message(F.text == "📅 Завтра")
@dp.message(Command("tomorrow"))
async def cmd_tomorrow(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    tomorrow = datetime.datetime.now(MSK_TZ).date() + datetime.timedelta(days=1)

    if tomorrow.weekday() == 6:
        text = f"📅 <b>Воскресенье</b> ({tomorrow.strftime('%d.%m.%Y')})\n👥 Группа: <code>{grp}</code>\n━━━━━━━━━━━━━━━━━━━━\n\n🎉 <b>Выходной день! Пар нет.</b>"
        await msg.answer(text, reply_markup=schedule_inline_keyboard(tomorrow.isoformat()))
        return

    wn_code, _ = get_week_info(tomorrow)
    day_name = DAYS_MAP[tomorrow.weekday()]
    sched = get_group_schedule(grp)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    text = format_day_variant_a(day_name, wn_code, lessons, grp, tomorrow.strftime('%d.%m.%Y'))
    await msg.answer(text, reply_markup=schedule_inline_keyboard(tomorrow.isoformat()))

@dp.callback_query(F.data.startswith("nav_"))
async def cb_nav_schedule(call: CallbackQuery):
    action = call.data.replace("nav_", "")
    user = get_user(call.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP

    if action == "today":
        target_d = datetime.datetime.now(MSK_TZ).date()
    elif action == "week":
        wn_code, wn_name = get_week_info()
        sched = get_group_schedule(grp)
        parts = [f"🗓 <b>Расписание на всю неделю ({wn_name})</b>\n👥 Группа: <code>{grp}</code>\n"]
        for day in DAYS_ORDER:
            lessons = sched.get(wn_code, {}).get(day, [])
            if lessons:
                parts.append(format_day_variant_a(day, wn_code, lessons))
        await call.message.edit_text("\n\n".join(parts), reply_markup=schedule_inline_keyboard(datetime.datetime.now(MSK_TZ).date().isoformat()))
        await call.answer()
        return
    else:
        try:
            target_d = datetime.date.fromisoformat(action)
        except Exception:
            target_d = datetime.datetime.now(MSK_TZ).date()

    if target_d.weekday() == 6:
        text = f"📅 <b>Воскресенье</b> ({target_d.strftime('%d.%m.%Y')})\n👥 Группа: <code>{grp}</code>\n━━━━━━━━━━━━━━━━━━━━\n\n🎉 <b>Выходной день! Пар нет.</b>"
        await call.message.edit_text(text, reply_markup=schedule_inline_keyboard(target_d.isoformat()))
        await call.answer()
        return

    wn_code, _ = get_week_info(target_d)
    day_name = DAYS_MAP[target_d.weekday()]
    sched = get_group_schedule(grp)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    text = format_day_variant_a(day_name, wn_code, lessons, grp, target_d.strftime('%d.%m.%Y'))
    await call.message.edit_text(text, reply_markup=schedule_inline_keyboard(target_d.isoformat()))
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
            parts.append(format_day_variant_a(day, wn_code, lessons))
    await msg.answer("\n\n".join(parts), reply_markup=schedule_inline_keyboard(datetime.datetime.now(MSK_TZ).date().isoformat()))

@dp.message(F.text == "⚙️ Настройки")
@dp.message(Command("settings"))
async def cmd_settings(msg: Message):
    user = get_user(msg.from_user.id)
    if not user:
        register_user(msg.from_user.id, msg.from_user.username or "", DEFAULT_GROUP)
        user = get_user(msg.from_user.id)

    notify_status = "Включена 🔔" if user[3] else "Выключена 🔕"
    is_adm = (msg.from_user.id == ADMIN_ID)
    _, wn_name = get_week_info()
    text = (
        "⚙️ <b>Настройки профиля</b>\n\n"
        "<blockquote>\n"
        f"┌ 👥 <b>Группа</b>: <code>{user[2]}</code>\n"
        f"├ 🔔 <b>Утренняя рассылка (07:30)</b>: <b>{notify_status}</b>\n"
        f"├ 🥪 <b>Обеденный перерыв</b>: <b>11:50 – 12:30</b>\n"
        f"├ ⚡ <b>Текущая неделя</b>: <b>{wn_name}</b>\n"
        f"└ 🆔 <b>Ваш ID</b>: <code>{msg.from_user.id}</code>\n"
        "</blockquote>\n\n"
        "Нажимайте на кнопки ниже для переключения:"
    )
    await msg.answer(text, reply_markup=settings_keyboard(bool(user[3]), is_admin=is_adm))

@dp.callback_query(F.data == "toggle_notify")
async def cb_toggle_notify(call: CallbackQuery):
    new_val = toggle_user_notify(call.from_user.id)
    is_adm = (call.from_user.id == ADMIN_ID)
    user = get_user(call.from_user.id)
    notify_status = "Включена 🔔" if new_val else "Выключена 🔕"
    _, wn_name = get_week_info()
    text = (
        "⚙️ <b>Настройки профиля</b>\n\n"
        "<blockquote>\n"
        f"┌ 👥 <b>Группа</b>: <code>{user[2]}</code>\n"
        f"├ 🔔 <b>Утренняя рассылка (07:30)</b>: <b>{notify_status}</b>\n"
        f"├ 🥪 <b>Обеденный перерыв</b>: <b>11:50 – 12:30</b>\n"
        f"├ ⚡ <b>Текущая неделя</b>: <b>{wn_name}</b>\n"
        f"└ 🆔 <b>Ваш ID</b>: <code>{call.from_user.id}</code>\n"
        "</blockquote>\n\n"
        "Нажимайте на кнопки ниже для переключения:"
    )
    await call.message.edit_text(text, reply_markup=settings_keyboard(bool(new_val), is_admin=is_adm))
    await call.answer("Настройки обновлены!")

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещен", show_alert=True)
        return
    total, active_notify = get_stats()
    await call.answer(f"📊 Пользователей: {total}\n🔔 Подписчиков на пуши: {active_notify}\n📚 Всего групп в базе: {len(SCHEDULE_DB)}", show_alert=True)

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
    demo_text = "☀️ <b>[ТЕСТ РАССЫЛКИ] Расписание на сегодня:</b>\n\n" + format_day_variant_a(day_name, wn_code, lessons, grp, today.strftime('%d.%m.%Y'))
    await call.message.answer(demo_text)
    await call.answer("Тестовое уведомление отправлено!")

@dp.message(F.text == "🌐 Веб-Админка")
@dp.message(Command("web"))
async def cmd_web_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.answer("⛔ Доступ только для администратора.")
        return
    admin_url = f"{WEB_DOMAIN}/admin?token={ADMIN_TOKEN}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть в браузере", url=admin_url)],
        [InlineKeyboardButton(text="📱 Открыть как Mini App", web_app=WebAppInfo(url=admin_url))]
    ])
    await msg.answer(f"🛠 <b>Панель управления расписанием:</b>\n\n🔗 <code>{admin_url}</code>", reply_markup=kb)

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    text = (
        "📖 <b>Команды бота расписания:</b>\n\n"
        "<blockquote>\n"
        "• /start — перезапустить меню\n"
        "• /today — расписание на сегодня\n"
        "• /tomorrow — расписание на завтра\n"
        "• /week — расписание на неделю\n"
        "• /now — какая пара идёт сейчас\n"
        "• /setgroup — сменить группу\n"
        "• /settings — настройки рассылки\n"
        "• /help — эта справка\n"
        "</blockquote>\n\n"
        "💡 <i>Также можно нажимать кнопки внизу экрана!</i>"
    )
    await msg.answer(text)

# --- ОБНОВЛЕНИЕ БАЗЫ EXCEL (ДЛЯ АДМИНА) ---
@dp.message(F.document)
async def handle_excel_upload(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    fname = msg.document.file_name or ""
    if not (fname.endswith('.xlsx') or fname.endswith('.xls')):
        await msg.answer("⚠️ Принимаются только файлы .xlsx")
        return

    status = await msg.answer("⏳ Скачиваю и обновляю базу всех групп...")
    tmp_path = f"temp_{msg.document.file_id}.xlsx"
    try:
        await bot.download(msg.document, destination=tmp_path)
        global SCHEDULE_DB
        import pandas as pd
        df = pd.read_excel(tmp_path, sheet_name=0, header=None)

        def extract_grp_info(val: str):
            match = re.search(r'Группа\s+([^\(\n]+)(?:\((.*?)(?:,\s*\d+\s*чел\))?\))?', val, re.IGNORECASE)
            if match:
                num = match.group(1).strip()
                spec = match.group(2).strip() if match.group(2) else ""
                return num, spec
            return val.strip(), ""

        updated_count = 0
        for c in range(df.shape[1]):
            val = str(df.iloc[0, c])
            if 'Группа' in val or 'группа' in val:
                num, spec = extract_grp_info(val)
                col_time, col_subject, col_bld = c - 2, c, c + 1
                col_room, col_type, col_teacher = c + 2, c + 3, c + 5

                sched = {"в": {d: [] for d in DAYS_ORDER}, "н": {d: [] for d in DAYS_ORDER}}
                for day_idx, day_name in enumerate(DAYS_ORDER):
                    start_row = 2 + day_idx * 14
                    for slot in range(7):
                        for wn_offset, wn_type in [(0, 'в'), (1, 'н')]:
                            row = start_row + slot * 2 + wn_offset
                            if row >= df.shape[0]:
                                continue
                            subj = df.iloc[row, col_subject]
                            if pd.notna(subj) and str(subj).strip():
                                raw_time = str(df.iloc[row, col_time])
                                time_str = raw_time.split()[0] if ' ' in raw_time else raw_time
                                if len(time_str.split(':')) == 3:
                                    time_str = ':'.join(time_str.split(':')[:2])
                                room = df.iloc[row, col_room]
                                room_str = str(int(room)) if isinstance(room, float) and not pd.isna(room) else (str(room) if pd.notna(room) else "")
                                sched[wn_type][day_name].append({
                                    "time": time_str,
                                    "subject": str(subj).strip(),
                                    "building": str(df.iloc[row, col_bld]).strip() if pd.notna(df.iloc[row, col_bld]) else "",
                                    "room": room_str,
                                    "type": str(df.iloc[row, col_type]).strip() if pd.notna(df.iloc[row, col_type]) else "",
                                    "teacher": str(df.iloc[row, col_teacher]).strip() if pd.notna(df.iloc[row, col_teacher]) else ""
                                })
                SCHEDULE_DB[num] = {"spec": spec, "schedule": sched}
                updated_count += 1

        save_schedule(SCHEDULE_DB)
        await status.edit_text(
            f"✅ <b>База расписания обновлена!</b>\n\n"
            f"• Обновлено групп из файла: {updated_count}\n"
            f"• Всего групп в системе: {len(SCHEDULE_DB)}"
        )
    except Exception as e:
        await status.edit_text(f"❌ Ошибка при разборе: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# --- УТРЕННЯЯ РАССЫЛКА (07:30 MSK) ---
async def morning_broadcast_worker():
    last_sent_date = None
    while True:
        try:
            now_msk = datetime.datetime.now(MSK_TZ)
            if now_msk.hour == 7 and now_msk.minute == 30 and last_sent_date != now_msk.date():
                last_sent_date = now_msk.date()
                if now_msk.weekday() != 6:
                    wn_code, _ = get_week_info(now_msk.date())
                    day_name = DAYS_MAP[now_msk.weekday()]
                    subscribers = get_subscribers()
                    for uid, grp in subscribers:
                        try:
                            sched = get_group_schedule(grp)
                            lessons = sched.get(wn_code, {}).get(day_name, [])
                            msg_text = "☀️ <b>Доброе утро! Расписание на сегодня:</b>\n\n" + format_day_variant_a(day_name, wn_code, lessons, grp, now_msk.strftime('%d.%m.%Y'))
                            await bot.send_message(uid, msg_text)
                            await asyncio.sleep(0.05)
                        except Exception:
                            pass
        except Exception as e:
            logging.error(f"Ошибка в рассылке: {e}")
        await asyncio.sleep(20)

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
    asyncio.create_task(morning_broadcast_worker())

    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logging.info(f"Веб-сервер запущен на 0.0.0.0:{PORT}")
    except Exception as e:
        logging.warning(f"Не удалось поднять порт: {e}")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
