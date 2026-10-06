import asyncio
import datetime
import json
import logging
import os
import sqlite3
import pandas as pd
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

TOKEN = os.getenv("BOT_TOKEN", "8918873090:AAFL5x_T3O5yr5swc5GUJKygjUsDqDEdpZQ")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8537137900"))
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "kfu7241_secret_key")
WEB_DOMAIN = os.getenv("WEB_DOMAIN", "").rstrip("/")
PORT = int(os.getenv("PORT", 8080))

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

class Form(StatesGroup):
    waiting_for_group = State()

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

@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    user = get_user(msg.from_user.id)
    is_adm = (msg.from_user.id == ADMIN_ID)
    if not user:
        await msg.answer(
            "👋 <b>Добро пожаловать в бот расписания НЧИ КФУ!</b>\n\n"
            "Давай настроим профиль. Выбери свою группу или укажи её номер:",
            reply_markup=onboarding_keyboard()
        )
        return

    _, wn_name = get_week_info()
    text = (
        f"👋 С возвращением! Группа: <b>{user[2]}</b>\n"
        f"⚡ Сейчас идет: <b>{wn_name}</b>\n\n"
        f"Используй кнопки внизу для просмотра расписания."
    )
    if is_adm:
        text += "\n\n👑 <i>Ты администратор. Доступна кнопка «🌐 Веб-Админка».</i>"
    await msg.answer(text, reply_markup=main_keyboard(is_admin=is_adm))

@dp.callback_query(F.data == "onboard_default")
async def cb_onboard_default(call: CallbackQuery):
    register_user(call.from_user.id, call.from_user.username or "", DEFAULT_GROUP)
    _, wn_name = get_week_info()
    is_adm = (call.from_user.id == ADMIN_ID)
    await call.message.edit_text(
        f"✅ Отлично! Установлена группа: <b>{DEFAULT_GROUP}</b> (ИСиП).\n"
        f"🔔 Утренние уведомления в 07:30: <b>Включены</b>.\n"
        f"⚡ Текущая неделя: <b>{wn_name}</b>"
    )
    await call.message.answer("Главное меню доступно:", reply_markup=main_keyboard(is_admin=is_adm))
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
    is_adm = (msg.from_user.id == ADMIN_ID)
    await msg.answer(f"✅ Группа успешно сохранена: <b>{new_grp}</b>", reply_markup=main_keyboard(is_admin=is_adm))

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

@dp.message(F.text == "🌐 Веб-Админка")
@dp.message(Command("web"))
async def cmd_web_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        await msg.answer("⛔ Доступ только для администратора.")
        return

    domain = WEB_DOMAIN or "https://твой-домен.bothost.tech"
    admin_url = f"{domain}/admin?token={ADMIN_TOKEN}"
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть в браузере", url=admin_url)],
        [InlineKeyboardButton(text="📱 Открыть как Mini App", web_app=WebAppInfo(url=admin_url))]
    ])

    text = (
        "🛠 <b>Панель управления расписанием:</b>\n\n"
        f"🔗 Ссылка: <code>{admin_url}</code>\n\n"
        "<i>Нажми на кнопку ниже, чтобы открыть панель на ПК, телефоне или прямо внутри Telegram:</i>"
    )
    await msg.answer(text, reply_markup=kb)

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
                    lessons = SCHEDULE.get(wn_code, {}).get(day_name, [])
                    morning_text = "☀️ <b>Доброе утро! Расписание на сегодня:</b>\n\n" + format_day(day_name, wn_code, lessons)
                    
                    subscribers = get_subscribers()
                    for uid, grp in subscribers:
                        try:
                            await bot.send_message(uid, morning_text)
                            await asyncio.sleep(0.05)
                        except Exception as e:
                            logging.warning(f"Не удалось отправить уведомление {uid}: {e}")
        except Exception as e:
            logging.error(f"Ошибка в рассылке: {e}")
        await asyncio.sleep(20)

ADMIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Админ-Панель | НЧИ КФУ</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg: #0b0f19;
            --surface: #131b2e;
            --border: #1e293b;
            --primary: #3b82f6;
            --primary-gradient: linear-gradient(135deg, #6366f1, #3b82f6);
            --success: #10b981;
            --danger: #ef4444;
            --text: #f8fafc;
            --text-muted: #94a3b8;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', sans-serif; }
        body { background: var(--bg); color: var(--text); padding: 16px; min-height: 100vh; }
        .container { max-width: 900px; margin: 0 auto; }
        .header { display: flex; justify-content: space-between; align-items: center; padding-bottom: 20px; border-bottom: 1px solid var(--border); margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
        .title-block h1 { font-size: 22px; font-weight: 700; background: var(--primary-gradient); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .title-block p { font-size: 13px; color: var(--text-muted); }
        .stats-badge { display: flex; gap: 8px; flex-wrap: wrap; }
        .badge { background: var(--surface); border: 1px solid var(--border); padding: 6px 12px; border-radius: 20px; font-size: 13px; font-weight: 500; }
        .tabs { display: flex; gap: 8px; margin-bottom: 24px; overflow-x: auto; padding-bottom: 4px; }
        .tab-btn { background: var(--surface); color: var(--text-muted); border: 1px solid var(--border); padding: 10px 18px; border-radius: 12px; cursor: pointer; font-weight: 600; font-size: 14px; white-space: nowrap; transition: 0.2s; }
        .tab-btn.active { background: var(--primary-gradient); color: #fff; border-color: transparent; box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3); }
        .card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; padding: 20px; margin-bottom: 20px; box-shadow: 0 4px 20px rgba(0,0,0,0.2); }
        .card h2 { font-size: 18px; margin-bottom: 16px; }
        .selector-row { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
        .pill-btn { background: var(--bg); border: 1px solid var(--border); color: var(--text-muted); padding: 8px 14px; border-radius: 8px; cursor: pointer; font-size: 13px; font-weight: 600; }
        .pill-btn.active { background: var(--primary); color: #fff; border-color: var(--primary); }
        .lesson-item { display: flex; justify-content: space-between; align-items: center; background: var(--bg); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-bottom: 10px; gap: 10px; flex-wrap: wrap; }
        .lesson-info { display: flex; flex-direction: column; gap: 4px; }
        .lesson-time { font-weight: 700; color: var(--primary); font-size: 14px; }
        .lesson-name { font-weight: 600; font-size: 15px; }
        .lesson-meta { font-size: 13px; color: var(--text-muted); }
        .type-badge { display: inline-block; padding: 2px 8px; border-radius: 6px; font-size: 11px; font-weight: 600; text-transform: uppercase; }
        .type-lek { background: rgba(59, 130, 246, 0.2); color: #60a5fa; border: 1px solid rgba(59, 130, 246, 0.4); }
        .type-pr { background: rgba(16, 185, 129, 0.2); color: #34d399; border: 1px solid rgba(16, 185, 129, 0.4); }
        textarea, input[type="text"] { width: 100%; background: var(--bg); border: 1px solid var(--border); color: var(--text); padding: 12px; border-radius: 10px; font-size: 14px; margin-bottom: 12px; outline: none; }
        textarea:focus, input[type="text"]:focus { border-color: var(--primary); }
        .btn { background: var(--primary-gradient); color: #fff; border: none; padding: 12px 20px; border-radius: 10px; font-weight: 600; cursor: pointer; font-size: 14px; display: inline-flex; align-items: center; gap: 8px; transition: 0.2s; }
        .btn-danger { background: var(--danger); padding: 6px 12px; font-size: 12px; border-radius: 6px; border: none; color: #fff; cursor: pointer; }
        #toast { position: fixed; bottom: 20px; right: 20px; background: var(--surface); border: 1px solid var(--primary); color: #fff; padding: 12px 20px; border-radius: 10px; display: none; z-index: 100; }
    </style>
</head>
<body>
    <div class="container">
        <header class="header">
            <div class="title-block">
                <h1>⚡ Админ-Панель | 7241452</h1>
                <p>Управление расписанием и оповещениями НЧИ КФУ</p>
            </div>
            <div class="stats-badge">
                <span class="badge" id="b-week">⏳ Загрузка...</span>
                <span class="badge" id="b-users">👥 Студентов: ...</span>
            </div>
        </header>

        <nav class="tabs">
            <button class="tab-btn active" onclick="switchTab('tab-schedule')">📅 Расписание</button>
            <button class="tab-btn" onclick="switchTab('tab-broadcast')">📢 Объявление</button>
            <button class="tab-btn" onclick="switchTab('tab-upload')">📁 Загрузить Excel</button>
        </nav>

        <section id="tab-schedule" class="card">
            <h2>📅 Редактирование расписания</h2>
            <div class="selector-row">
                <button class="pill-btn active" id="wn-v" onclick="selectWeek('в')">🔼 Верхняя неделя</button>
                <button class="pill-btn" id="wn-n" onclick="selectWeek('н')">🔽 Нижняя неделя</button>
            </div>
            <div class="selector-row" id="days-pills"></div>
            <div id="lessons-list"></div>
            <div style="margin-top: 16px; display: flex; gap: 10px;">
                <button class="btn" onclick="openAddLesson()">➕ Добавить пару</button>
                <button class="btn" style="background: var(--success);" onclick="saveScheduleToServer()">💾 Сохранить изменения</button>
            </div>
        </section>

        <section id="tab-broadcast" class="card" style="display: none;">
            <h2>📢 Срочное объявление одногруппникам</h2>
            <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 12px;">
                Сообщение будет мгновенно отправлено всем студентам, подписанным на бота.
            </p>
            <textarea id="broadcast-text" rows="5" placeholder="Например: Завтра первой пары не будет, препод заболел!"></textarea>
            <button class="btn" onclick="sendBroadcast()">🚀 Разослать сообщение</button>
        </section>

        <section id="tab-upload" class="card" style="display: none;">
            <h2>📁 Обновление базы из Excel (.xlsx)</h2>
            <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 14px;">
                Выберите обновленный файл расписания колледжа. Бот автоматически спарсит группу 7241452.
            </p>
            <input type="file" id="excel-file" accept=".xlsx, .xls" style="margin-bottom: 14px;">
            <br>
            <button class="btn" onclick="uploadExcel()">📤 Загрузить и обновить</button>
        </section>
    </div>

    <div id="toast"></div>

    <script>
        const urlParams = new URLSearchParams(window.location.search);
        let token = urlParams.get('token') || localStorage.getItem('admin_token') || 'kfu7241_secret_key';
        localStorage.setItem('admin_token', token);

        let currentWN = 'в';
        let currentDay = 'Понедельник';
        let scheduleData = null;
        const days = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота'];

        function showToast(msg) {
            const t = document.getElementById('toast');
            t.innerText = msg;
            t.style.display = 'block';
            setTimeout(() => { t.style.display = 'none'; }, 3000);
        }

        function switchTab(tabId) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelectorAll('.card').forEach(c => c.style.display = 'none');
            event.target.classList.add('active');
            document.getElementById(tabId).style.display = 'block';
        }

        function initDays() {
            const container = document.getElementById('days-pills');
            container.innerHTML = '';
            days.forEach(d => {
                const btn = document.createElement('button');
                btn.className = 'pill-btn' + (d === currentDay ? ' active' : '');
                btn.innerText = d;
                btn.onclick = () => {
                    currentDay = d;
                    initDays();
                    renderLessons();
                };
                container.appendChild(btn);
            });
        }

        function selectWeek(wn) {
            currentWN = wn;
            document.getElementById('wn-v').classList.toggle('active', wn === 'в');
            document.getElementById('wn-n').classList.toggle('active', wn === 'н');
            renderLessons();
        }

        async function loadStats() {
            try {
                const res = await fetch(`/api/stats?token=${token}`);
                if (!res.ok) throw new Error();
                const data = await res.json();
                document.getElementById('b-week').innerText = '⚡ ' + data.week_type;
                document.getElementById('b-users').innerText = `👥 Студентов: ${data.total_users} (пуши: ${data.subscribers})`;
            } catch(e) {
                document.getElementById('b-week').innerText = '⚡ НЧИ КФУ';
            }
        }

        async function loadSchedule() {
            try {
                const res = await fetch(`/api/schedule?token=${token}`);
                scheduleData = await res.json();
                renderLessons();
            } catch(e) {
                showToast('Ошибка загрузки');
            }
        }

        function renderLessons() {
            const list = document.getElementById('lessons-list');
            list.innerHTML = '';
            if (!scheduleData || !scheduleData[currentWN] || !scheduleData[currentWN][currentDay]) return;
            const items = scheduleData[currentWN][currentDay];
            if (items.length === 0) {
                list.innerHTML = '<div style="color: var(--text-muted); padding: 10px;">Пар нет 🎉</div>';
                return;
            }
            items.forEach((item, idx) => {
                const el = document.createElement('div');
                el.className = 'lesson-item';
                const typeClass = item.type === 'лек' ? 'type-lek' : 'type-pr';
                el.innerHTML = `
                    <div class="lesson-info">
                        <span class="lesson-time">${item.time}</span>
                        <span class="lesson-name">${item.subject} <span class="type-badge ${typeClass}">${item.type || 'пара'}</span></span>
                        <span class="lesson-meta">📍 ${item.building || ''} ${item.room ? 'ауд. ' + item.room : ''} | 👤 ${item.teacher || '—'}</span>
                    </div>
                    <div>
                        <button class="btn-danger" onclick="deleteLesson(${idx})">🗑 Удалить</button>
                    </div>
                `;
                list.appendChild(el);
            });
        }

        function deleteLesson(idx) {
            scheduleData[currentWN][currentDay].splice(idx, 1);
            renderLessons();
            showToast('Пара удалена (нажмите Сохранить)');
        }

        function openAddLesson() {
            const time = prompt('Время (например 08:30):', '08:30');
            if (!time) return;
            const subj = prompt('Название предмета:');
            if (!subj) return;
            const room = prompt('Аудитория (например 405):', '');
            const bld = prompt('Корпус (например УЛК-1):', 'УЛК-1');
            const type = prompt('Тип (лек / пр):', 'пр');
            const teacher = prompt('Преподаватель:', '');

            scheduleData[currentWN][currentDay].push({
                time: time,
                subject: subj,
                room: room,
                building: bld,
                type: type,
                teacher: teacher
            });
            renderLessons();
            showToast('Пара добавлена (нажмите Сохранить)');
        }

        async function saveScheduleToServer() {
            try {
                const res = await fetch(`/api/schedule?token=${token}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(scheduleData)
                });
                if (res.ok) showToast('✅ Расписание сохранено!');
                else showToast('Ошибка сохранения');
            } catch(e) {
                showToast('Ошибка сети');
            }
        }

        async function sendBroadcast() {
            const text = document.getElementById('broadcast-text').value.trim();
            if (!text) return alert('Введите текст!');
            if (!confirm('Отправить сообщение всем подписанным студентам?')) return;
            try {
                const res = await fetch(`/api/broadcast?token=${token}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text })
                });
                const data = await res.json();
                if (res.ok) {
                    showToast(`✅ Отправлено ${data.sent_count} студентам!`);
                    document.getElementById('broadcast-text').value = '';
                } else alert(data.error);
            } catch(e) {
                alert('Ошибка отправки');
            }
        }

        async function uploadExcel() {
            const fileInput = document.getElementById('excel-file');
            if (!fileInput.files[0]) return alert('Выберите файл!');
            const formData = new FormData();
            formData.append('file', fileInput.files[0]);

            showToast('⏳ Загрузка и парсинг...');
            try {
                const res = await fetch(`/api/upload_excel?token=${token}`, {
                    method: 'POST',
                    body: formData
                });
                if (res.ok) {
                    showToast('✅ База обновлена из Excel!');
                    await loadSchedule();
                } else alert('Ошибка разбора Excel');
            } catch(e) {
                alert('Ошибка загрузки');
            }
        }

        initDays();
        loadStats();
        loadSchedule();
    </script>
</body>
</html>"""

async def handle_index(request):
    return web.Response(text=ADMIN_HTML, content_type='text/html')

def check_token(request):
    req_token = request.query.get("token") or request.headers.get("Authorization", "").replace("Bearer ", "")
    return req_token == ADMIN_TOKEN

async def handle_api_schedule(request):
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    return web.json_response(SCHEDULE)

async def handle_api_save_schedule(request):
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
        global SCHEDULE
        save_schedule(data)
        SCHEDULE = data
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def handle_api_stats(request):
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    total, subs = get_stats()
    _, wn_name = get_week_info()
    return web.json_response({
        "total_users": total,
        "subscribers": subs,
        "week_type": wn_name,
        "group": DEFAULT_GROUP
    })

async def handle_api_broadcast(request):
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        data = await request.json()
        text = data.get("text", "").strip()
        if not text:
            return web.json_response({"error": "Текст пустой"}, status=400)
        subs = get_subscribers()
        sent = 0
        msg_formatted = f"📢 <b>Объявление от старосты / админа:</b>\n\n{text}"
        for uid, _ in subs:
            try:
                await bot.send_message(uid, msg_formatted)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                pass
        return web.json_response({"status": "ok", "sent_count": sent})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

async def handle_api_upload_excel(request):
    if not check_token(request):
        return web.json_response({"error": "Unauthorized"}, status=401)
    try:
        reader = await request.multipart()
        field = await reader.next()
        if field.name != 'file':
            return web.json_response({"error": "Файл не передан"}, status=400)
        temp_path = f"web_upload_{datetime.datetime.now().timestamp()}.xlsx"
        with open(temp_path, "wb") as f:
            while True:
                chunk = await field.read_chunk()
                if not chunk:
                    break
                f.write(chunk)
        global SCHEDULE
        new_data = parse_excel(temp_path)
        save_schedule(new_data)
        SCHEDULE = new_data
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return web.json_response({"status": "ok"})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)

def create_web_app():
    app = web.Application(client_max_size=20 * 1024 * 1024)
    app.router.add_get('/', handle_index)
    app.router.add_get('/admin', handle_index)
    app.router.add_get('/api/schedule', handle_api_schedule)
    app.router.add_post('/api/schedule', handle_api_save_schedule)
    app.router.add_get('/api/stats', handle_api_stats)
    app.router.add_post('/api/broadcast', handle_api_broadcast)
    app.router.add_post('/api/upload_excel', handle_api_upload_excel)
    return app

async def main():
    init_db()
    asyncio.create_task(morning_broadcast_worker())

    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на 0.0.0.0:{PORT}")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
