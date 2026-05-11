import os
import asyncio
import logging
import sqlite3
import requests
import re
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters, CallbackQueryHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = 8000
API_BASE_URL = "https://revolshtilil-book-recommendation-bot.hf.space"

DB_SESSIONS = "sessions.db"

def init_sessions_db():
    conn = sqlite3.connect(DB_SESSIONS)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS sessions
                 (telegram_id INTEGER PRIMARY KEY,
                  api_user_id INTEGER NOT NULL)''')
    conn.commit()
    conn.close()

def get_api_user_id(telegram_id):
    conn = sqlite3.connect(DB_SESSIONS)
    c = conn.cursor()
    c.execute("SELECT api_user_id FROM sessions WHERE telegram_id = ?", (telegram_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else None

def set_api_user_id(telegram_id, api_user_id):
    conn = sqlite3.connect(DB_SESSIONS)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO sessions (telegram_id, api_user_id) VALUES (?, ?)",
              (telegram_id, api_user_id))
    conn.commit()
    conn.close()

def delete_api_user_id(telegram_id):
    conn = sqlite3.connect(DB_SESSIONS)
    c = conn.cursor()
    c.execute("DELETE FROM sessions WHERE telegram_id = ?", (telegram_id,))
    conn.commit()
    conn.close()

init_sessions_db()

def escape_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

def get_main_keyboard():
    buttons = [
        [KeyboardButton("🏠 Главное меню")],
        [KeyboardButton("🔍 Поиск книги"), KeyboardButton("🎯 Персональные рекомендации")],
        [KeyboardButton("📝 Регистрация"), KeyboardButton("🔑 Вход (логин/пароль)"), KeyboardButton("🆔 Мой ID")],
        [KeyboardButton("⭐ Оценить книгу"), KeyboardButton("📚 Мои оценки"), KeyboardButton("ℹ️ Информация о книге")],
        [KeyboardButton("🚪 Выход"), KeyboardButton("❌ Отмена")]
    ]
    return ReplyKeyboardMarkup(buttons, resize_keyboard=True)

async def ensure_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    api_user_id = get_api_user_id(telegram_id)
    if api_user_id:
        context.user_data["api_user_id"] = api_user_id
    else:
        context.user_data.pop("api_user_id", None)
    return api_user_id

user_search_results = {}

async def do_find(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    await update.message.reply_text(f"🔎 Ищу книги по запросу «{escape_html(query)}»...",
                                    parse_mode='HTML', reply_markup=get_main_keyboard())
    try:
        response = requests.post(f"{API_BASE_URL}/find", json={"query": query}, timeout=45)
        response.raise_for_status()
        books = response.json()
        if not books:
            await update.message.reply_text("😕 Ничего не найдено.", reply_markup=get_main_keyboard())
            return
        telegram_id = update.effective_user.id
        user_search_results[telegram_id] = books
        msg = "📖 <b>Найденные книги:</b>\n\n"
        for book in books:
            safe_title = escape_html(book['title_ru'])
            msg += f"ID: <code>{book['id']}</code> — {safe_title}\n"
        msg += "\n✏️ Чтобы получить обычные рекомендации, отправь ID книги (просто число).\n"
        msg += "Для персональных используй /rec_personal <ID>\n"
        msg += "Для информации о книге используй /info <ID>"
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"find error: {e}")
        await update.message.reply_text("❌ Ошибка соединения с сервером.", reply_markup=get_main_keyboard())
    finally:
        context.user_data.pop("awaiting_find", None)

async def do_recommend_personal(update: Update, context: ContextTypes.DEFAULT_TYPE, api_id: int, book_id: int):
    payload = {"user_id": api_id, "book_id": book_id, "alpha": 0.6, "top_n": 10}
    try:
        resp = requests.post(f"{API_BASE_URL}/recommend_personal", json=payload, timeout=45)
        if resp.status_code != 200:
            await update.message.reply_text("Не удалось получить персональные рекомендации. Возможно, у вас мало оценок?",
                                            reply_markup=get_main_keyboard())
            return
        books = resp.json()
        if not books:
            await update.message.reply_text("Рекомендаций не найдено.", reply_markup=get_main_keyboard())
            return
        lines = [f"{i}. <b>{escape_html(title)}</b>" for i, title in enumerate(books[:10], 1)]
        answer = "📚 <b>Ваши персональные рекомендации:</b>\n\n" + "\n".join(lines)
        await update.message.reply_text(answer, parse_mode='HTML', reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"rec_personal error: {e}")
        await update.message.reply_text("Ошибка получения рекомендаций.", reply_markup=get_main_keyboard())
    finally:
        context.user_data.pop("awaiting_rec_personal", None)

async def do_rate(update: Update, context: ContextTypes.DEFAULT_TYPE, api_id: int, book_id: int, rating: int):
    payload = {"user_id": api_id, "book_id": book_id, "rating": rating}
    try:
        resp = requests.post(f"{API_BASE_URL}/rate", json=payload, timeout=45)
        if resp.status_code == 200:
            await update.message.reply_text(f"⭐ Книга с ID <code>{book_id}</code> оценена на {rating}",
                                            parse_mode='HTML', reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text(f"❌ Ошибка: {resp.text}", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"rate error: {e}")
        await update.message.reply_text("Не удалось сохранить оценку.", reply_markup=get_main_keyboard())
    finally:
        context.user_data.pop("awaiting_rate_step", None)
        context.user_data.pop("awaiting_rate_book_id", None)

async def do_info(update: Update, context: ContextTypes.DEFAULT_TYPE, book_id: int):
    await update.message.reply_text(f"🔍 Получаю информацию о книге ID <code>{book_id}</code>...",
                                    parse_mode='HTML', reply_markup=get_main_keyboard())
    try:
        resp = requests.get(f"{API_BASE_URL}/book_info/{book_id}", timeout=45)
        if resp.status_code == 404:
            await update.message.reply_text("Книга с таким ID не найдена.", reply_markup=get_main_keyboard())
            return
        resp.raise_for_status()
        data = resp.json()
        msg = (f"📖 <b>Информация о книге</b>\n\n"
               f"<b>ID:</b> {data['id']}\n"
               f"<b>Название (рус):</b> {escape_html(data['title_ru'])}\n"
               f"<b>Название (англ):</b> {escape_html(data['title_en'])}\n"
               f"<b>Автор(ы):</b> {escape_html(data['authors']) if data['authors'] else '—'}\n"
               f"<b>Жанр(ы):</b> {escape_html(data['categories']) if data['categories'] else '—'}\n"
               f"<b>Средняя оценка:</b> {data['average_rating'] if data['average_rating'] else '—'}\n"
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"info error: {e}")
        await update.message.reply_text("❌ Ошибка получения информации.", reply_markup=get_main_keyboard())
    finally:
        context.user_data.pop("awaiting_info", None)

async def do_register_with_login(update: Update, context: ContextTypes.DEFAULT_TYPE, login: str, password: str):
    try:
        resp = requests.post(f"{API_BASE_URL}/register_with_login", json={"login": login, "password": password}, timeout=45)
        if resp.status_code == 400:
            await update.message.reply_text("❌ Такой логин уже существует. Придумайте другой.", reply_markup=get_main_keyboard())
            return
        resp.raise_for_status()
        data = resp.json()
        api_user_id = data["user_id"]
        telegram_id = update.effective_user.id
        set_api_user_id(telegram_id, api_user_id)
        context.user_data["api_user_id"] = api_user_id
        await update.message.reply_text(
            f"✅ Вы зарегистрированы! Ваш ID в системе: <code>{api_user_id}</code>\n"
            "Теперь вы можете оценивать книги и получать персональные рекомендации.",
            parse_mode='HTML', reply_markup=get_main_keyboard()
        )
    except Exception as e:
        logger.error(f"register_with_login error: {e}")
        await update.message.reply_text("❌ Ошибка регистрации на сервере.", reply_markup=get_main_keyboard())
    finally:
        context.user_data.pop("awaiting_register_login", None)
        context.user_data.pop("awaiting_register_password", None)

async def do_login_with_password(update: Update, context: ContextTypes.DEFAULT_TYPE, login: str, password: str):
    try:
        resp = requests.post(f"{API_BASE_URL}/login_with_password", json={"login": login, "password": password}, timeout=45)
        if resp.status_code == 401:
            await update.message.reply_text("❌ Неверный логин или пароль.", reply_markup=get_main_keyboard())
            return
        resp.raise_for_status()
        data = resp.json()
        api_user_id = data["user_id"]
        telegram_id = update.effective_user.id
        set_api_user_id(telegram_id, api_user_id)
        context.user_data["api_user_id"] = api_user_id
        await update.message.reply_text(f"✅ Вход выполнен. Ваш ID: <code>{api_user_id}</code>",
                                        parse_mode='HTML', reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"login_with_password error: {e}")
        await update.message.reply_text("❌ Ошибка входа. Попробуйте позже.", reply_markup=get_main_keyboard())
    finally:
        context.user_data.pop("awaiting_login_login", None)
        context.user_data.pop("awaiting_login_password", None)

# ------------------- Обработчики команд -------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_session(update, context)
    text = (
        "📚 <b>Книжный рекомендательный бот</b>\n\n"
        "🔹 <b>Без регистрации:</b>\n"
        "   /find <часть названия> – найти книгу\n"
        "   /info <ID> – получить информацию о книге\n"
        "   Отправь ID книги (число) – обычные рекомендации\n\n"
        "🔹 <b>С регистрацией по логину/паролю:</b>\n"
        "   /register – создать профиль (логин, пароль)\n"
        "   /login – войти в профиль (логин, пароль)\n"
        "   /logout – выйти\n"
        "   /rate <ID_книги> <оценка> – оценить книгу\n"
        "   /my_ratings – список оценок\n"
        "   /rec_personal <ID_книги> – персональные рекомендации\n"
        "   /my_id – показать свой ID\n\n"
        "❌ /cancel – отменить текущее действие"
    )
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=get_main_keyboard())

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    states = [
        "awaiting_find", "awaiting_rec_personal", "awaiting_rate_step",
        "awaiting_genre_filter", "awaiting_delete_rating", "awaiting_info",
        "awaiting_register_login", "awaiting_register_password",
        "awaiting_login_login", "awaiting_login_password"
    ]
    cleared = False
    for state in states:
        if context.user_data.pop(state, None):
            cleared = True
    context.user_data.pop("awaiting_rate_book_id", None)
    context.user_data.pop("last_recommendations_en", None)
    if cleared:
        await update.message.reply_text("❌ Действие отменено.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("Нет активного действия.", reply_markup=get_main_keyboard())

async def register_with_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_register_login"):
        await update.message.reply_text("Регистрация уже начата. Завершите её или отмените командой /cancel.")
        return
    context.user_data["awaiting_register_login"] = True
    await update.message.reply_text("📝 Введите желаемый логин (уникальный):", reply_markup=get_main_keyboard())

async def login_with_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_login_login"):
        await update.message.reply_text("Вход уже начат. Завершите или отмените.")
        return
    context.user_data["awaiting_login_login"] = True
    await update.message.reply_text("🔑 Введите ваш логин:", reply_markup=get_main_keyboard())

async def info_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args:
        try:
            book_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID должен быть числом.", reply_markup=get_main_keyboard())
            return
        await do_info(update, context, book_id)
    else:
        context.user_data["awaiting_info"] = True
        await update.message.reply_text("ℹ️ Введите ID книги:", reply_markup=get_main_keyboard())

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    delete_api_user_id(telegram_id)
    context.user_data.pop("api_user_id", None)
    await update.message.reply_text("🚪 Вы вышли из профиля.", reply_markup=get_main_keyboard())

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_id = await ensure_session(update, context)
    if api_id:
        await update.message.reply_text(f"Ваш ID в системе: <code>{api_id}</code>", parse_mode='HTML', reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text("Вы не авторизованы. Используйте /register или /login.", reply_markup=get_main_keyboard())

async def my_ratings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_id = await ensure_session(update, context)
    if not api_id:
        await update.message.reply_text("Сначала войдите в профиль.", reply_markup=get_main_keyboard())
        return
    try:
        resp = requests.get(f"{API_BASE_URL}/user_ratings/{api_id}", timeout=45)
        if resp.status_code != 200:
            await update.message.reply_text("Не удалось получить оценки.", reply_markup=get_main_keyboard())
            return
        ratings = resp.json()
        if not ratings:
            await update.message.reply_text("У вас пока нет оценок.", reply_markup=get_main_keyboard())
            return
        msg = "<b>📋 Ваши оценки:</b>\n"
        for r in ratings[:20]:
            safe_title = escape_html(r['title_ru'])
            msg += f"• ID <code>{r['book_id']}</code> — {safe_title} — <b>{r['rating']}</b>\n"
        if len(ratings) > 20:
            msg += f"\n... и ещё {len(ratings)-20} оценок."
        await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_main_keyboard())
        if ratings:
            keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🗑 Удалить оценку", callback_data="delete_rating")]])
            await update.message.reply_text("Можете удалить одну из оценок (кнопка ниже).", reply_markup=keyboard)
    except Exception as e:
        logger.error(f"my_ratings error: {e}")
        await update.message.reply_text("Ошибка получения оценок.", reply_markup=get_main_keyboard())

async def delete_rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "delete_rating":
        context.user_data["awaiting_delete_rating"] = True
        await query.edit_message_text("Введите ID книги, оценку которой хотите удалить:")

async def show_ratings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    api_id = context.user_data.get("api_user_id")
    if not api_id:
        await query.edit_message_text("Вы не авторизованы.")
        return
    try:
        resp = requests.get(f"{API_BASE_URL}/user_ratings/{api_id}", timeout=45)
        if resp.status_code != 200:
            await query.edit_message_text("Не удалось получить оценки.")
            return
        ratings = resp.json()
        if not ratings:
            await query.edit_message_text("У вас пока нет оценок.")
            return
        msg = "<b>Ваши оценки:</b>\n"
        for r in ratings[:20]:
            safe_title = escape_html(r['title_ru'])
            msg += f"• ID <code>{r['book_id']}</code> — {safe_title} — <b>{r['rating']}</b>\n"
        if len(ratings) > 20:
            msg += f"\n... и ещё {len(ratings)-20} оценок."
        await query.edit_message_text(msg, parse_mode='HTML')
    except Exception as e:
        logger.error(f"show_ratings error: {e}")
        await query.edit_message_text("Ошибка получения оценок.")

async def find_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_session(update, context)
    if context.args:
        query = " ".join(context.args)
        await do_find(update, context, query)
    else:
        context.user_data["awaiting_find"] = True
        await update.message.reply_text("🔎 Введите название книги (или его часть):", parse_mode='HTML', reply_markup=get_main_keyboard())

async def recommend_personal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_id = await ensure_session(update, context)
    if not api_id:
        await update.message.reply_text("Сначала войдите в профиль.", reply_markup=get_main_keyboard())
        return
    if context.args:
        try:
            book_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("ID книги должен быть числом.", reply_markup=get_main_keyboard())
            return
        await do_recommend_personal(update, context, api_id, book_id)
    else:
        context.user_data["awaiting_rec_personal"] = True
        await update.message.reply_text("📚 Введите ID книги для персональных рекомендаций:", reply_markup=get_main_keyboard())

async def rate_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_id = await ensure_session(update, context)
    if not api_id:
        await update.message.reply_text("Сначала войдите в профиль.", reply_markup=get_main_keyboard())
        return
    if len(context.args) == 2:
        try:
            book_id = int(context.args[0])
            rating = int(context.args[1])
            if not (1 <= rating <= 5):
                raise ValueError
        except ValueError:
            await update.message.reply_text("ID книги и оценка (1-5) должны быть числами.", reply_markup=get_main_keyboard())
            return
        await do_rate(update, context, api_id, book_id, rating)
    else:
        context.user_data["awaiting_rate_step"] = 1
        await update.message.reply_text("✏️ Введите ID книги для оценки:", reply_markup=get_main_keyboard())

async def handle_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "filter_yes":
        context.user_data["awaiting_genre_filter"] = True
        await query.edit_message_text("🔍 Введите жанр (например, 'фантастика'):", reply_markup=None)
    else:
        await query.edit_message_text("❌ Фильтрация отменена.", reply_markup=None)

# ------------------- Главный обработчик текста -------------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    logger.info(f"Получен текст: '{text}'")

    if text == "🏠 Главное меню":
        await start(update, context)
        return
    if text == "🔍 Поиск книги":
        await find_books(update, context)
        return
    if text == "🎯 Персональные рекомендации":
        await recommend_personal(update, context)
        return
    if text == "📝 Регистрация":
        await register_with_login(update, context)
        return
    if text == "🔑 Вход (логин/пароль)":
        await login_with_password(update, context)
        return
    if text == "🆔 Мой ID":
        await my_id(update, context)
        return
    if text == "⭐ Оценить книгу":
        await rate_book(update, context)
        return
    if text == "📚 Мои оценки":
        await my_ratings(update, context)
        return
    if text == "ℹ️ Информация о книге":
        await info_book(update, context)
        return
    if text == "🚪 Выход":
        await logout(update, context)
        return
    if text == "❌ Отмена":
        await cancel(update, context)
        return

    # Регистрация: ввод логина
    if context.user_data.get("awaiting_register_login"):
        context.user_data["register_temp_login"] = text
        context.user_data["awaiting_register_login"] = False
        context.user_data["awaiting_register_password"] = True
        await update.message.reply_text("🔐 Введите пароль:", reply_markup=get_main_keyboard())
        return
    if context.user_data.get("awaiting_register_password"):
        login = context.user_data.pop("register_temp_login", "")
        password = text
        await do_register_with_login(update, context, login, password)
        return

    # Вход: ввод логина
    if context.user_data.get("awaiting_login_login"):
        context.user_data["login_temp_login"] = text
        context.user_data["awaiting_login_login"] = False
        context.user_data["awaiting_login_password"] = True
        await update.message.reply_text("🔐 Введите пароль:", reply_markup=get_main_keyboard())
        return
    if context.user_data.get("awaiting_login_password"):
        login = context.user_data.pop("login_temp_login", "")
        password = text
        await do_login_with_password(update, context, login, password)
        return

    # Информация о книге
    if context.user_data.get("awaiting_info"):
        if text.isdigit():
            await do_info(update, context, int(text))
        else:
            await update.message.reply_text("ID должен быть числом.", reply_markup=get_main_keyboard())
            context.user_data.pop("awaiting_info", None)
        return

    # Поиск
    if context.user_data.get("awaiting_find"):
        await do_find(update, context, text)
        return

    # Персональные рекомендации
    if context.user_data.get("awaiting_rec_personal"):
        if text.isdigit():
            api_id = context.user_data.get("api_user_id")
            if api_id:
                await do_recommend_personal(update, context, api_id, int(text))
            else:
                await update.message.reply_text("Ошибка профиля.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("Введите число – ID книги.", reply_markup=get_main_keyboard())
        return

    # Оценка: шаг 1
    if context.user_data.get("awaiting_rate_step") == 1:
        if text.isdigit():
            context.user_data["awaiting_rate_book_id"] = int(text)
            context.user_data["awaiting_rate_step"] = 2
            await update.message.reply_text("Введите оценку (от 1 до 5):", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("ID книги должен быть числом.", reply_markup=get_main_keyboard())
        return
    if context.user_data.get("awaiting_rate_step") == 2:
        if text.isdigit():
            rating = int(text)
            if 1 <= rating <= 5:
                api_id = context.user_data.get("api_user_id")
                book_id = context.user_data.get("awaiting_rate_book_id")
                if api_id and book_id:
                    await do_rate(update, context, api_id, book_id, rating)
                else:
                    await update.message.reply_text("Ошибка данных.", reply_markup=get_main_keyboard())
            else:
                await update.message.reply_text("Оценка от 1 до 5.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("Оценка должна быть числом.", reply_markup=get_main_keyboard())
        return

    # Фильтрация по жанру
    if context.user_data.get("awaiting_genre_filter"):
        genre = text
        last_en = context.user_data.get("last_recommendations_en", [])
        if not last_en:
            await update.message.reply_text("Нет сохранённых рекомендаций.", reply_markup=get_main_keyboard())
            context.user_data.pop("awaiting_genre_filter", None)
            return
        await update.message.reply_text(f"Фильтрую по жанру: «{escape_html(genre)}»...", parse_mode='HTML')
        try:
            resp = requests.post(f"{API_BASE_URL}/filter_recommendations", json={"titles": last_en, "genre_query": genre}, timeout=45)
            resp.raise_for_status()
            filtered = resp.json()
            if not filtered:
                await update.message.reply_text("Книг с таким жанром не найдено.", reply_markup=get_main_keyboard())
            else:
                msg = "📚 <b>Отфильтрованные рекомендации:</b>\n\n"
                for i, title in enumerate(filtered[:10], 1):
                    msg += f"{i}. <b>{escape_html(title)}</b>\n"
                await update.message.reply_text(msg, parse_mode='HTML', reply_markup=get_main_keyboard())
        except Exception as e:
            logger.error(f"filter error: {e}")
            await update.message.reply_text("❌ Ошибка фильтрации.", reply_markup=get_main_keyboard())
        finally:
            context.user_data.pop("awaiting_genre_filter", None)
        return

    # Удаление оценки
    if context.user_data.get("awaiting_delete_rating"):
        if text.isdigit():
            book_id = int(text)
            api_id = context.user_data.get("api_user_id")
            if not api_id:
                await update.message.reply_text("Вы не вошли в профиль.", reply_markup=get_main_keyboard())
            else:
                await update.message.reply_text(f"Удаляю оценку для книги ID {book_id}...", reply_markup=get_main_keyboard())
                try:
                    resp = requests.post(f"{API_BASE_URL}/delete_rating", json={"user_id": api_id, "book_id": book_id}, timeout=45)
                    if resp.status_code == 200:
                        await update.message.reply_text(f"✅ Оценка удалена.", reply_markup=get_main_keyboard())
                        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Показать обновлённые оценки", callback_data="show_ratings")]])
                        await update.message.reply_text("Хотите обновить список?", reply_markup=keyboard)
                    else:
                        error = resp.json().get("detail", "Ошибка")
                        await update.message.reply_text(f"❌ Ошибка: {error}", reply_markup=get_main_keyboard())
                except Exception as e:
                    logger.error(f"delete_rating error: {e}")
                    await update.message.reply_text("Не удалось соединиться с сервером.", reply_markup=get_main_keyboard())
        else:
            await update.message.reply_text("Введите число – ID книги.", reply_markup=get_main_keyboard())
        context.user_data.pop("awaiting_delete_rating", None)
        return

    # Обычные рекомендации по ID книги
    if text.isdigit():
        book_id = int(text)
        await update.message.reply_text(f"🔍 Ищу рекомендации для книги ID {book_id}...", reply_markup=get_main_keyboard())
        try:
            response = requests.post(f"{API_BASE_URL}/recommend", json={"book_id": book_id}, timeout=45)
            response.raise_for_status()
            data = response.json()
            recommendations = data.get("recommendations", [])
            recommendations_en = data.get("recommendations_en", [])
            if recommendations_en:
                context.user_data['last_recommendations_en'] = recommendations_en
            if not recommendations:
                await update.message.reply_text("😕 Рекомендаций не найдено.", reply_markup=get_main_keyboard())
                return
            answer = "📚 <b>Обычные рекомендации:</b>\n\n"
            for i, book in enumerate(recommendations[:10], 1):
                answer += f"{i}. {escape_html(book)}\n"
            await update.message.reply_text(answer, parse_mode='HTML', reply_markup=get_main_keyboard())
            if recommendations_en:
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Да", callback_data="filter_yes"), InlineKeyboardButton("Нет", callback_data="filter_no")]])
                await update.message.reply_text("Хотите отфильтровать по жанру?", reply_markup=keyboard)
        except Exception as e:
            logger.error(f"recommend error: {e}")
            await update.message.reply_text("❌ Ошибка получения рекомендаций.", reply_markup=get_main_keyboard())
    else:
        await update.message.reply_text(
            "Пожалуйста, используйте кнопки меню или команды.\n"
            "Для отмены действия введите /cancel.",
            reply_markup=get_main_keyboard()
        )

# ------------------- Запуск вебхука -------------------
async def main():
    application = Application.builder().token(TOKEN).updater(None).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("cancel", cancel))
    application.add_handler(CommandHandler("register", register_with_login))
    application.add_handler(CommandHandler("login", login_with_password))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("my_id", my_id))
    application.add_handler(CommandHandler("rate", rate_book))
    application.add_handler(CommandHandler("my_ratings", my_ratings))
    application.add_handler(CommandHandler("rec_personal", recommend_personal))
    application.add_handler(CommandHandler("find", find_books))
    application.add_handler(CommandHandler("info", info_book))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    application.add_handler(CallbackQueryHandler(handle_filter_callback, pattern="^filter_"))
    application.add_handler(CallbackQueryHandler(delete_rating_callback, pattern="^delete_rating$"))
    application.add_handler(CallbackQueryHandler(show_ratings_callback, pattern="^show_ratings$"))

    await application.bot.set_webhook(f"{URL}/telegram", allowed_updates=Update.ALL_TYPES)

    async def telegram(request: Request) -> Response:
        await application.update_queue.put(Update.de_json(await request.json(), application.bot))
        return Response()

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    starlette_app = Starlette(routes=[
        Route("/telegram", telegram, methods=["POST"]),
        Route("/healthcheck", health, methods=["GET"]),
    ])

    import uvicorn
    web = uvicorn.Server(uvicorn.Config(starlette_app, host="0.0.0.0", port=PORT, log_level="info"))

    async with application:
        await application.start()
        await web.serve()
        await application.stop()

if __name__ == "__main__":
    asyncio.run(main())
