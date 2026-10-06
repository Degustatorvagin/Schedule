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

# --- НАСТРОЙКИ ХОСТИНГА ---
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
ANCHOR_MONDAY = datetime.date(2026, 8, 31)
MSK_TZ = datetime.timezone(datetime.timedelta(hours=3))

DAYS_ORDER = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
DAYS_MAP = {0: 'Понедельник', 1: 'Вторник', 2: 'Среда', 3: 'Четверг', 4: 'Пятница', 5: 'Суббота'}

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

# --- РАСПИСАНИЕ ВСЕХ ГРУПП ---
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
    """Умный поиск группы по номеру (например: '545', '18.2-545', '7241452')"""
    clean_q = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', query).lower()
    if not clean_q:
        return None

    # Прямое совпадение
    for grp in SCHEDULE_DB.keys():
        clean_grp = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower()
        if clean_q == clean_grp:
            return grp

    # Поиск по подстроке (например '545' найдет '18.03-545')
    for grp in SCHEDULE_DB.keys():
        clean_grp = re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower()
        if clean_q in clean_grp:
            return grp

    return None

def get_group_schedule(group_name: str) -> dict:
    data = SCHEDULE_DB.get(group_name)
    if not data:
        # Резервный поиск
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
        return 'в', 'Верхняя 🔼'
    return 'н', 'Нижняя 🔽'

def format_day(day_name: str, wn: str, lessons: list, group_name: str = "") -> str:
    wn_label = "Верхняя неделя 🔼" if wn == 'в' else "Нижняя неделя 🔽"
    header = f"📅 <b>{day_name}</b> ({wn_label})"
    if group_name:
        header += f" | Группа: <code>{group_name}</code>"
    lines = [header, "━━━━━━━━━━━━━━━━━━━━"]
    if not lessons:
        lines.append("🎉 Пар нет! Отдыхаем.")
        return "\n".join(lines)

    for idx, l in enumerate(lessons, 1):
        typ = f"({l['type']})" if l.get('type') else ""
        lines.append(f"<b>{idx}. {l['time']}</b> — <b>{l['subject']}</b> {typ}")
        loc = []
        if l.get('building'):
            loc.append(l['building'])
        if l.get('room'):
            loc.append(f"ауд. {l['room']}")
        if loc:
            lines.append(f"    📍 {', '.join(loc)}")
        if l.get('teacher'):
            lines.append(f"    👤 {l['teacher']}")
    return "\n".join(lines)

# --- КЛАВИАТУРЫ ---
def main_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="➡️ Завтра")],
        [KeyboardButton(text="🔼 Верхняя неделя"), KeyboardButton(text="🔽 Нижняя неделя")],
        [KeyboardButton(text="ℹ️ Какая неделя?"), KeyboardButton(text="⚙️ Настройки")]
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🌐 Веб-Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def onboarding_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎓 7241452 (ИСиП)", callback_data="setgrp_7241452")],
        [InlineKeyboardButton(text="⚖️ 18.03-545 (Юристы)", callback_data="setgrp_18.03-545")],
        [InlineKeyboardButton(text="✍️ Ввести другую группу", callback_data="onboard_custom")]
    ])

def days_keyboard(wn: str) -> InlineKeyboardMarkup:
    short_days = [("Пн", "Понедельник"), ("Вт", "Вторник"), ("Ср", "Среда"),
                  ("Чт", "Четверг"), ("Пт", "Пятница"), ("Сб", "Суббота")]
    buttons = [
        InlineKeyboardButton(text=short, callback_data=f"day_{wn}_{full}")
        for short, full in short_days
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        buttons[:3],
        buttons[3:],
        [InlineKeyboardButton(text="📋 Вся неделя целиком", callback_data=f"all_{wn}")]
    ])

def settings_keyboard(notify_enabled: bool, is_admin: bool = False) -> InlineKeyboardMarkup:
    status_icon = "🔔" if notify_enabled else "🔕"
    status_action = "Выключить" if notify_enabled else "Включить"
    kb = [
        [InlineKeyboardButton(text=f"{status_icon} Рассылка 07:30: {status_action}", callback_data="toggle_notify")],
        [InlineKeyboardButton(text="👥 Сменить группу", callback_data="change_group")]
    ]
    if is_admin:
        kb.append([InlineKeyboardButton(text="🚀 Тест утренней рассылки (мне)", callback_data="admin_test_push")])
        kb.append([InlineKeyboardButton(text="📊 Статистика пользователей", callback_data="admin_stats")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# --- ХЕНДЛЕРЫ TELEGRAM ---
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    user = get_user(msg.from_user.id)
    is_adm = (msg.from_user.id == ADMIN_ID)
    if not user:
        await msg.answer(
            "👋 <b>Добро пожаловать в бот расписания НЧИ КФУ!</b>\n\n"
            "Выбери свою группу кнопкой или введи её номер вручную:",
            reply_markup=onboarding_keyboard()
        )
        return

    _, wn_name = get_week_info()
    text = (
        f"👋 С возвращением! Твоя группа: <b>{user[2]}</b>\n"
        f"⚡ Сейчас идет: <b>{wn_name}</b>\n\n"
        f"Используй кнопки внизу для просмотра расписания."
    )
    if is_adm:
        text += "\n\n👑 <i>Ты администратор. Доступна кнопка «🌐 Веб-Админка».</i>"
    await msg.answer(text, reply_markup=main_keyboard(is_admin=is_adm))

@dp.callback_query(F.data.startswith("setgrp_"))
async def cb_set_group(call: CallbackQuery):
    grp = call.data.replace("setgrp_", "")
    register_user(call.from_user.id, call.from_user.username or "", grp)
    _, wn_name = get_week_info()
    is_adm = (call.from_user.id == ADMIN_ID)
    await call.message.edit_text(
        f"✅ Установлена группа: <b>{grp}</b>.\n"
        f"🔔 Утренние уведомления в 07:30: <b>Включены</b>.\n"
        f"⚡ Текущая неделя: <b>{wn_name}</b>"
    )
    await call.message.answer("Главное меню доступно:", reply_markup=main_keyboard(is_admin=is_adm))
    await call.answer()

@dp.callback_query(F.data == "onboard_custom")
@dp.callback_query(F.data == "change_group")
async def cb_input_group(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    await call.message.answer("✍️ Напиши номер своей группы (например: <code>7241452</code> или <code>18.03-545</code>):")
    await call.answer()

@dp.message(Form.waiting_for_group)
async def process_custom_group(msg: Message, state: FSMContext):
    raw_query = msg.text.strip()
    matched = find_group(raw_query)
    final_grp = matched if matched else raw_query
    register_user(msg.from_user.id, msg.from_user.username or "", final_grp)
    await state.clear()
    is_adm = (msg.from_user.id == ADMIN_ID)
    note = "" if matched else "\n⚠️ <i>Группа пока не найдена в расписании, но сохранена.</i>"
    await msg.answer(f"✅ Группа сохранена: <b>{final_grp}</b>{note}", reply_markup=main_keyboard(is_admin=is_adm))

@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(msg: Message):
    user = get_user(msg.from_user.id)
    if not user:
        register_user(msg.from_user.id, msg.from_user.username or "", DEFAULT_GROUP)
        user = get_user(msg.from_user.id)

    notify_status = "Включена 🔔 (каждое утро в 07:30)" if user and user[3] else "Выключена 🔕"
    is_adm = (msg.from_user.id == ADMIN_ID)
    grp = user[2] if user else DEFAULT_GROUP
    text = (
        "⚙️ <b>Настройки профиля</b>\n\n"
        f"👥 Твоя группа: <b>{grp}</b>\n"
        f"⏰ Утренняя рассылка: <b>{notify_status}</b>\n"
    )
    if is_adm:
        text += "\n👑 <i>Статус: Администратор</i>"
    await msg.answer(text, reply_markup=settings_keyboard(bool(user[3]), is_admin=is_adm))

@dp.callback_query(F.data == "toggle_notify")
async def cb_toggle_notify(call: CallbackQuery):
    new_val = toggle_user_notify(call.from_user.id)
    is_adm = (call.from_user.id == ADMIN_ID)
    user = get_user(call.from_user.id)
    notify_status = "Включена 🔔 (каждое утро в 07:30)" if new_val else "Выключена 🔕"
    grp = user[2] if user else DEFAULT_GROUP
    text = (
        "⚙️ <b>Настройки профиля</b>\n\n"
        f"👥 Твоя группа: <b>{grp}</b>\n"
        f"⏰ Утренняя рассылка: <b>{notify_status}</b>\n"
    )
    if is_adm:
        text += "\n👑 <i>Статус: Администратор</i>"
    await call.message.edit_text(text, reply_markup=settings_keyboard(bool(new_val), is_admin=is_adm))
    await call.answer("Настройки обновлены!")

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещен", show_alert=True)
        return
    total, active_notify = get_stats()
    await call.answer(f"📊 Пользователей: {total}\n🔔 Подписчиков на рассылку: {active_notify}\n📚 Всего групп в базе: {len(SCHEDULE_DB)}", show_alert=True)

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
    demo_text = "☀️ <b>[ТЕСТ РАССЫЛКИ] Расписание на сегодня:</b>\n\n" + format_day(day_name, wn_code, lessons, grp)
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

@dp.message(F.text == "ℹ️ Какая неделя?")
async def cmd_current_week(msg: Message):
    today = datetime.datetime.now(MSK_TZ).date()
    _, wn_name = get_week_info(today)
    await msg.answer(f"📆 Сегодня: <b>{today.strftime('%d.%m.%Y')}</b>\n⚡ Текущая неделя: <b>{wn_name}</b>")

@dp.message(F.text == "📅 Сегодня")
async def cmd_today(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    today = datetime.datetime.now(MSK_TZ).date()
    if today.weekday() == 6:
        await msg.answer("🎉 Сегодня воскресенье! Занятий нет.")
        return
    wn_code, _ = get_week_info(today)
    day_name = DAYS_MAP[today.weekday()]
    sched = get_group_schedule(grp)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    await msg.answer(format_day(day_name, wn_code, lessons, grp))

@dp.message(F.text == "➡️ Завтра")
async def cmd_tomorrow(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    tomorrow = datetime.datetime.now(MSK_TZ).date() + datetime.timedelta(days=1)
    if tomorrow.weekday() == 6:
        await msg.answer("🎉 Завтра воскресенье! Выходной.")
        return
    wn_code, _ = get_week_info(tomorrow)
    day_name = DAYS_MAP[tomorrow.weekday()]
    sched = get_group_schedule(grp)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    await msg.answer(format_day(day_name, wn_code, lessons, grp))

@dp.message(F.text == "🔼 Верхняя неделя")
async def cmd_upper(msg: Message):
    await msg.answer("Выбери день (<b>Верхняя неделя</b>):", reply_markup=days_keyboard('в'))

@dp.message(F.text == "🔽 Нижняя неделя")
async def cmd_lower(msg: Message):
    await msg.answer("Выбери день (<b>Нижняя неделя</b>):", reply_markup=days_keyboard('н'))

@dp.callback_query(F.data.startswith("day_"))
async def cb_day(call: CallbackQuery):
    user = get_user(call.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    _, wn, day_name = call.data.split("_")
    sched = get_group_schedule(grp)
    lessons = sched.get(wn, {}).get(day_name, [])
    await call.message.edit_text(format_day(day_name, wn, lessons, grp), reply_markup=days_keyboard(wn))
    await call.answer()

@dp.callback_query(F.data.startswith("all_"))
async def cb_all(call: CallbackQuery):
    user = get_user(call.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    wn = call.data.split("_")[1]
    wn_label = "Верхняя неделя 🔼" if wn == 'в' else "Нижняя неделя 🔽"
    sched = get_group_schedule(grp)
    parts = [f"📚 <b>Вся {wn_label} целиком</b> | Группа: <code>{grp}</code>\n"]
    for day in DAYS_ORDER:
        lessons = sched.get(wn, {}).get(day, [])
        if lessons:
            parts.append(format_day(day, wn, lessons))
    await call.message.edit_text("\n\n".join(parts), reply_markup=days_keyboard(wn))
    await call.answer()

# --- ОБНОВЛЕНИЕ БАЗЫ ИЗ ТЕЛЕГРАМ ---
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

        updated_count = 0
        for c in range(df.shape[1]):
            val = str(df.iloc[0, c])
            if 'Группа' in val or 'группа' in val:
                num, spec = extract_group_info(val)
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

# --- УТРЕННЯЯ РАССЫЛКА ПО ВСЕМ ГРУППАМ ---
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
                            msg_text = "☀️ <b>Доброе утро! Расписание на сегодня:</b>\n\n" + format_day(day_name, wn_code, lessons, grp)
                            await bot.send_message(uid, msg_text)
                            await asyncio.sleep(0.05)
                        except Exception:
                            pass
        except Exception as e:
            logging.error(f"Ошибка в рассылке: {e}")
        await asyncio.sleep(20)

# --- ВЕБ-СЕРВЕР ---
async def handle_index(request):
    return web.Response(text=f"<h1>Сервер расписания НЧИ КФУ онлайн</h1><p>Групп в базе: {len(SCHEDULE_DB)}</p>", content_type='text/html')

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
        logging.warning(f"Не удалось поднять веб-порт: {e}")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
