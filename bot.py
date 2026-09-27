import asyncio
import datetime
import html
import os
import re
import time
import urllib.parse
from bs4 import BeautifulSoup
import httpx
from curl_cffi.requests import AsyncSession as CurlAsyncSession
from playwright.async_api import async_playwright
import aiosqlite

from seed_db import seed_if_needed

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Токен бота и путь к БД берём из переменных окружения (см. Railway → Variables),
# со значением по умолчанию для локального запуска на ПК.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8849901584:AAHHxn_5yFLW4EdzaCAWj2YnadgBNEHblZU")
TARGET_GROUP = "6111"
SITE_URL = "https://bseumtc.by/raspisanie"
TRUE_SITE_URL = "https://bseumtc.by/uchashhimsya/raspisanie-zanyatij/"
TIMEZONE = datetime.timezone(datetime.timedelta(hours=3))
DB_PATH = os.environ.get("DB_PATH", "bot_database.db")

# Точный список предметов
SUBJECTS_LIST = [
    "ТехнПригПищи",
    "ОснИнжГрафики",
    "ОсобДеятПрООП",
    "ИстБелГос",
    "Физкультура",
    "Микробиология",
    "ОхранаТруда",
    "АналитХимия",
    "ТоварПищПрод"
]

DAYS_RU = {
    0: "Понедельник",
    1: "Вторник",
    2: "Среда",
    3: "Четверг",
    4: "Пятница",
    5: "Суббота",
    6: "Воскресенье",
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler(timezone="Europe/Minsk")
subscribed_users = set()


# === FSM СОСТОЯНИЯ ===
class GradeStates(StatesGroup):
    selecting_subject = State()
    selecting_grade = State()


class HomeworkStates(StatesGroup):
    selecting_subject = State()
    entering_task = State()
    selecting_due_date = State()
    editing_task = State()


CALL_SCHEDULE = [
    {"pair": 1, "h1_start": datetime.time(8, 20), "h1_end": datetime.time(9, 5), "h2_start": datetime.time(9, 15),
     "h2_end": datetime.time(10, 0), "full_start": datetime.time(8, 20), "full_end": datetime.time(10, 0)},
    {"pair": 2, "h1_start": datetime.time(10, 10), "h1_end": datetime.time(10, 55), "h2_start": datetime.time(11, 5),
     "h2_end": datetime.time(11, 50), "full_start": datetime.time(10, 10), "full_end": datetime.time(11, 50)},
    {"pair": 3, "h1_start": datetime.time(12, 0), "h1_end": datetime.time(12, 45), "h2_start": datetime.time(13, 15),
     "h2_end": datetime.time(14, 0), "full_start": datetime.time(12, 0), "full_end": datetime.time(14, 0)},
    {"pair": 4, "h1_start": datetime.time(14, 10), "h1_end": datetime.time(14, 55), "h2_start": datetime.time(15, 0),
     "h2_end": datetime.time(15, 45), "full_start": datetime.time(14, 10), "full_end": datetime.time(15, 45)},
    {"pair": 5, "h1_start": datetime.time(15, 55), "h1_end": datetime.time(16, 40), "h2_start": datetime.time(16, 45),
     "h2_end": datetime.time(17, 30), "full_start": datetime.time(15, 55), "full_end": datetime.time(17, 30)},
    {"pair": 6, "h1_start": datetime.time(17, 40), "h1_end": datetime.time(18, 25), "h2_start": datetime.time(18, 30),
     "h2_end": datetime.time(19, 15), "full_start": datetime.time(17, 40), "full_end": datetime.time(19, 15)},
    {"pair": 7, "h1_start": datetime.time(19, 25), "h1_end": datetime.time(20, 10), "h2_start": datetime.time(20, 15),
     "h2_end": datetime.time(21, 0), "full_start": datetime.time(19, 25), "full_end": datetime.time(21, 0)},
]


# === БАЗА ДАННЫХ ===
async def init_db():
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS grades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                grade INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS homeworks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                subject TEXT NOT NULL,
                task TEXT NOT NULL,
                due_date TEXT DEFAULT 'Не указан',
                is_done INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()


# --- CRUD для оценок ---
async def add_grade_db(user_id: int, subject: str, grade: int):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO grades (user_id, subject, grade) VALUES (?, ?, ?)",
            (user_id, subject, grade),
        )
        await db.commit()


async def get_user_grades_db(user_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        async with db.execute(
                "SELECT subject, grade FROM grades WHERE user_id = ?", (user_id,)
        ) as cursor:
            return await cursor.fetchall()


async def clear_user_grades_db(user_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute("DELETE FROM grades WHERE user_id = ?", (user_id,))
        await db.commit()


# --- CRUD для домашних заданий ---
async def add_homework_db(user_id: int, subject: str, task: str, due_date: str):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute(
            "INSERT INTO homeworks (user_id, subject, task, due_date) VALUES (?, ?, ?, ?)",
            (user_id, subject, task, due_date)
        )
        await db.commit()


async def get_user_homeworks_db(user_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        async with db.execute(
                "SELECT id, subject, task, due_date, is_done FROM homeworks WHERE user_id = ? ORDER BY id DESC",
                (user_id,)
        ) as cursor:
            return await cursor.fetchall()


async def get_homework_by_id_db(hw_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        async with db.execute(
                "SELECT id, subject, task, due_date, is_done FROM homeworks WHERE id = ?", (hw_id,)
        ) as cursor:
            return await cursor.fetchone()


async def toggle_homework_status_db(hw_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute(
            "UPDATE homeworks SET is_done = CASE WHEN is_done = 1 THEN 0 ELSE 1 END WHERE id = ?",
            (hw_id,)
        )
        await db.commit()


async def update_homework_task_db(hw_id: int, new_task: str):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute("UPDATE homeworks SET task = ? WHERE id = ?", (new_task, hw_id))
        await db.commit()


async def delete_homework_db(hw_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute("DELETE FROM homeworks WHERE id = ?", (hw_id,))
        await db.commit()


async def clear_user_homeworks_db(user_id: int):
    async with aiosqlite.connect(DB_PATH, timeout=10.0) as db:
        await db.execute("DELETE FROM homeworks WHERE user_id = ?", (user_id,))
        await db.commit()


# === КЛАВИАТУРЫ ===
def get_bottom_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Куда мне идти?")],
            [KeyboardButton(text="📋 Меню"), KeyboardButton(text="📊 Успеваемость"), KeyboardButton(text="📝 ДЗ")],
        ],
        resize_keyboard=True,
    )


def get_main_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔔 Расписание звонков", callback_data="calls")],
            [InlineKeyboardButton(text="📅 Расписание занятий", callback_data="select_date")],
            [InlineKeyboardButton(text="🌐 Открыть сайт", url=TRUE_SITE_URL)],
        ]
    )


def get_subjects_keyboard(prefix: str = "set_subj") -> InlineKeyboardMarkup:
    buttons = [[InlineKeyboardButton(text=subj, callback_data=f"{prefix}:{subj}")] for subj in SUBJECTS_LIST]
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_grades_keyboard() -> InlineKeyboardMarkup:
    row1 = [InlineKeyboardButton(text=str(i), callback_data=f"set_num:{i}") for i in range(1, 6)]
    row2 = [InlineKeyboardButton(text=str(i), callback_data=f"set_num:{i}") for i in range(6, 11)]
    cancel_btn = [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]
    return InlineKeyboardMarkup(inline_keyboard=[row1, row2, cancel_btn])


def get_due_dates_keyboard() -> InlineKeyboardMarkup:
    today = datetime.datetime.now(TIMEZONE).date()
    tomorrow = today + datetime.timedelta(days=1)
    next_week = today + datetime.timedelta(days=7)

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"На завтра ({tomorrow.strftime('%d.%m')})",
                                  callback_data=f"set_date:{tomorrow.strftime('%d.%m')}")],
            [InlineKeyboardButton(text=f"Через неделю ({next_week.strftime('%d.%m')})",
                                  callback_data=f"set_date:{next_week.strftime('%d.%m')}")],
            [InlineKeyboardButton(text="Без срока", callback_data="set_date:Без срока")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]
        ]
    )


# === ОЧИСТКА И ПАРСИНГ ТЕКСТА ===
def clean_table_artifacts(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"[├┼┴┬┤┌┐└┘─│\-\|]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def clean_and_fix_lesson_text(text: str) -> str:
    if not text:
        return ""
    text = clean_table_artifacts(text)
    text = re.sub(r"^\d+\s*[\.\)]?\s*", "", text)
    text = re.sub(r"^\d+\s+", "", text)

    text = re.sub(r"(?:Фа)?Физ[Кк]ул(?:Издор)?\.?", "Физкультура", text, flags=re.IGNORECASE)
    text = re.sub(r"КураторскийЧас", "Кураторский Час", text, flags=re.IGNORECASE)

    for subj in SUBJECTS_LIST:
        pattern = re.compile(re.escape(subj), re.IGNORECASE)
        if pattern.search(text):
            text = pattern.sub(subj, text)

    words = text.split()
    dedup_words = []
    for w in words:
        if not dedup_words or dedup_words[-1].lower() != w.lower():
            dedup_words.append(w)
    text = " ".join(dedup_words)

    return text.strip()


def parse_lesson_details(lesson_str: str):
    if not lesson_str or lesson_str == "Информация отсутствует":
        return "Информация отсутствует", "Не указан", "Не указан"

    text = clean_and_fix_lesson_text(lesson_str)

    cabinets = []
    match_cabs = re.findall(r"(?:ауд\.?|каб\.?|кабинет|аудитория)\s*(\d+[а-яА-Яa-zA-Z]?)", text, re.IGNORECASE)
    if match_cabs:
        cabinets.extend(match_cabs)
    else:
        match_cab_nums = re.findall(r"\b(\d{3,4}[а-яА-Яa-zA-Z]?)\b", text)
        if match_cab_nums:
            cabinets.extend(match_cab_nums)
        elif "физк" in text.lower() or "спорт" in text.lower() or "зал" in text.lower():
            cabinets.append("Зал")

    cabinets = list(dict.fromkeys(cabinets))
    cabinet_str = " / ".join(cabinets) if cabinets else "Не указан"

    teachers = re.findall(r"([А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.|\s+[А-ЯЁ]\.))", text)
    teachers = list(dict.fromkeys([t.strip() for t in teachers]))
    teacher_str = " / ".join(teachers) if teachers else "Не указан"

    matched_subject = text
    for subj in SUBJECTS_LIST:
        if subj.lower() in text.lower():
            matched_subject = subj
            break

    if matched_subject == text:
        clean_subj = text
        for cab in cabinets:
            clean_subj = re.sub(rf"\b{re.escape(cab)}\b", "", clean_subj)
        for teach in teachers:
            # Обычный .replace(), а не regex с \b: ФИО обычно заканчивается точкой
            # (инициалы), а после неё нет "границы слова" — \b там не срабатывал,
            # и препод не вырезался из названия предмета.
            clean_subj = clean_subj.replace(teach, "")

        clean_subj = re.sub(r"(?:ауд\.?|каб\.?|кабинет|аудитория|\bзал\b|\bс/зал\b)", "", clean_subj,
                            flags=re.IGNORECASE)
        clean_subj = re.sub(r"\s+", " ", clean_subj).strip(" /-")
        matched_subject = clean_subj if clean_subj else text

    return matched_subject, cabinet_str, teacher_str


def parse_date_obj(date_str: str):
    match = re.search(r"(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?", date_str)
    if not match:
        return None
    day = int(match.group(1))
    month = int(match.group(2))
    year = int(match.group(3)) if match.group(3) else datetime.datetime.now(TIMEZONE).year
    if year < 100:
        year += 2000
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def format_date_with_weekday(date_str: str) -> str:
    dt = parse_date_obj(date_str)
    if not dt:
        return date_str
    weekday_name = DAYS_RU.get(dt.weekday(), "")
    return f"{dt.day:02d}.{dt.month:02d} ({weekday_name})"


def get_calls_text() -> str:
    now = datetime.datetime.now(TIMEZONE).time()
    lines = ["🔔 <b>Расписание звонков:</b>\n"]
    lunch_start = datetime.time(12, 45)
    lunch_end = datetime.time(13, 15)

    for item in CALL_SCHEDULE:
        p_num = item["pair"]
        h1_s = item["h1_start"].strftime("%H:%M")
        h1_e = item["h1_end"].strftime("%H:%M")
        h2_s = item["h2_start"].strftime("%H:%M")
        h2_e = item["h2_end"].strftime("%H:%M")
        status = ""

        if item["h1_start"] <= now <= item["h1_end"]:
            status = " 🔥 <i>(Идет 1-й час)</i>"
        elif item["h1_end"] < now < item["h2_start"]:
            if p_num == 3 and lunch_start <= now <= lunch_end:
                status = " 🥗 <i>(ИДЕТ ОБЕД)</i>"
            else:
                status = " ⏸ <i>(Перерыв)</i>"
        elif item["h2_start"] <= now <= item["h2_end"]:
            status = " 🔥 <i>(Идет 2-й час)</i>"

        lines.append(f"<b>{p_num} пара:</b> {h1_s}–{h1_e} | {h2_s}–{h2_e}{status}")

        if p_num == 3:
            lunch_status = " 🥗 <i>(ИДЕТ ОБЕД)</i>" if lunch_start <= now <= lunch_end else ""
            lines.append(f"🍱 <b>Обед:</b> 12:45 – 13:15{lunch_status}\n")

    return "\n".join(lines)


def parse_word_plaintext_schedule(soup, group_num: str):
    paragraphs = soup.find_all(["p", "div"], class_=re.compile(r"MsoPlainText", re.I))
    if not paragraphs:
        paragraphs = soup.find_all(["p", "div"])

    is_our_group_block = False
    raw_lines = []

    for p in paragraphs:
        raw_text = html.unescape(p.get_text(strip=True))
        text = clean_table_artifacts(raw_text)
        if not text or len(text) < 2:
            continue
        parts = [part.strip() for part in text.split(" ") if part.strip()]
        if not parts:
            continue

        if parts[0] == group_num:
            is_our_group_block = True
            lesson_data = " ".join(parts[1:])
            if lesson_data:
                raw_lines.append(lesson_data)
            continue
        elif is_our_group_block and parts[0].isdigit() and len(parts[0]) == 4 and parts[0] != group_num:
            is_our_group_block = False
            break
        elif is_our_group_block:
            lesson_data = " ".join(parts)
            if "mso-" not in lesson_data and "font-signature" not in lesson_data:
                raw_lines.append(lesson_data)

    schedule_by_pair = {}
    current_pair_idx = None

    for line in raw_lines:
        match_pair = re.match(r"^([1-6])\s*[\.\)]?\s*(.*)", line)
        if match_pair:
            current_pair_idx = int(match_pair.group(1))
            content = match_pair.group(2)
        else:
            content = line

        cleaned_content = clean_and_fix_lesson_text(content)
        if not cleaned_content:
            continue

        if current_pair_idx is not None:
            if current_pair_idx in schedule_by_pair:
                existing = schedule_by_pair[current_pair_idx]
                if cleaned_content.lower() not in existing.lower():
                    schedule_by_pair[current_pair_idx] += f" / {cleaned_content}"
            else:
                schedule_by_pair[current_pair_idx] = cleaned_content

    final_lessons = []
    max_pair = max(schedule_by_pair.keys()) if schedule_by_pair else 0
    for i in range(1, max_pair + 1):
        final_lessons.append(schedule_by_pair.get(i, ""))

    return final_lessons


# === ОБХОД АНТИДУДОС-ЗАЩИТЫ САЙТА / ПРОБЛЕМ С МАРШРУТОМ ДО САЙТА ===
# Если сайт недоступен напрямую с твоего IP (провайдер режет трафик), укажи здесь
# адрес прокси/VPN, через который ходить ТОЛЬКО ботом — например:
#   "http://127.0.0.1:10809"   (локальный прокси от VPN-клиента)
#   "socks5://127.0.0.1:1080"  (SOCKS5 от VPN-клиента)
# Оставь пустой строкой "", если прокси не нужен.
PROXY_URL = ""

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://bseumtc.by/",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}


def _looks_like_antibot_page(raw_text: str) -> bool:
    """Проверяет, не является ли ответ заглушкой антидудос-защиты вместо реального контента."""
    lowered = raw_text.lower()
    markers = [
        "ddos-guard", "ddos_guard", "checking your browser", "just a moment",
        "attention required", "cf-browser-verification", "captcha", "enable javascript",
        "включите javascript", "проверка браузера", "please wait while your request",
        "one moment, please",
    ]
    if any(m in lowered for m in markers):
        return True
    # Настоящая страница расписания всегда содержит ссылки на файлы расписания
    if "schedule/public/rasp" not in lowered and len(raw_text) < 20000:
        return True
    return False


# Кэш ответов сайта: расписание и список дат не меняются каждую минуту, а фоновые задачи
# дёргают fetch_protected_html() каждые ~60 секунд — без кэша это на каждый тик заново
# проходит проверку антибота (а иногда и заново поднимает браузер). TTL — сколько секунд
# держим уже скачанный HTML, прежде чем идти на сайт заново.
_HTML_CACHE: dict[str, tuple[float, str]] = {}
HTML_CACHE_TTL = 240  # 4 минуты

# Cookie, которые сайт выдаёт после успешного прохождения JS-проверки браузером.
# Пока они не протухли, быстрый способ (curl_cffi) может подставлять их и часто
# вообще не спотыкаться об антибот — тогда браузер не нужен вовсе.
_verified_cookies: dict | None = None
_cookies_ts = 0.0
COOKIES_TTL = 1500  # ~25 минут


async def _fetch_via_curl_cffi(url: str) -> str:
    """Способ 1 (быстрый): запрос с TLS/JA3-отпечатком настоящего Chrome + (если есть) уже
    подтверждённые антибот-cookie. Обходит антидудос-фильтры без запуска браузера."""
    proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
    cookies = None
    if _verified_cookies and (time.monotonic() - _cookies_ts) < COOKIES_TTL:
        cookies = _verified_cookies
    async with CurlAsyncSession(
        headers=BROWSER_HEADERS, impersonate="chrome124", timeout=15,
        proxies=proxies, cookies=cookies,
    ) as session:
        res = await session.get(url)
        return res.content.decode("windows-1251", errors="ignore")


# JS-код, который подставляем в страницу ДО загрузки сайта, чтобы скрыть признаки автоматизации,
# которые ищет антибот-скрипт сайта (navigator.webdriver, нулевые размеры окна и т.д.)
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth});
Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight});
"""


# Держим один запущенный Chromium на всё время работы бота вместо того, чтобы поднимать
# новый процесс браузера на каждый вызов — запуск браузера сам по себе занимает пару секунд,
# и при фоновых проверках каждую минуту это быстро накапливается.
_pw = None
_pw_browser = None
_pw_lock = asyncio.Lock()


async def _get_browser():
    global _pw, _pw_browser
    async with _pw_lock:
        if _pw_browser is None:
            _pw = await async_playwright().start()
            launch_kwargs = {
                "headless": True,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    # Снижаем потребление памяти/CPU — важно на маленьких инстансах
                    # (например, бесплатный план Railway с ограничением по RAM).
                    "--disable-gpu",
                    "--disable-dev-shm-usage",  # не использовать /dev/shm (там мало места в контейнерах)
                    "--no-sandbox",  # безопасно внутри изолированного docker-контейнера
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-default-apps",
                    "--disable-sync",
                    "--mute-audio",
                    "--js-flags=--max-old-space-size=128",  # ограничиваем память JS-движка
                ],
            }
            if PROXY_URL:
                launch_kwargs["proxy"] = {"server": PROXY_URL}
            _pw_browser = await _pw.chromium.launch(**launch_kwargs)
        return _pw_browser


async def _fetch_via_browser(url: str) -> str:
    """Способ 2 (запасной, тяжелее): сайт требует пройти JS-проверку 'вы не бот'
    (смотрит на navigator.webdriver, размеры окна, User-Agent). Открываем страницу в Chromium
    через Playwright, маскируем признаки автоматизации и ждём, пока проверка сама себя пропустит
    (страница у них перезагружается каждые 5 секунд, пока проверка не пройдена)."""
    browser = await _get_browser()
    context = await browser.new_context(
        user_agent=BROWSER_HEADERS["User-Agent"],
        locale="ru-RU",
        viewport={"width": 1366, "height": 768},
    )
    global _verified_cookies, _cookies_ts
    try:
        await context.add_init_script(STEALTH_INIT_SCRIPT)
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        # Если проверка проходит сразу (обычный случай) — сайт отдаёт настоящий контент
        # почти сразу, без ожидания их 5-секундного цикла перезагрузки. Поэтому сначала
        # короткие паузы, и только если не помогло — ждём подольше.
        html_content = await page.content()
        for delay_ms in (600, 1200, 2500, 4500):  # суммарно ~9 секунд в худшем случае
            if not _looks_like_antibot_page(html_content):
                break
            await page.wait_for_timeout(delay_ms)
            html_content = await page.content()

        if not _looks_like_antibot_page(html_content):
            # Проверка пройдена — сохраняем cookie, чтобы следующие запросы могли
            # обойтись быстрым способом и не поднимать браузер вообще.
            try:
                raw_cookies = await context.cookies()
                _verified_cookies = {c["name"]: c["value"] for c in raw_cookies}
                _cookies_ts = time.monotonic()
            except Exception:
                pass

        return html_content
    finally:
        await context.close()  # закрываем только вкладку/контекст, браузер остаётся жить


async def fetch_protected_html(url: str) -> str:
    """Получает HTML страницы, обходя антидудос-защиту сайта: сперва пробует лёгкий способ
    (подмена TLS-отпечатка), и только если сайт всё равно отдаёт заглушку — включает браузер.
    Результат кэшируется на HTML_CACHE_TTL секунд, чтобы фоновые проверки каждую минуту
    не долбили сайт заново, а брали уже готовый ответ."""
    cached = _HTML_CACHE.get(url)
    if cached and (time.monotonic() - cached[0]) < HTML_CACHE_TTL:
        return cached[1]

    try:
        raw_text = await _fetch_via_curl_cffi(url)
        if not _looks_like_antibot_page(raw_text):
            _HTML_CACHE[url] = (time.monotonic(), raw_text)
            return raw_text
    except Exception as e:
        print(f"curl_cffi запрос не удался, пробуем через headless-браузер: {e}")

    html_content = await _fetch_via_browser(url)
    _HTML_CACHE[url] = (time.monotonic(), html_content)
    return html_content


async def fetch_available_dates():
    try:
        raw_text = await fetch_protected_html(SITE_URL)
        links = re.findall(
            r"https://bseumtc\.by/schedule/public/rasp/[^;\r\n\"]+\.htm",
            raw_text,
            re.IGNORECASE,
        )
        if not links:
            return {}
        unique_links = list(dict.fromkeys(links))
        date_map = {}
        for link in unique_links:
            file_name = urllib.parse.unquote(link.split("/")[-1])
            if any(x in file_name.upper() for x in ["SETKA", "ZVON", "ZVAN", "RASP"]):
                continue
            match = re.search(r"(\d{2}[\.\-_]\d{2}(?:[\.\-_]\d{2,4})?)", file_name)
            if match:
                clean_date = match.group(1).replace("-", ".").replace("_", ".")
                date_map[clean_date] = link
            else:
                clean_title = file_name.replace(".htm", "").replace("PODNAM", "").strip()
                date_map[clean_title] = link

        today = datetime.datetime.now(TIMEZONE).date()
        parsed_dates = []
        for d_str, link in date_map.items():
            dt = parse_date_obj(d_str)
            if dt:
                parsed_dates.append((dt, d_str, link))

        past_dates = [item for item in parsed_dates if item[0] < today]
        future_or_today_dates = [item for item in parsed_dates if item[0] >= today]

        past_dates.sort(key=lambda x: x[0])
        future_or_today_dates.sort(key=lambda x: x[0])

        recent_past_dates = past_dates[-3:] if len(past_dates) > 3 else past_dates

        all_sorted_dates = sorted(recent_past_dates + future_or_today_dates, key=lambda x: x[0], reverse=True)

        filtered_map = {}
        for dt, d_str, link in all_sorted_dates:
            filtered_map[d_str] = link

        return filtered_map
    except Exception as e:
        print(f"Ошибка при получении дат: {e}")
        return {}


async def fetch_schedule_by_link(link: str, group_num: str) -> list:
    try:
        encoded_link = link.replace(" ", "%20")
        day_html = await fetch_protected_html(encoded_link)
        soup = BeautifulSoup(day_html, "html.parser")
        return parse_word_plaintext_schedule(soup, group_num)
    except Exception:
        return []


# === УВЕДОМЛЕНИЯ ===
async def broadcast_message(text: str):
    """Вспомогательная функция отправки сообщения всем подписчикам."""
    for user_id in list(subscribed_users):
        try:
            await bot.send_message(user_id, text, parse_mode="HTML")
        except Exception:
            pass


async def get_today_pair_numbers() -> set[int]:
    """Возвращает номера пар, присутствующих сегодня в расписании."""
    now_dt = datetime.datetime.now(TIMEZONE)
    today_pattern = f"{now_dt.day:02d}.{now_dt.month:02d}"
    dates_map = await fetch_available_dates()
    target_link = None

    for d_str, link in dates_map.items():
        if today_pattern in d_str or today_pattern in link:
            target_link = link
            break

    if not target_link:
        return set()

    lessons = await fetch_schedule_by_link(target_link, TARGET_GROUP)
    active_pairs = set()

    for idx, lesson in enumerate(lessons, start=1):
        if lesson and lesson.strip():
            active_pairs.add(idx)

    return active_pairs


async def check_call_notifications():
    """Проверка и отправка уведомлений о начале/конце первого и второго часов пар."""
    if not subscribed_users:
        return

    now = datetime.datetime.now(TIMEZONE).time().replace(second=0, microsecond=0)
    active_pairs = await get_today_pair_numbers()

    if not active_pairs:
        return

    for item in CALL_SCHEDULE:
        p_num = item["pair"]

        if p_num not in active_pairs:
            continue

        if now == item["h1_start"]:
            await broadcast_message(f"🔔 Началась {p_num}-я пара (1-й час)!")

        elif now == item["h1_end"]:
            await broadcast_message(f"⏸ Закончился 1-й час {p_num}-й пары. Перерыв.")

        elif now == item["h2_start"]:
            if p_num == 3:
                await broadcast_message(f"🔔 Закончился обед! Начался 2-й час 3-й пары.")
            else:
                await broadcast_message(f"🔔 Начался 2-й час {p_num}-й пары!")

        elif now == item["h2_end"]:
            await broadcast_message(f"🏁 {p_num}-я пара завершена!")


async def check_and_send_notifications():
    """Проверка уведомлений за 15 минут до начала и окончания всей пары."""
    if not subscribed_users:
        return
    now_dt = datetime.datetime.now(TIMEZONE)
    now_time = now_dt.time().replace(second=0, microsecond=0)
    today_pattern = f"{now_dt.day:02d}.{now_dt.month:02d}"

    for item in CALL_SCHEDULE:
        p_num = item["pair"]
        start_t = item["full_start"]
        end_t = item["full_end"]

        start_dt = datetime.datetime.combine(now_dt.date(), start_t, tzinfo=TIMEZONE)
        before_start_15 = (start_dt - datetime.timedelta(minutes=15)).time().replace(second=0, microsecond=0)

        end_dt = datetime.datetime.combine(now_dt.date(), end_t, tzinfo=TIMEZONE)
        before_end_15 = (end_dt - datetime.timedelta(minutes=15)).time().replace(second=0, microsecond=0)

        if now_time == before_start_15:
            dates_map = await fetch_available_dates()
            target_link = None
            for d_str, link in dates_map.items():
                if today_pattern in d_str or today_pattern in link:
                    target_link = link
                    break
            lesson_info = "Занятие отсутствует"
            cabinet = "Неизвестно"
            teacher = "Неизвестно"
            if target_link:
                lessons = await fetch_schedule_by_link(target_link, TARGET_GROUP)
                if len(lessons) >= p_num and lessons[p_num - 1]:
                    subj, cabinet, teacher = parse_lesson_details(lessons[p_num - 1])
                    lesson_info = subj

            if lesson_info and lesson_info != "Занятие отсутствует":
                t_label = "Преподаватели" if " / " in teacher else "Преподаватель"
                c_label = "Кабинеты" if " / " in cabinet else "Кабинет"
                teacher_line = f"\n👨‍🏫 <b>{t_label}:</b> <code>{teacher}</code>" if teacher != "Не указан" else ""

                msg = (
                    f"⏰ <b>Через 15 минут начинается {p_num}-я пара!</b> ({start_t.strftime('%H:%M')})\n"
                    f"📖 <code>{lesson_info}</code>\n"
                    f"🚪 <b>{c_label}:</b> <code>{cabinet}</code>"
                    f"{teacher_line}"
                )
                await broadcast_message(msg)

        elif now_time == before_end_15:
            # Раньше отправлялось безусловно для любого номера пары, даже если сегодня
            # расписания нет вообще (выходной) или конкретно этой пары сегодня нет —
            # теперь сверяемся с реальным расписанием на сегодня.
            active_pairs = await get_today_pair_numbers()
            if p_num in active_pairs:
                msg = f"⏳ <b>До конца {p_num}-й пары осталось 15 минут!</b> (Завершение в {end_t.strftime('%H:%M')})"
                await broadcast_message(msg)


# === ХЭНДЛЕРЫ ===
async def send_welcome_message(message: types.Message):
    subscribed_users.add(message.from_user.id)
    welcome_text = (
        "🥀 <b>Расписание занятий и звонков торгово-экономический колледж</b>\n\n"
        "🔔 <i>Автоматические уведомления за 15 минут до начала и конца пар включены!</i>"
    )
    await message.answer(welcome_text, reply_markup=get_main_inline_keyboard(), parse_mode="HTML")
    await message.answer("Используйте кнопки ниже для быстрого доступа 👇", reply_markup=get_bottom_reply_keyboard())


@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    await send_welcome_message(message)


@dp.message(F.text == "📋 Меню")
async def menu_handler(message: types.Message):
    await send_welcome_message(message)


@dp.message(F.text == "📍 Куда мне идти?")
async def where_to_go_handler(message: types.Message):
    subscribed_users.add(message.from_user.id)
    await message.answer("🔍 Проверяю дату и расписание...")
    now_dt = datetime.datetime.now(TIMEZONE)
    now_time = now_dt.time()
    today_pattern = f"{now_dt.day:02d}.{now_dt.month:02d}"
    dates_map = await fetch_available_dates()
    target_link = None
    matched_date = ""
    for d_str, link in dates_map.items():
        if today_pattern in d_str or today_pattern in link:
            target_link = link
            matched_date = d_str
            break
    if not target_link and dates_map:
        matched_date, target_link = list(dates_map.items())[0]
    if not target_link:
        await message.answer(
            f"📅 Сегодня <b>{now_dt.strftime('%d.%m.%Y')}</b>.\n❌ На сайте колледжа не удалось найти файл с расписанием.",
            parse_mode="HTML")
        return

    formatted_date_str = format_date_with_weekday(matched_date)
    lessons = await fetch_schedule_by_link(target_link, TARGET_GROUP)
    if not lessons or not any(lessons):
        await message.answer(
            f"🎉 На дату <b>{formatted_date_str}</b> у группы <b>{TARGET_GROUP}</b> нет занятий в файле!",
            parse_mode="HTML")
        return

    current_pair_item = None
    next_pair_item = None
    for item in CALL_SCHEDULE:
        if item["full_start"] <= now_time <= item["full_end"]:
            current_pair_item = item
            break
        elif now_time < item["full_start"]:
            next_pair_item = item
            break

    response = f"📅 <b>Расписание на: {formatted_date_str}</b>\n\n"
    if current_pair_item is not None:
        p_num = current_pair_item["pair"]
        raw_lesson = lessons[p_num - 1] if len(lessons) >= p_num and lessons[
            p_num - 1] else "Занятие отсутствует / Свободный час"
        subj, cabinet, teacher = parse_lesson_details(raw_lesson)

        end_dt = datetime.datetime.combine(now_dt.date(), current_pair_item["full_end"], tzinfo=TIMEZONE)
        total_remaining_minutes = max(1, int((end_dt - now_dt).total_seconds()) // 60)

        h1_end_dt = datetime.datetime.combine(now_dt.date(), current_pair_item["h1_end"], tzinfo=TIMEZONE)
        h2_start_dt = datetime.datetime.combine(now_dt.date(), current_pair_item["h2_start"], tzinfo=TIMEZONE)

        if now_time <= current_pair_item["h1_end"]:
            to_break_min = max(1, int((h1_end_dt - now_dt).total_seconds()) // 60)
            break_info = f"☕️ <b>До 10-мин перерыва:</b> {to_break_min} мин.\n"
        elif current_pair_item["h1_end"] < now_time < current_pair_item["h2_start"]:
            to_h2_min = max(1, int((h2_start_dt - now_dt).total_seconds()) // 60)
            break_info = f"⏸ <b>Сейчас 10-мин перерыв!</b> (до 2-го часа {to_h2_min} мин)\n"
        else:
            break_info = "🔥 <b>Идет 2-й час пары</b>\n"

        t_label = "Преподаватели" if " / " in teacher else "Преподаватель"
        c_label = "Кабинеты" if " / " in cabinet else "Кабинет"
        teacher_str = f"\n👨‍🏫 <b>{t_label}:</b> <code>{teacher}</code>" if teacher != "Не указан" else ""

        response += (
            f"🚨 <b>СЕЙЧАС ИДЕТ {p_num}-Я ПАРА:</b>\n\n📖 <code>{subj}</code>\n🚪 <b>{c_label}:</b> <code>{cabinet}</code>{teacher_str}\n\n{break_info}"
            f"⏳ <b>До ВСЕГО конца пары осталось:</b> {total_remaining_minutes} мин."
        )
    elif next_pair_item is not None:
        p_num = next_pair_item["pair"]
        raw_lesson = lessons[p_num - 1] if len(lessons) >= p_num and lessons[
            p_num - 1] else "Занятие отсутствует / Свободный час"
        subj, cabinet, teacher = parse_lesson_details(raw_lesson)

        p_time = next_pair_item["full_start"].strftime("%H:%M")
        start_dt = datetime.datetime.combine(now_dt.date(), next_pair_item["full_start"], tzinfo=TIMEZONE)
        until_start_minutes = max(1, int((start_dt - now_dt).total_seconds()) // 60)

        t_label = "Преподаватели" if " / " in teacher else "Преподаватель"
        c_label = "Кабинеты" if " / " in cabinet else "Кабинет"
        teacher_str = f"\n👨‍🏫 <b>{t_label}:</b> <code>{teacher}</code>" if teacher != "Не указан" else ""

        response += (
            f"⏳ <b>БЛИЖАЙШАЯ {p_num}-Я ПАРА (в {p_time}):</b>\n📖 <code>{subj}</code>\n🚪 <b>{c_label}:</b> <code>{cabinet}</code>{teacher_str}\n\n⏱ <b>До начала осталось: {until_start_minutes} мин.</b>"
        )
    else:
        response += "🎉 <b>На сегодня все пары уже завершились!</b>"
    await message.answer(response, parse_mode="HTML")


@dp.callback_query(F.data == "calls")
async def process_calls(callback_query: types.CallbackQuery):
    calls_text = get_calls_text()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_main")]])
    await callback_query.message.edit_text(calls_text, reply_markup=kb, parse_mode="HTML")
    await callback_query.answer()


@dp.callback_query(F.data == "select_date")
async def process_select_date(callback_query: types.CallbackQuery):
    await callback_query.answer("Загружаем даты...")
    dates_map = await fetch_available_dates()
    if not dates_map:
        await callback_query.message.answer("❌ Не удалось загрузить даты с сайта.")
        return
    buttons = []
    for date_str, link in dates_map.items():
        display_text = format_date_with_weekday(date_str)
        buttons.append([InlineKeyboardButton(text=f"📅 {display_text}", callback_data=f"get_date:{date_str}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    dp["dates_map"] = dates_map
    await callback_query.message.edit_text("📅 <b>Выберите дату расписания:</b>", reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("get_date:"))
async def process_get_schedule_for_date(callback_query: types.CallbackQuery):
    date_str = callback_query.data.split("get_date:")[1]
    dates_map = dp.get("dates_map", {})
    link = dates_map.get(date_str)
    if not link:
        await callback_query.answer("⚠️ Ссылка устарела, выберите дату заново.", show_alert=True)
        return
    await callback_query.answer("Загружаем предметы...")
    lessons = await fetch_schedule_by_link(link, TARGET_GROUP)
    formatted_date_str = format_date_with_weekday(date_str)

    if lessons and any(lessons):
        formatted_list = []
        for idx, lesson_raw in enumerate(lessons, start=1):
            if not lesson_raw:
                continue
            subj, cabinet, teacher = parse_lesson_details(lesson_raw)
            time_str = ""
            if idx <= len(CALL_SCHEDULE):
                p_info = CALL_SCHEDULE[idx - 1]
                time_str = f"({p_info['full_start'].strftime('%H:%M')} – {p_info['full_end'].strftime('%H:%M')})"

            t_label = "Преподаватели" if " / " in teacher else "Преподаватель"
            c_label = "Кабинеты" if " / " in cabinet else "Кабинет"
            teacher_line = f"\n👨‍🏫 <b>{t_label}:</b> <code>{html.escape(teacher)}</code>" if teacher != "Не указан" else ""

            formatted_list.append(
                f"<b>{idx}-я пара {time_str}</b>\n📖 {html.escape(subj)}\n🚪 <b>{c_label}:</b> <code>{cabinet}</code>{teacher_line}"
            )

        lessons_formatted = "\n\n".join(formatted_list)
        schedule_text = (
            f"📅 <b>Расписание на {html.escape(formatted_date_str)}</b>\n"
            f"👥 <b>Группа:</b> <code>{TARGET_GROUP}</code>\n"
            f"───────────────────\n\n{lessons_formatted}"
        )
    else:
        schedule_text = f"❌ На <b>{html.escape(formatted_date_str)}</b> пар для группы <b>{TARGET_GROUP}</b> не найдено."

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Выбрать другую дату", callback_data="select_date")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="back_main")]
    ])
    await callback_query.message.edit_text(schedule_text, reply_markup=kb, parse_mode="HTML")


# === УСПЕВАЕМОСТЬ ===
async def show_grades_menu(user_id: int, message_or_query):
    records = await get_user_grades_db(user_id)
    if not records:
        text = "📊 <b>У вас пока нет сохранённых оценок.</b>\n\nНажмите кнопку ниже, чтобы добавить первую оценку!"
        buttons = [[InlineKeyboardButton(text="➕ Добавить оценку", callback_data="start_add_grade")]]
    else:
        subject_dict = {}
        all_grades = []
        for subj, gr in records:
            subject_dict.setdefault(subj, []).append(gr)
            all_grades.append(gr)

        lines = ["📊 <b>Ваша успеваемость:</b>\n"]
        for subj, grades in subject_dict.items():
            avg_subj = sum(grades) / len(grades)
            grades_str = ", ".join(map(str, grades))
            lines.append(f"• <b>{html.escape(subj)}</b>: {grades_str} (Средний: <code>{avg_subj:.2f}</code>)")

        overall_avg = sum(all_grades) / len(all_grades)
        lines.append(f"\n📈 <b>Общий средний балл:</b> <code>{overall_avg:.2f}</code>")
        text = "\n".join(lines)

        buttons = [
            [InlineKeyboardButton(text="➕ Добавить оценку", callback_data="start_add_grade")],
            [InlineKeyboardButton(text="🗑 Очистить все оценки", callback_data="clear_grades")]
        ]

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(message_or_query, types.Message):
        await message_or_query.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.message(F.text == "📊 Успеваемость")
async def grades_text_handler(message: types.Message):
    await show_grades_menu(message.from_user.id, message)


@dp.callback_query(F.data == "start_add_grade")
async def start_add_grade_callback(callback_query: types.CallbackQuery, state: FSMContext):
    await state.set_state(GradeStates.selecting_subject)
    await callback_query.message.edit_text(
        "📚 <b>Выберите предмет из списка:</b>",
        reply_markup=get_subjects_keyboard(prefix="set_subj"),
        parse_mode="HTML"
    )
    await callback_query.answer()


@dp.callback_query(F.data.startswith("set_subj:"), GradeStates.selecting_subject)
async def process_subject_selection(callback_query: types.CallbackQuery, state: FSMContext):
    subject = callback_query.data.split("set_subj:")[1]
    await state.update_data(subject=subject)
    await state.set_state(GradeStates.selecting_grade)

    await callback_query.message.edit_text(
        f"📖 <b>Предмет:</b> <code>{html.escape(subject)}</code>\n\n"
        "⭐ <b>Выберите оценку (от 1 до 10):</b>",
        reply_markup=get_grades_keyboard(),
        parse_mode="HTML"
    )
    await callback_query.answer()


@dp.callback_query(F.data.startswith("set_num:"), GradeStates.selecting_grade)
async def process_grade_selection(callback_query: types.CallbackQuery, state: FSMContext):
    grade = int(callback_query.data.split("set_num:")[1])
    data = await state.get_data()
    subject = data.get("subject", "Предмет")

    await add_grade_db(callback_query.from_user.id, subject, grade)
    await state.clear()

    await callback_query.answer(f"Записано: {subject} — {grade}", show_alert=True)
    await show_grades_menu(callback_query.from_user.id, callback_query)


@dp.callback_query(F.data == "clear_grades")
async def process_clear_grades(callback_query: types.CallbackQuery):
    await clear_user_grades_db(callback_query.from_user.id)
    await callback_query.answer("Все оценки удалены!", show_alert=True)
    await show_grades_menu(callback_query.from_user.id, callback_query)


# === ДОМАШНИЕ ЗАДАНИЯ (CRUD) ===
async def show_homework_menu(user_id: int, message_or_query):
    records = await get_user_homeworks_db(user_id)
    buttons = [[InlineKeyboardButton(text="➕ Добавить задание", callback_data="hw_add")]]

    if not records:
        text = "📝 <b>Ваш список домашних заданий пуст!</b>\n\nНажмите кнопку ниже, чтобы создать новое задание."
    else:
        lines = ["📝 <b>Список домашних заданий:</b>\n"]
        for hw_id, subject, task, due_date, is_done in records:
            status = "✅" if is_done else "📌"
            due_str = f" <i>(до {due_date})</i>" if due_date != "Без срока" else ""
            lines.append(f"{status} <b>{html.escape(subject)}</b>{due_str}:\n   └ {html.escape(task)}")

            status_btn_text = "🔄 В работу" if is_done else "✅ Готово"
            buttons.append([
                InlineKeyboardButton(text=f"{status_btn_text} (№{hw_id})", callback_data=f"hw_toggle:{hw_id}"),
                InlineKeyboardButton(text="✏️", callback_data=f"hw_edit:{hw_id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"hw_del:{hw_id}")
            ])

        lines.append("\n<i>Нажимайте на кнопки управления под списком:</i>")
        text = "\n".join(lines)
        buttons.append([InlineKeyboardButton(text="🗑 Очистить ВСЁ ДЗ", callback_data="hw_clear_all")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(message_or_query, types.Message):
        await message_or_query.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message_or_query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@dp.message(F.text == "📝 ДЗ")
async def homework_text_handler(message: types.Message):
    await show_homework_menu(message.from_user.id, message)


@dp.callback_query(F.data == "hw_add")
async def start_add_hw(callback_query: types.CallbackQuery, state: FSMContext):
    await state.set_state(HomeworkStates.selecting_subject)
    await callback_query.message.edit_text(
        "📚 <b>Выберите предмет для ДЗ:</b>",
        reply_markup=get_subjects_keyboard(prefix="set_hw_subj"),
        parse_mode="HTML"
    )
    await callback_query.answer()


@dp.callback_query(F.data.startswith("set_hw_subj:"), HomeworkStates.selecting_subject)
async def process_hw_subject(callback_query: types.CallbackQuery, state: FSMContext):
    subject = callback_query.data.split("set_hw_subj:")[1]
    await state.update_data(subject=subject)
    await state.set_state(HomeworkStates.entering_task)

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]])
    await callback_query.message.edit_text(
        f"📖 <b>Предмет:</b> <code>{html.escape(subject)}</code>\n\n"
        "✍️ <b>Напишите текст домашнего задания в чат:</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback_query.answer()


@dp.message(HomeworkStates.entering_task)
async def process_hw_task_text(message: types.Message, state: FSMContext):
    await state.update_data(task=message.text)
    await state.set_state(HomeworkStates.selecting_due_date)
    await message.answer(
        "📅 <b>Выберите срок сдачи ДЗ:</b>",
        reply_markup=get_due_dates_keyboard(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("set_date:"), HomeworkStates.selecting_due_date)
async def process_hw_due_date(callback_query: types.CallbackQuery, state: FSMContext):
    due_date = callback_query.data.split("set_date:")[1]
    data = await state.get_data()

    await add_homework_db(callback_query.from_user.id, data["subject"], data["task"], due_date)
    await state.clear()

    await callback_query.answer("Задание успешно добавлено!", show_alert=True)
    await show_homework_menu(callback_query.from_user.id, callback_query)


@dp.callback_query(F.data.startswith("hw_toggle:"))
async def process_hw_toggle(callback_query: types.CallbackQuery):
    hw_id = int(callback_query.data.split("hw_toggle:")[1])
    await toggle_homework_status_db(hw_id)
    await callback_query.answer("Статус обновлен!")
    await show_homework_menu(callback_query.from_user.id, callback_query)


@dp.callback_query(F.data.startswith("hw_del:"))
async def process_hw_delete(callback_query: types.CallbackQuery):
    hw_id = int(callback_query.data.split("hw_del:")[1])
    await delete_homework_db(hw_id)
    await callback_query.answer("Задание удалено!")
    await show_homework_menu(callback_query.from_user.id, callback_query)


@dp.callback_query(F.data == "hw_clear_all")
async def process_hw_clear_all(callback_query: types.CallbackQuery):
    await clear_user_homeworks_db(callback_query.from_user.id)
    await callback_query.answer("Все задания удалены!")
    await show_homework_menu(callback_query.from_user.id, callback_query)


@dp.callback_query(F.data.startswith("hw_edit:"))
async def start_hw_edit(callback_query: types.CallbackQuery, state: FSMContext):
    hw_id = int(callback_query.data.split("hw_edit:")[1])
    hw = await get_homework_by_id_db(hw_id)
    if not hw:
        await callback_query.answer("Задание не найдено!", show_alert=True)
        return

    await state.update_data(editing_hw_id=hw_id)
    await state.set_state(HomeworkStates.editing_task)

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]])
    await callback_query.message.edit_text(
        f"✏️ <b>Редактирование задания по предмету:</b> <code>{html.escape(hw[1])}</code>\n"
        f"<b>Текущий текст:</b> <i>{html.escape(hw[2])}</i>\n\n"
        "<b>Введите новый текст задания в чат:</b>",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await callback_query.answer()


@dp.message(HomeworkStates.editing_task)
async def process_hw_edit_save(message: types.Message, state: FSMContext):
    data = await state.get_data()
    hw_id = data.get("editing_hw_id")
    if hw_id:
        await update_homework_task_db(hw_id, message.text)
        await message.answer("✅ Задание успешно обновлено!")
    await state.clear()
    await show_homework_menu(message.from_user.id, message)


# === ОБЩИЕ ВСПОМОГАТЕЛЬНЫЕ ХЭНДЛЕРЫ ===
@dp.callback_query(F.data == "cancel_action")
async def cancel_action_handler(callback_query: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    await state.clear()

    if current_state and "HomeworkStates" in current_state:
        await show_homework_menu(callback_query.from_user.id, callback_query)
    elif current_state and "GradeStates" in current_state:
        await show_grades_menu(callback_query.from_user.id, callback_query)
    else:
        await process_back_main(callback_query, state)


@dp.callback_query(F.data == "cancel_grade")
async def cancel_grade_handler(callback_query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_grades_menu(callback_query.from_user.id, callback_query)


@dp.callback_query(F.data == "back_main")
async def process_back_main(callback_query: types.CallbackQuery, state: FSMContext):
    await state.clear()
    welcome_text = "🥀 <b>Расписание занятий и звонков торгово-экономический колледж</b>\n\n"
    await callback_query.message.edit_text(welcome_text, reply_markup=get_main_inline_keyboard(), parse_mode="HTML")
    await callback_query.answer()


async def main():
    seed_if_needed(DB_PATH)  # восстанавливаем данные из снимка, если БД ещё пустая
    await init_db()
    # Регистрируем обе функции уведомлений в планировщик
    scheduler.add_job(check_and_send_notifications, "interval", minutes=1)
    scheduler.add_job(check_call_notifications, "interval", minutes=1)
    scheduler.start()
    print("Бот, БД и планировщик успешно запущены!")
    try:
        await dp.start_polling(bot)
    finally:
        # Закрываем фоновый браузер, если он был поднят, чтобы не оставался висеть в системе
        if _pw_browser is not None:
            await _pw_browser.close()
        if _pw is not None:
            await _pw.stop()


if __name__ == "__main__":
    asyncio.run(main())