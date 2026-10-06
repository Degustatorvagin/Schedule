import asyncio
import datetime
import json
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
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
import pandas as pd

TOKEN = os.getenv("BOT_TOKEN", "8918873090:AAFL5x_T3O5yr5swc5GUJKygjUsDqDEdpZQ")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8537137900"))
GROUP_NAME = "7241452"
ANCHOR_MONDAY = datetime.date(2026, 8, 31)  # Неделя со 2 сентября 2026 была верхней
JSON_FILE = "schedule.json"

DAYS_ORDER = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота']
DAYS_MAP = {0: 'Понедельник', 1: 'Вторник', 2: 'Среда', 3: 'Четверг', 4: 'Пятница', 5: 'Суббота'}

logging.basicConfig(level=logging.INFO)
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

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
        target_date = datetime.date.today()
    weeks_diff = (target_date - ANCHOR_MONDAY).days // 7
    if weeks_diff % 2 == 0:
        return 'в', 'Верхняя 🔼'
    return 'н', 'Нижняя 🔽'

def format_day(day_name: str, wn: str, lessons: list) -> str:
    wn_label = "Верхняя неделя 🔼" if wn == 'в' else "Нижняя неделя 🔽"
    text = f"📅 <b>{day_name}</b> ({wn_label})\n━━━━━━━━━━━━━━━━━━━━\n"
    if not lessons:
        return text + "🎉 Пар нет! Отдыхаем."
    
    for idx, l in enumerate(lessons, 1):
        typ = f"({l['type']})" if l.get('type') else ""
        text += f"<b>{idx}. {l['time']}</b> — <b>{l['subject']}</b> {typ}\n"
        loc = []
        if l.get('building'):
            loc.append(l['building'])
        if l.get('room'):
            loc.append(f"ауд. {l['room']}")
        if loc:
            text += f"    📍 {', '.join(loc)}\n"
        if l.get('teacher'):
            text += f"    👤 {l['teacher']}\n"
    return text

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="➡️ Завтра")],
            [KeyboardButton(text="🔼 Верхняя неделя"), KeyboardButton(text="🔽 Нижняя неделя")],
            [KeyboardButton(text="ℹ️ Какая сейчас неделя?")]
        ],
        resize_keyboard=True
    )

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

@dp.message(CommandStart())
async def cmd_start(msg: Message):
    wn_code, wn_name = get_week_info()
    text = (
        f"👋 Привет! Это бот расписания группы <b>{GROUP_NAME}</b> (ИСиП).\n\n"
        f"⚡ Сейчас идет: <b>{wn_name}</b>\n"
        f"Используй кнопки внизу для просмотра расписания."
    )
    if msg.from_user.id == ADMIN_ID:
        text += "\n\n👑 <i>Ты администратор бота. Просто отправь файл .xlsx сюда, чтобы обновить расписание.</i>"
    await msg.answer(text, reply_markup=main_keyboard())

@dp.message(F.text == "ℹ️ Какая сейчас неделя?")
async def cmd_current_week(msg: Message):
    _, wn_name = get_week_info()
    today_str = datetime.date.today().strftime("%d.%m.%Y")
    await msg.answer(f"📆 Сегодня: <b>{today_str}</b>\n⚡ Текущая неделя: <b>{wn_name}</b>")

@dp.message(F.text == "📅 Сегодня")
async def cmd_today(msg: Message):
    today = datetime.date.today()
    if today.weekday() == 6:
        await msg.answer("🎉 Сегодня воскресенье! Занятий нет.")
        return
    wn_code, _ = get_week_info(today)
    day_name = DAYS_MAP[today.weekday()]
    lessons = SCHEDULE.get(wn_code, {}).get(day_name, [])
    await msg.answer(format_day(day_name, wn_code, lessons))

@dp.message(F.text == "➡️ Завтра")
async def cmd_tomorrow(msg: Message):
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
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

def parse_excel(file_path: str, group: str = GROUP_NAME) -> dict:
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

async def main():
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
