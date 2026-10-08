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

# Системный промпт для сжатия лекций
AI_SYSTEM_PROMPT = """Ты — университетский ИИ-ассистент НЧИ КФУ.
Твоя задача — профессионально и компактно сжимать учебные материалы, лекции и статьи.
Правила работы:
1. Выдели 3-5 ключевых тезисов без лишней 'воды'.
2. Выдели главные термины, определения и формулы, если они есть.
3. Оформи всё чёткими списками с эмодзи-маркерами (📌, 💡, ⚡).
4. Пиши строго по делу, понятно, академично, на русском языке."""

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
    return None, str(time_str), "", None, None

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
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT,
                    role TEXT,
                    action TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    auth_code TEXT PRIMARY KEY,
                    user_id INTEGER,
                    username TEXT,
                    first_name TEXT,
                    role TEXT,
                    target_name TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS user_notes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    note_key TEXT,
                    lesson_date TEXT,
                    lesson_time TEXT,
                    subject TEXT,
                    note_text TEXT,
                    remind_evening INTEGER DEFAULT 1,
                    remind_sent INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, note_key)
                )
            """)
            conn.commit()
    except Exception as e:
        logging.error(f"Ошибка БД: {e}")

def log_activity(user_id: int, username: str, role: str, action: str, details: str = ""):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO activity_logs (user_id, username, role, action, details) VALUES (?, ?, ?, ?, ?)",
                      (user_id, username or "", role, action, details))
            conn.commit()
    except Exception as e:
        logging.error(f"Ошибка логирования: {e}")

def get_user(user_id: int):
    try:
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("SELECT user_id, username, group_name, notify_morning, notify_remind, notify_hw, view_type, role, teacher_name FROM users WHERE user_id = ?", (user_id,))
            return c.fetchone()
    except Exception:
        return None

def register_user(user_id: int, username: str, role: str = "student", group_name: str = None, teacher_name: str = None):
    try:
        existing = get_user(user_id)
        saved_grp = group_name if group_name is not None else (existing[2] if existing else DEFAULT_GROUP)
        saved_teacher = teacher_name if teacher_name is not None else (existing[8] if existing else "")
        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO users (user_id, username, role, group_name, teacher_name, notify_morning, notify_remind, notify_hw, view_type)
                VALUES (?, ?, ?, ?, ?,
                    COALESCE((SELECT notify_morning FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT notify_remind FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT notify_hw FROM users WHERE user_id = ?), 1),
                    COALESCE((SELECT view_type FROM users WHERE user_id = ?), 'text')
                )
            """, (user_id, username, role, saved_grp, saved_teacher, user_id, user_id, user_id, user_id))
            conn.commit()
    except Exception as e:
        logging.error(f"Ошибка регистрации: {e}")

def load_schedule() -> dict:
    if os.path.exists(JSON_FILE):
        try:
            with open(JSON_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

SCHEDULE_DB = load_schedule()

def get_group_schedule(group_name: str) -> dict:
    data = SCHEDULE_DB.get(group_name)
    if isinstance(data, dict) and "schedule" in data:
        return data["schedule"]
    return {"в": {d: [] for d in DAYS_ORDER}, "н": {d: [] for d in DAYS_ORDER}}

def get_teacher_schedule(target_teacher: str) -> dict:
    t_sched = {"в": {d: [] for d in DAYS_ORDER}, "н": {d: [] for d in DAYS_ORDER}}
    t_clean = target_teacher.lower()
    for grp, g_data in SCHEDULE_DB.items():
        g_sched = g_data.get("schedule", {})
        for wn in ["в", "н"]:
            for day, lessons in g_sched.get(wn, {}).items():
                for l in lessons:
                    if t_clean in (l.get("teacher") or "").lower():
                        t_sched[wn][day].append({**l, "group": grp})
    for wn in ["в", "н"]:
        for day in t_sched[wn]:
            t_sched[wn][day].sort(key=lambda x: x.get("time", ""))
    return t_sched

def get_week_info(target_date: datetime.date = None):
    if target_date is None:
        target_date = datetime.datetime.now(MSK_TZ).date()
    weeks_diff = (target_date - ANCHOR_MONDAY).days // 7
    return ('в', 'Верхняя неделя 🔼') if weeks_diff % 2 == 0 else ('н', 'Нижняя неделя 🔽')

def format_day_text(day_name: str, wn_code: str, lessons: list, target_name: str = "", role: str = "student", date_str: str = "") -> str:
    wn_label = "Верхняя неделя 🔼" if wn_code == 'в' else "Нижняя неделя 🔽"
    header_title = f"📅 <b>{day_name}</b>" + (f" ({date_str})" if date_str else "") + f" — <i>{wn_label}</i>"
    who_label = f"👨‍🏫 Преподаватель: <code>{target_name}</code>" if role == "teacher" else f"👥 Группа: <code>{target_name}</code>"
    lines = [header_title, who_label, "━━━━━━━━━━━━━━━━━━━━"]

    if not lessons:
        lines.append("\n🎉 <b>В этот день пар нет! Можно отдыхать.</b>")
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
        if role == "teacher":
            lines.append(f"👥 Группа: <code>{l.get('group', '—')}</code>")
        else:
            if l.get('teacher'):
                lines.append(f"👤 <i>{l.get('teacher')}</i>")

    return "\n".join(lines).strip()

def main_keyboard(role: str = "student", target_name: str = DEFAULT_GROUP, is_admin: bool = False) -> ReplyKeyboardMarkup:
    web_url = f"{WEB_APP_URL}?role={role}&group={urllib.parse.quote(target_name)}"
    top_button = [KeyboardButton(text="⚡ Открыть расписание онлайн", web_app=WebAppInfo(url=web_url), style="success")]
    change_btn_text = "🔍 Сменить группу" if role == "student" else "🔍 Сменить преподавателя"
    rows = [
        top_button,
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📅 Завтра")],
        [KeyboardButton(text="⏱ Сейчас"), KeyboardButton(text="🗓 Неделя")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text=change_btn_text)]
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🌐 Веб-Админка")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

@dp.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    args = msg.text.split()[1] if len(msg.text.split()) > 1 else ""

    if args.startswith("auth_"):
        auth_code = args.replace("auth_", "").strip()
        user = get_user(msg.from_user.id)
        role = user[7] if user and len(user) > 7 else "student"
        target_name = user[8] if role == "teacher" else (user[2] if user else DEFAULT_GROUP)

        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT OR REPLACE INTO auth_sessions (auth_code, user_id, username, first_name, role, target_name, status)
                VALUES (?, ?, ?, ?, ?, ?, 'confirmed')
            """, (auth_code, msg.from_user.id, msg.from_user.username or "", msg.from_user.first_name or "", role, target_name))
            conn.commit()

        log_activity(msg.from_user.id, msg.from_user.username, role, "Вход через WebApp", auth_code)
        await msg.answer(
            f"✅ <b>Вход подтверждён!</b>\n\n"
            f"👤 Имя: <b>{msg.from_user.first_name}</b>\n"
            f"👥 Расписание: <code>{target_name}</code>\n\n"
            f"Вернитесь в приложение — синхронизация активирована."
        )
        return

    if args == "support":
        await msg.answer(f"💬 Служба технической поддержки: {SUPPORT_USERNAME}")
        return

    user = get_user(msg.from_user.id)
    if not user:
        register_user(msg.from_user.id, msg.from_user.username or "", role="student", group_name=DEFAULT_GROUP)
        user = get_user(msg.from_user.id)

    role = user[7]
    target_name = user[8] if role == "teacher" else user[2]
    _, wn_name = get_week_info()
    is_adm = (msg.from_user.id == ADMIN_ID)
    log_activity(msg.from_user.id, msg.from_user.username, role, "/start")

    await msg.answer(
        f"👋 <b>Добро пожаловать в сервис расписания НЧИ КФУ!</b>\n\n"
        f"👥 Текущий профиль: <b>{target_name}</b>\n"
        f"⚡ Неделя: <b>{wn_name}</b>",
        reply_markup=main_keyboard(role, target_name, is_admin=is_adm)
    )

@dp.message(F.text == "📅 Сегодня")
@dp.message(Command("today"))
async def cmd_today(msg: Message):
    user = get_user(msg.from_user.id)
    role = user[7] if user else "student"
    target_name = user[8] if role == "teacher" else (user[2] if user else DEFAULT_GROUP)
    today = datetime.datetime.now(MSK_TZ).date()
    wn_code, _ = get_week_info(today)
    day_name = DAYS_MAP[today.weekday()] if today.weekday() < 6 else 'Понедельник'
    sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    log_activity(msg.from_user.id, msg.from_user.username, role, "📅 Сегодня")
    await msg.answer(format_day_text(day_name, wn_code, lessons, target_name, role, today.strftime('%d.%m.%Y')))

@dp.message(F.text == "📅 Завтра")
@dp.message(Command("tomorrow"))
async def cmd_tomorrow(msg: Message):
    user = get_user(msg.from_user.id)
    role = user[7] if user else "student"
    target_name = user[8] if role == "teacher" else (user[2] if user else DEFAULT_GROUP)
    tom = datetime.datetime.now(MSK_TZ).date() + datetime.timedelta(days=1)
    wn_code, _ = get_week_info(tom)
    day_name = DAYS_MAP[tom.weekday()] if tom.weekday() < 6 else 'Понедельник'
    sched = get_teacher_schedule(target_name) if role == "teacher" else get_group_schedule(target_name)
    lessons = sched.get(wn_code, {}).get(day_name, [])
    log_activity(msg.from_user.id, msg.from_user.username, role, "📅 Завтра")
    await msg.answer(format_day_text(day_name, wn_code, lessons, target_name, role, tom.strftime('%d.%m.%Y')))

# --- API И РАБОТА С НЕЙРОСЕТЬЮ (GROQ API) ---
def cors_response(data: dict):
    return web.json_response(data, headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
    })

async def handle_options(request):
    return web.Response(headers={
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type"
    })

async def handle_api_auth_poll(request):
    code = request.query.get('code', '')
    if not code:
        return cors_response({"status": "error"})
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT user_id, username, first_name, role, target_name, status FROM auth_sessions WHERE auth_code = ?", (code,))
        row = c.fetchone()
    if row and row[5] == 'confirmed':
        return cors_response({
            "status": "confirmed",
            "user_id": row[0],
            "username": row[1],
            "first_name": row[2],
            "role": row[3],
            "target": row[4]
        })
    return cors_response({"status": "pending"})

# ЭНДПОИНТ РЕАЛЬНОГО ИИ (С ПОДРОБНЫМ ЛОГИРОВАНИЕМ В КОНСОЛЬ)
async def handle_api_ai_compress(request):
    try:
        data = await request.json()
        raw_text = data.get("text", "").strip()
        custom_task = data.get("task", "summary")

        if not raw_text:
            return cors_response({"status": "error", "message": "Вставьте текст для обработки"})

        # Динамическое считывание ключа из переменных Bothost
        raw_env_key = os.getenv("GROQ_API_KEY", "")
        active_key = raw_env_key.strip()

        logging.info(f"Запрос к ИИ. Длина ключа: {len(active_key)}, префикс: {active_key[:4] if active_key else 'НЕТ'}")

        if active_key:
            instruction = AI_SYSTEM_PROMPT
            if custom_task == 'cards':
                instruction += "\nСделай из этого текста шпаргалку в формате 'Вопрос — Краткий ответ'."
            elif custom_task == 'simple':
                instruction += "\nОбъясни смысл этого текста максимально простыми словами, на бытовых аналогиях."

            payload = {
                "model": "llama-3.1-8b-instant",
                "messages": [
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": f"Материал для обработки:\n{raw_text}"}
                ],
                "temperature": 0.3,
                "max_tokens": 1400
            }

            async with ClientSession() as session:
                async with session.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {active_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload,
                    timeout=25
                ) as resp:
                    resp_text = await resp.text()
                    logging.info(f"Ответ от Groq API: статус {resp.status}")

                    if resp.status == 200:
                        res_json = json.loads(resp_text)
                        ai_text = res_json['choices'][0]['message']['content']
                        return cors_response({"status": "ok", "result": ai_text})
                    else:
                        logging.error(f"Ошибка вызова Groq: {resp_text}")
                        return cors_response({
                            "status": "error",
                            "message": f"Ошибка нейросети Groq ({resp.status}). Проверьте правильность ключа."
                        })

        # Если ключ действительно пустой или отсутствует в Bothost
        sentences = [s.strip() for s in re.split(r'[.!?]\s+', raw_text) if len(s.strip()) > 5]
        top_sentences = sentences[:5] if len(sentences) >= 5 else sentences
        fallback_text = "📌 <b>Главные тезисы материала:</b>\n\n" + "\n".join([f"• {s}." for s in top_sentences])
        fallback_text += "\n\n<i>(Сервер не обнаружил переменную GROQ_API_KEY. Добавьте её в настройках Bothost и перезапустите бота)</i>"
        return cors_response({"status": "ok", "result": fallback_text})

    except Exception as e:
        logging.error(f"Сбой в handle_api_ai_compress: {e}")
        return cors_response({"status": "error", "message": str(e)})

async def handle_api_save_note(request):
    try:
        data = await request.json()
        uid = int(data.get("user_id", 0))
        n_key = data.get("note_key", "")
        n_text = data.get("note_text", "").strip()
        l_subj = data.get("subject", "")
        l_time = data.get("time", "")
        l_date = data.get("date", "")
        remind = int(data.get("remind", 1))

        if not uid or not n_key:
            return cors_response({"status": "error", "message": "Missing fields"})

        with sqlite3.connect(DB_FILE) as conn:
            c = conn.cursor()
            if n_text:
                c.execute("""
                    INSERT OR REPLACE INTO user_notes (user_id, note_key, lesson_date, lesson_time, subject, note_text, remind_evening, remind_sent)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                """, (uid, n_key, l_date, l_time, l_subj, n_text, remind))
            else:
                c.execute("DELETE FROM user_notes WHERE user_id = ? AND note_key = ?", (uid, n_key))
            conn.commit()

        return cors_response({"status": "ok"})
    except Exception as e:
        return cors_response({"status": "error", "error": str(e)})

async def handle_admin_dashboard(request):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT count(*) FROM users")
        total_u = c.fetchone()[0] or 0
        c.execute("SELECT count(*) FROM user_notes")
        total_notes = c.fetchone()[0] or 0
    return web.Response(
        text=f"<h1>Панель Bothost</h1><p>Пользователей: {total_u} · Заметок: {total_notes}</p>",
        content_type='text/html'
    )

def create_web_app():
    app = web.Application()
    app.router.add_get('/', handle_admin_dashboard)
    app.router.add_get('/admin', handle_admin_dashboard)
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
        logging.info(f"Сервер Bothost запущен на 0.0.0.0:{PORT}")
    except Exception as e:
        logging.warning(f"Ошибка порта: {e}")

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
