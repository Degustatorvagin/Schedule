import asyncio
import datetime
import json
import logging
import os
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
    Message
)
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import pandas as pd

TOKEN = os.getenv("BOT_TOKEN", "8918873090:AAFL5x_T3O5yr5swc5GUJKygjUsDqDEdpZQ")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8537137900"))
DEFAULT_GROUP = "7241452"
ANCHOR_MONDAY = datetime.date(2026, 8, 31)
JSON_FILE = "schedule.json"
DB_FILE = "users.db"
MSK_TZ = datetime.timezone(datetime.timedelta(hours=3))

DAYS_ORDER = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
DAYS_MAP = {0: 'Понедельник', 1: 'Вторник', 2: 'Среда', 3: 'Четверг', 4: 'Пятница', 5: 'Суббота'}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

# --- FSM ДЛЯ ВВОДА ГРУППЫ ---
class Form(StatesGroup):
    waiting_for_group = State()

# --- БАЗА ДАННЫХ SQLITE ---
def init_db():
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

def get_user(user_id: int):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, username, group_name, notify_enabled FROM users WHERE user_id = ?", (user_id,))
        return cursor.fetchone()

def register_user(user_id: int, username: str, group_name: str = DEFAULT_GROUP):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT OR REPLACE INTO users (user_id, username, group_name, notify_enabled)
            VALUES (?, ?, ?, COALESCE((SELECT notify_enabled FROM users WHERE user_id = ?), 1))
        """, (user_id, username, group_name, user_id))
        conn.commit()

def update_user_group(user_id: int, group_name: str):
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET group_name = ? WHERE user_id = ?", (group_name, user_id))
        conn.commit()

def toggle_user_notify(user_id: int) -> int:
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET notify_enabled = 1 - notify_enabled WHERE user_id = ?", (user_id,))
        conn.commit()
        cursor.execute("SELECT notify_enabled FROM users WHERE user_id = ?", (user_id,))
        res = cursor.fetchone()
        return res[0] if res else 1

def get_subscribers():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, group_name FROM users WHERE notify_enabled = 1")
        return cursor.fetchall()

def get_stats():
    with sqlite3.connect(DB_FILE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT count(*), sum(notify_enabled) FROM users")
        row = cursor.fetchone()
        return (row[0] or 0), (row[1] or 0)

# --- РАСПИСАНИЕ И ЛОГИКА ---
def load_schedule() -> dict:
    if os.path.exists(JSON_FILE):
        with open(JSON_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"в": {d: [] for d in DAYS_ORDER}, "н": {d: [] for d in DAYS_ORDER}}

def save_schedule(data: dict):
    with open(JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

SCHEDULE = load_schedule()

def get_week_info(target_date: datetime.date = None):
    if target_date is None:
        target_date = datetime.datetime.now(MSK_TZ).date()
    weeks_diff = (target_date - ANCHOR_MONDAY).days // 7
    if weeks_diff % 2 == 0:
        return 'в', 'Верхняя 🔼'
    return 'н', 'Нижняя 🔽'

def format_day(day_name: str, wn: str, lessons: list) -> str:
    wn_label = "Верхняя неделя 🔼" if wn == 'в' else "Нижняя неделя 🔽"
    lines = [f"📅 <b>{day_name}</b> ({wn_label})", "━━━━━━━━━━━━━━━━━━━━"]
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
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="➡️ Завтра")],
            [KeyboardButton(text="🔼 Верхняя неделя"), KeyboardButton(text="🔽 Нижняя неделя")],
            [KeyboardButton(text="ℹ️ Какая неделя?"), KeyboardButton(text="⚙️ Настройки")]
        ],
        resize_keyboard=True
    )

def onboarding_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🎓 {DEFAULT_GROUP} (ИСиП)", callback_data="onboard_default")],
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

# --- ХЕНДЛЕРЫ ---
@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    user = get_user(msg.from_user.id)
    if not user:
        text = (
            "👋 <b>Добро пожаловать в бот расписания НЧИ КФУ!</b>\n\n"
            "Давай настроим профиль. Выбери свою группу или укажи её номер:"
        )
        await msg.answer(text, reply_markup=onboarding_keyboard())
        return

    _, wn_name = get_week_info()
    grp = user[2]
    text = (
        f"👋 С возвращением! Группа: <b>{grp}</b>\n"
        f"⚡ Сейчас идет: <b>{wn_name}</b>\n\n"
        f"Используй кнопки внизу для просмотра расписания."
    )
    if msg.from_user.id == ADMIN_ID:
        text += "\n\n👑 <i>Админ: отправь файл .xlsx для обновления базы.</i>"
    await msg.answer(text, reply_markup=main_keyboard())

@dp.callback_query(F.data == "onboard_default")
async def cb_onboard_default(call: CallbackQuery):
    register_user(call.from_user.id, call.from_user.username or "", DEFAULT_GROUP)
    _, wn_name = get_week_info()
    await call.message.edit_text(
        f"✅ Отлично! Установлена группа: <b>{DEFAULT_GROUP}</b> (ИСиП).\n"
        f"🔔 Утренние уведомления в 07:30: <b>Включены</b>.\n"
        f"⚡ Текущая неделя: <b>{wn_name}</b>"
    )
    await call.message.answer("Главное меню доступно:", reply_markup=main_keyboard())
    await call.answer()

@dp.callback_query(F.data == "onboard_custom")
@dp.callback_query(F.data == "change_group")
async def cb_input_group(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    await call.message.answer("✍️ Напиши номер своей группы (например: <code>7241452</code>):")
    await call.answer()

@dp.message(Form.waiting_for_group)
async def process_custom_group(msg: Message, state: FSMContext):
    new_grp = msg.text.strip()
    register_user(msg.from_user.id, msg.from_user.username or "", new_grp)
    await state.clear()
    await msg.answer(f"✅ Группа успешно сохранена: <b>{new_grp}</b>", reply_markup=main_keyboard())

@dp.message(F.text == "⚙️ Настройки")
async def cmd_settings(msg: Message):
    user = get_user(msg.from_user.id)
    if not user:
        register_user(msg.from_user.id, msg.from_user.username or "", DEFAULT_GROUP)
        user = get_user(msg.from_user.id)

    notify_status = "Включена 🔔 (каждое утро в 07:30)" if user[3] else "Выключена 🔕"
    is_adm = (msg.from_user.id == ADMIN_ID)
    text = (
        "⚙️ <b>Настройки профиля</b>\n\n"
        f"👥 Твоя группа: <b>{user[2]}</b>\n"
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
    text = (
        "⚙️ <b>Настройки профиля</b>\n\n"
        f"👥 Твоя группа: <b>{user[2]}</b>\n"
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
    await call.answer(f"📊 Пользователей: {total}\n🔔 Подписчиков на рассылку: {active_notify}", show_alert=True)

@dp.callback_query(F.data == "admin_test_push")
async def cb_admin_test_push(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("Доступ запрещен", show_alert=True)
        return
    today = datetime.datetime.now(MSK_TZ).date()
    wn_code, _ = get_week_info(today)
    day_name = DAYS_MAP[today.weekday()] if today.weekday() < 6 else 'Понедельник'
    lessons = SCHEDULE.get(wn_code, {}).get(day_name, [])
    demo_text = "☀️ <b>[ТЕСТ РАССЫЛКИ] Доброе утро! Расписание на сегодня:</b>\n\n" + format_day(day_name, wn_code, lessons)
    await call.message.answer(demo_text)
    await call.answer("Тестовое уведомление отправлено!")

@dp.message(F.text == "ℹ️ Какая неделя?")
async def cmd_current_week(msg: Message):
    today = datetime.datetime.now(MSK_TZ).date()
    _, wn_name = get_week_info(today)
    today_str = today.strftime("%d.%m.%Y")
    await msg.answer(f"📆 Сегодня: <b>{today_str}</b>\n⚡ Текущая неделя: <b>{wn_name}</b>")

@dp.message(F.text == "📅 Сегодня")
async def cmd_today(msg: Message):
    today = datetime.datetime.now(MSK_TZ).date()
    if today.weekday() == 6:
        await msg.answer("🎉 Сегодня воскресенье! Занятий нет.")
        return
    wn_code, _ = get_week_info(today)
    day_name = DAYS_MAP[today.weekday()]
    lessons = SCHEDULE.get(wn_code, {}).get(day_name, [])
    await msg.answer(format_day(day_name, wn_code, lessons))

@dp.message(F.text == "➡️ Завтра")
async def cmd_tomorrow(msg: Message):
    tomorrow = datetime.datetime.now(MSK_TZ).date() + datetime.timedelta(days=1)
    if tomorrow.weekday() == 6:
        await msg.answer("🎉 Завтра воскресенье! Выходной.")
        return
    wn_code, _ = get_week_info(tomorrow)
    day_name = DAYS_MAP[tomorrow.weekday()]
    lessons = SCHEDULE.get(wn_code, {}).get(day_name, [])
    await msg.answer(format_day(day_name, wn_code, lessons))

@dp.message(F.text == "🔼 Верхняя неделя")
async def cmd_upper(msg: Message):
    await msg.answer("Выбери день (<b>Верхняя неделя</b>):", reply_markup=days_keyboard('в'))

@dp.message(F.text == "🔽 Нижняя неделя")
async def cmd_lower(msg: Message):
    await msg.answer("Выбери день (<b>Нижняя неделя</b>):", reply_markup=days_keyboard('н'))

@dp.callback_query(F.data.startswith("day_"))
async def cb_day(call: CallbackQuery):
    _, wn, day_name = call.data.split("_")
    lessons = SCHEDULE.get(wn, {}).get(day_name, [])
    await call.message.edit_text(format_day(day_name, wn, lessons), reply_markup=days_keyboard(wn))
    await call.answer()

@dp.callback_query(F.data.startswith("all_"))
async def cb_all(call: CallbackQuery):
    wn = call.data.split("_")[1]
    wn_label = "Верхняя неделя 🔼" if wn == 'в' else "Нижняя неделя 🔽"
    parts = [f"📚 <b>Вся {wn_label} целиком</b>\n"]
    for day in DAYS_ORDER:
        lessons = SCHEDULE.get(wn, {}).get(day, [])
        if lessons:
            parts.append(format_day(day, wn, lessons))
    await call.message.edit_text("\n\n".join(parts), reply_markup=days_keyboard(wn))
    await call.answer()

# --- ПАРСИНГ EXCEL ---
def parse_excel(file_path: str, group: str = DEFAULT_GROUP) -> dict:
    df = pd.read_excel(file_path, sheet_name=0, header=None)
    target_col = None
    for c in range(df.shape[1]):
        if group in str(df.iloc[0, c]):
            target_col = c
            break
    if target_col is None:
        raise ValueError(f"Группа {group} не найдена в таблице!")

    col_time = target_col - 2
    col_subject = target_col
    col_bld = target_col + 1
    col_room = target_col + 2
    col_type = target_col + 3
    col_teacher = target_col + 5

    parsed = {"в": {d: [] for d in DAYS_ORDER}, "н": {d: [] for d in DAYS_ORDER}}
    for day_idx, day_name in enumerate(DAYS_ORDER):
        start_row = 2 + day_idx * 14
        for slot in range(7):
            for wn_offset, wn in [(0, 'в'), (1, 'н')]:
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
                    
                    parsed[wn][day_name].append({
                        "time": time_str,
                        "subject": str(subj).strip(),
                        "building": str(df.iloc[row, col_bld]).strip() if pd.notna(df.iloc[row, col_bld]) else "",
                        "room": room_str,
                        "type": str(df.iloc[row, col_type]).strip() if pd.notna(df.iloc[row, col_type]) else "",
                        "teacher": str(df.iloc[row, col_teacher]).strip() if pd.notna(df.iloc[row, col_teacher]) else ""
                    })
    return parsed

@dp.message(F.document)
async def handle_excel_upload(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    fname = msg.document.file_name or ""
    if not (fname.endswith('.xlsx') or fname.endswith('.xls')):
        await msg.answer("⚠️ Принимаются только файлы .xlsx")
        return

    status = await msg.answer("⏳ Скачиваю и парсю расписание...")
    tmp_path = f"temp_{msg.document.file_id}.xlsx"
    try:
        await bot.download(msg.document, destination=tmp_path)
        global SCHEDULE
        new_data = parse_excel(tmp_path)
        save_schedule(new_data)
        SCHEDULE = new_data
        
        v_count = sum(len(l) for l in new_data["в"].values())
        n_count = sum(len(l) for l in new_data["н"].values())
        await status.edit_text(
            f"✅ <b>Расписание успешно обновлено!</b>\n\n"
            f"• Верхняя неделя: {v_count} пар\n"
            f"• Нижняя неделя: {n_count} пар"
        )
    except Exception as e:
        await status.edit_text(f"❌ Ошибка при разборе: {e}")
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

# --- ФОНОВАЯ УТРЕННЯЯ РАССЫЛКА (07:30 ПО МСК) ---
async def morning_broadcast_worker():
    last_sent_date = None
    while True:
        try:
            now_msk = datetime.datetime.now(MSK_TZ)
            # Проверяем: 07:30 утра по Москве, еще не отправляли сегодня, и не воскресенье (6)
            if now_msk.hour == 7 and now_msk.minute == 30 and last_sent_date != now_msk.date():
                last_sent_date = now_msk.date()
                if now_msk.weekday() != 6:
                    wn_code, _ = get_week_info(now_msk.date())
                    day_name = DAYS_MAP[now_msk.weekday()]
                    lessons = SCHEDULE.get(wn_code, {}).get(day_name, [])
                    morning_text = "☀️ <b>Доброе утро! Расписание на сегодня:</b>\n\n" + format_day(day_name, wn_code, lessons)
                    
                    subscribers = get_subscribers()
                    for uid, grp in subscribers:
                        try:
                            await bot.send_message(uid, morning_text)
                            await asyncio.sleep(0.05)
                        except Exception as e:
                            logging.warning(f"Не удалось отправить уведомление пользователю {uid}: {e}")
        except Exception as e:
            logging.error(f"Ошибка в цикле рассылки: {e}")
        await asyncio.sleep(20)

async def main():
    init_db()
    asyncio.create_task(morning_broadcast_worker())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
