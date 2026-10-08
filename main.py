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
3. Форматируй списки без использования двойных звездочек (**), используй дефисы и эмодзи (📌, 💡, ⚡).
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
    return None, time_str, "", None, None

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
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", os.path.join(DATA_DIR, "font.ttf"), "font.ttf"]:
        if os.path.exists(p):
            try: return ImageFont.truetype(p, size)
            except Exception: pass
    return None

def init_db():
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY, username TEXT, group_name TEXT DEFAULT '7241452',
                notify_morning INTEGER DEFAULT 1, notify_remind INTEGER DEFAULT 1, notify_hw INTEGER DEFAULT 1,
                view_type TEXT DEFAULT 'text', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS auth_sessions (
                auth_code TEXT PRIMARY KEY, user_id INTEGER, username TEXT, first_name TEXT,
                group_name TEXT, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
            c.execute("""CREATE TABLE IF NOT EXISTS user_notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, note_key TEXT, note_text TEXT,
                UNIQUE(user_id, note_key))""")
            conn.commit()
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
            c.execute("""INSERT OR REPLACE INTO users (user_id, username, group_name, notify_morning, notify_remind, notify_hw, view_type)
                VALUES (?, ?, ?, COALESCE((SELECT notify_morning FROM users WHERE user_id = ?), 1),
                COALESCE((SELECT notify_remind FROM users WHERE user_id = ?), 1),
                COALESCE((SELECT notify_hw FROM users WHERE user_id = ?), 1),
                COALESCE((SELECT view_type FROM users WHERE user_id = ?), 'text'))""",
                (user_id, username, group_name, user_id, user_id, user_id, user_id))
            conn.commit()
    except Exception as e: logging.error(e)

def update_user_field(user_id: int, field: str, value):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id))
            conn.commit()
    except Exception: pass

def get_subscribers(field: str = "notify_morning"):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute(f"SELECT user_id, group_name FROM users WHERE {field} = 1")
            return c.fetchall()
    except Exception: return []

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
        if clean_q == re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower(): return grp
    for grp in SCHEDULE_DB.keys():
        if clean_q in re.sub(r'[^a-zA-Z0-9а-яА-Я]', '', grp).lower(): return grp
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

    for i, l in enumerate(lessons):
        start_str = l.get('time', '')[:5]
        slot, s_str, e_str, _, _ = get_slot_info(start_str)
        t_range = f"{s_str} – {e_str}" if e_str else start_str
        typ = normalize_type(l.get('type', ''))
        lines.append(f"\n⏰ <b>{t_range}</b> | <b>{typ}</b>")
        lines.append(f"📘 <b>{l.get('subject')}</b>")
        if l.get('building') or l.get('room'):
            lines.append(f"📍 {l.get('building', '')}, ауд. <b>{l.get('room', '')}</b>")
        if l.get('teacher'):
            lines.append(f"👤 <i>{l.get('teacher')}</i>")
    return "\n".join(lines).strip()

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

bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    args = msg.text.split()[1] if len(msg.text.split()) > 1 else ""

    if args.startswith("auth_"):
        auth_code = args.replace("auth_", "").strip()
        user = get_user(msg.from_user.id)
        grp = user[2] if user else DEFAULT_GROUP
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""INSERT OR REPLACE INTO auth_sessions (auth_code, user_id, username, first_name, group_name, status)
                VALUES (?, ?, ?, ?, ?, 'confirmed')""", (auth_code, msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "", grp))
            conn.commit()
        await msg.answer(f"✅ <b>Вход подтверждён, {msg.from_user.first_name}!</b>\nВернитесь в приложение.")
        return

    user = get_user(msg.from_user.id)
    if not user:
        await state.set_state(Form.waiting_for_group)
        await msg.answer("👋 <b>Добро пожаловать в бот НЧИ КФУ!</b>\n\nНапиши номер своей группы:")
        return

    _, wn_name = get_week_info()
    await msg.answer(f"👋 С возвращением! Группа: <code>{user[2]}</code>\n⚡ Неделя: <b>{wn_name}</b>", reply_markup=main_keyboard(user[2], is_admin=(msg.from_user.id == ADMIN_ID)))

@dp.message(Form.waiting_for_group)
async def process_custom_group(msg: Message, state: FSMContext):
    matched = find_group(msg.text.strip()) or msg.text.strip()
    register_user(msg.from_user.id, msg.from_user.username or "", matched)
    await state.clear()
    _, wn_name = get_week_info()
    await msg.answer(f"✅ Установлена группа: <b>{matched}</b>\n⚡ Неделя: <b>{wn_name}</b>", reply_markup=main_keyboard(matched, is_admin=(msg.from_user.id == ADMIN_ID)))

@dp.message(F.text == "📅 Сегодня")
async def cmd_today(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    today = datetime.datetime.now(MSK_TZ).date()
    wn_code, _ = get_week_info(today)
    lessons = get_group_schedule(grp).get(wn_code, {}).get(DAYS_MAP[today.weekday()] if today.weekday() < 6 else 'Понедельник', [])
    await msg.answer(format_day_text("Сегодня", wn_code, lessons, grp, today.strftime('%d.%m.%Y')))

@dp.message(F.text == "📅 Завтра")
async def cmd_tomorrow(msg: Message):
    user = get_user(msg.from_user.id)
    grp = user[2] if user else DEFAULT_GROUP
    tom = datetime.datetime.now(MSK_TZ).date() + datetime.timedelta(days=1)
    wn_code, _ = get_week_info(tom)
    lessons = get_group_schedule(grp).get(wn_code, {}).get(DAYS_MAP[tom.weekday()] if tom.weekday() < 6 else 'Понедельник', [])
    await msg.answer(format_day_text("Завтра", wn_code, lessons, grp, tom.strftime('%d.%m.%Y')))

@dp.message(F.text.contains("Сменить группу"))
async def cmd_change_group(msg: Message, state: FSMContext):
    await state.set_state(Form.waiting_for_group)
    await msg.answer("✍️ Напиши номер новой группы:")

# --- API ИИ И СИНХРОНИЗАЦИИ ---
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
            if custom_task == 'cards': instruction += "\nСделай шпаргалку Вопрос-Ответ."
            elif custom_task == 'simple': instruction += "\nОбъясни простыми словами."

            payload = {
                "model": "openai/gpt-oss-120b",
                "messages": [{"role": "system", "content": instruction}, {"role": "user", "content": f"Текст:\n{raw_text}"}],
                "temperature": 0.3, "max_tokens": 1200
            }
            async with ClientSession() as session:
                async with session.post("https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": f"Bearer {active_key}", "Content-Type": "application/json"}, json=payload, timeout=25) as resp:
                    if resp.status == 200:
                        res_json = await resp.json()
                        return cors_response({"status": "ok", "result": res_json['choices'][0]['message']['content']})

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
    app = create_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    try:
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
    except Exception: pass

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
