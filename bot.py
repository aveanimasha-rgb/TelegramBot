import os
import asyncio
import logging
import sqlite3
import requests
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# --- Настройка логирования ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- Конфигурация ---
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = os.environ.get("RENDER_EXTERNAL_URL")
PORT = 8000
API_BASE_URL = "https://revolshtilil-book-recommendation-bot.hf.space"

# --- База данных для хранения привязок telegram_id -> api_user_id ---
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

# --- Вспомогательная функция для загрузки api_user_id в context.user_data ---
async def ensure_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    api_user_id = get_api_user_id(telegram_id)
    if api_user_id:
        context.user_data["api_user_id"] = api_user_id
    else:
        context.user_data.pop("api_user_id", None)
    return api_user_id

# --- Обработчики команд ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_session(update, context)
    text = (
        "📚 Книжный рекомендательный бот\n\n"
        "🔹 Без регистрации:\n"
        "   /find <часть названия> – найти книгу\n"
        "   Отправь ID книги (число) – получу обычные рекомендации\n\n"
        "🔹 С регистрацией (все оценки сохраняются):\n"
        "   /register – создать новый профиль\n"
        "   /login <ID> – войти в существующий профиль\n"
        "   /logout – выйти из профиля\n"
        "   /my_id – показать свой ID в системе\n"
        "   /rate <ID_книги> <оценка> – оценить книгу (1-5)\n"
        "   /my_ratings – список ваших оценок\n"
        "   /rec_personal <ID_книги> – персональные рекомендации (учитывают ваши оценки)\n\n"
        "📌 После поиска просто отправь ID книги (число) – получу обычные рекомендации."
    )
    await update.message.reply_text(text)

async def register(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = requests.post(f"{API_BASE_URL}/register", timeout=10)
        if resp.status_code != 200:
            await update.message.reply_text("❌ Ошибка регистрации на сервере.")
            return
        data = resp.json()
        api_user_id = data["user_id"]
        telegram_id = update.effective_user.id
        set_api_user_id(telegram_id, api_user_id)
        context.user_data["api_user_id"] = api_user_id
        await update.message.reply_text(
            f"✅ Вы зарегистрированы! Ваш ID в системе: {api_user_id}\n"
            "Сохраните этот ID, чтобы войти позже командой /login.\n"
            "Теперь вы можете оценивать книги и получать персональные рекомендации."
        )
    except Exception as e:
        logger.error(f"register error: {e}")
        await update.message.reply_text("❌ Ошибка соединения с сервером.")

async def login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /login <ID>")
        return
    try:
        api_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return
    try:
        resp = requests.get(f"{API_BASE_URL}/user_ratings/{api_user_id}", timeout=10)
        if resp.status_code == 404:
            await update.message.reply_text("Пользователь с таким ID не найден. Зарегистрируйтесь с помощью /register.")
            return
        resp.raise_for_status()
    except Exception:
        await update.message.reply_text("Ошибка проверки. Попробуйте позже.")
        return
    telegram_id = update.effective_user.id
    set_api_user_id(telegram_id, api_user_id)
    context.user_data["api_user_id"] = api_user_id
    await update.message.reply_text(f"✅ Вход выполнен. Ваш ID: {api_user_id}")

async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    delete_api_user_id(telegram_id)
    context.user_data.pop("api_user_id", None)
    await update.message.reply_text("🚪 Вы вышли из профиля. Теперь рекомендации будут без учёта ваших оценок.")

async def my_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_id = await ensure_session(update, context)
    if api_id:
        await update.message.reply_text(f"Ваш ID в системе: {api_id}")
    else:
        await update.message.reply_text("Вы не зарегистрированы и не вошли. Используйте /register или /login.")

async def rate_book(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_id = await ensure_session(update, context)
    if not api_id:
        await update.message.reply_text("Сначала войдите в профиль: /register или /login.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Использование: /rate <ID_книги> <оценка (1-5)>")
        return
    try:
        book_id = int(context.args[0])
        rating = int(context.args[1])
        if not (1 <= rating <= 5):
            raise ValueError
    except ValueError:
        await update.message.reply_text("ID книги и оценка должны быть числами. Оценка от 1 до 5.")
        return
    payload = {"user_id": api_id, "book_id": book_id, "rating": rating}
    try:
        resp = requests.post(f"{API_BASE_URL}/rate", json=payload, timeout=10)
        if resp.status_code == 200:
            await update.message.reply_text(f"⭐ Книга с ID {book_id} оценена на {rating}")
        else:
            await update.message.reply_text(f"❌ Ошибка: {resp.text}")
    except Exception as e:
        logger.error(f"rate error: {e}")
        await update.message.reply_text("Не удалось сохранить оценку.")

async def my_ratings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_id = await ensure_session(update, context)
    if not api_id:
        await update.message.reply_text("Сначала войдите в профиль: /register или /login.")
        return
    try:
        resp = requests.get(f"{API_BASE_URL}/user_ratings/{api_id}", timeout=10)
        if resp.status_code != 200:
            await update.message.reply_text("Не удалось получить оценки.")
            return
        ratings = resp.json()
        if not ratings:
            await update.message.reply_text("У вас пока нет оценок. Используйте /rate <ID_книги> <оценка>")
            return
        msg = "⭐ Ваши оценки:\n"
        for r in ratings[:20]:
            msg += f"• ID {r['book_id']} — {r['title_ru']} — оценка: {r['rating']}\n"
        if len(ratings) > 20:
            msg += f"\n... и ещё {len(ratings)-20} оценок."
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"my_ratings error: {e}")
        await update.message.reply_text("Ошибка получения оценок.")

async def recommend_personal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    api_id = await ensure_session(update, context)
    if not api_id:
        await update.message.reply_text("Сначала войдите в профиль: /register или /login.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /rec_personal <ID_книги>")
        return
    try:
        book_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID книги должен быть числом.")
        return
    payload = {"user_id": api_id, "book_id": book_id, "alpha": 0.6, "top_n": 10}
    try:
        resp = requests.post(f"{API_BASE_URL}/recommend_personal", json=payload, timeout=30)
        if resp.status_code != 200:
            await update.message.reply_text("Не удалось получить персональные рекомендации. Возможно, у вас мало оценок?")
            return
        books = resp.json()
        if not books:
            await update.message.reply_text("Рекомендаций не найдено.")
            return
        answer = "📚 Ваши персональные рекомендации:\n\n" + "\n".join(f"{i+1}. {title}" for i, title in enumerate(books[:10]))
        await update.message.reply_text(answer)
    except Exception as e:
        logger.error(f"rec_personal error: {e}")
        await update.message.reply_text("Ошибка получения рекомендаций.")

# --- Поиск книг ---
user_search_results = {}

async def find_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ensure_session(update, context)
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажи часть названия после /find. Пример: /find Властелин")
        return
    query = " ".join(context.args)
    await update.message.reply_text(f"🔎 Ищу книги по запросу «{query}»...")
    try:
        response = requests.post(f"{API_BASE_URL}/find", json={"query": query}, timeout=10)
        response.raise_for_status()
        books = response.json()
        if not books:
            await update.message.reply_text("😕 Ничего не найдено. Попробуй другое название.")
            return
        telegram_id = update.effective_user.id
        user_search_results[telegram_id] = books
        msg = "📖 Найденные книги:\n\n"
        for book in books:
            msg += f"ID: {book['id']} — {book['title_ru']}\n"
        msg += "\n✏️ Чтобы получить обычные рекомендации, отправь ID книги (просто число).\n"
        msg += "Для персональных используй /rec_personal <ID>"
        await update.message.reply_text(msg)
    except Exception as e:
        logger.error(f"find_books error: {e}")
        await update.message.reply_text("❌ Ошибка соединения с сервером.")

# --- Обработчик обычных текстовых сообщений (ID книги) ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    if text.isdigit():
        book_id = int(text)
        await update.message.reply_text(f"🔍 Получаю обычные рекомендации для книги ID {book_id}...")
        try:
            response = requests.post(f"{API_BASE_URL}/recommend", json={"book_id": book_id}, timeout=30)
            response.raise_for_status()
            data = response.json()
            recommendations = data.get("recommendations", [])
            if not recommendations:
                await update.message.reply_text("😕 Для этой книги не нашлось рекомендаций.")
                return
            answer = "📚 Обычные рекомендации (без учёта вашего профиля):\n\n"
            for i, book in enumerate(recommendations[:10], 1):
                answer += f"{i}. {book}\n"
            await update.message.reply_text(answer)
        except Exception as e:
            logger.error(f"handle_text recommend error: {e}")
            await update.message.reply_text("❌ Ошибка получения рекомендаций.")
    else:
        await update.message.reply_text(
            "Пожалуйста, отправь ID книги (число) из списка после /find.\n"
            "Используй /find <название> для поиска, /rec_personal для персональных рекомендаций."
        )

# --- Создание и настройка веб-хука и сервера ---
async def main():
    application = Application.builder().token(TOKEN).updater(None).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("register", register))
    application.add_handler(CommandHandler("login", login))
    application.add_handler(CommandHandler("logout", logout))
    application.add_handler(CommandHandler("my_id", my_id))
    application.add_handler(CommandHandler("rate", rate_book))
    application.add_handler(CommandHandler("my_ratings", my_ratings))
    application.add_handler(CommandHandler("rec_personal", recommend_personal))
    application.add_handler(CommandHandler("find", find_books))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

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
