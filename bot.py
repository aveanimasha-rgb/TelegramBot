import os
import asyncio
import logging
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
API_BASE_URL = "https://revolshtilil-book-recommendation-bot.hf.space"  # Ваш Space

# --- Хранилище результатов поиска для каждого пользователя ---
user_search_results = {}  # {user_id: [{"id": ..., "title_ru": ...}]}

# --- Обработчик команды /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📚 Привет! Я бот для поиска книг.\n\n"
        "🔍 Отправь команду:\n"
        "/find <часть названия на русском>\n\n"
        "Пример: /find Анна Каренина\n\n"
        "Я покажу список книг с ID. Отправь ID (число), и я дам рекомендации."
    )

# --- Обработчик команды /find ---
async def find_books(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Пожалуйста, укажи часть названия после /find. Пример: /find Властелин")
        return

    query = " ".join(context.args)
    await update.message.reply_text(f"🔎 Ищу книги по запросу «{query}»...")

    try:
        response = requests.post(f"{API_BASE_URL}/find", json={"query": query}, timeout=10)
        response.raise_for_status()
        books = response.json()  # список [{"id": ..., "title_ru": ...}]

        if not books:
            await update.message.reply_text("😕 Ничего не найдено. Попробуй другое название.")
            return

        # Сохраняем результаты для этого пользователя
        user_search_results[user_id] = books

        # Формируем сообщение со списком
        msg = "📖 Найденные книги:\n\n"
        for book in books:
            msg += f"ID: `{book['id']}` — {book['title_ru']}\n"
        msg += "\n✏️ Чтобы получить рекомендации, отправь ID книги (просто число)."

        await update.message.reply_text(msg, parse_mode="Markdown")

    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при вызове /find: {e}")
        await update.message.reply_text("❌ Ошибка соединения с сервером. Попробуйте позже.")

# --- Обработчик текстовых сообщений (ожидаем ID книги) ---
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # Проверяем, является ли ввод числом (ID)
    if text.isdigit():
        book_id = int(text)
        # Можно проверить, есть ли этот ID в последних результатах поиска (опционально)
        await update.message.reply_text(f"🔍 Получаю рекомендации для книги с ID {book_id}...")

        try:
            response = requests.post(f"{API_BASE_URL}/recommend", json={"book_id": book_id}, timeout=30)
            response.raise_for_status()
            data = response.json()
            recommendations = data.get("recommendations", [])

            if not recommendations:
                await update.message.reply_text("😕 Для этой книги не нашлось рекомендаций.")
                return

            # Ограничим 10 книгами
            recommendations = recommendations[:10]
            answer = "📚 **Вот что я нашёл:**\n\n"
            for i, book in enumerate(recommendations, 1):
                answer += f"{i}. {book}\n"
            await update.message.reply_text(answer, parse_mode="Markdown")

        except requests.exceptions.RequestException as e:
            logger.error(f"Ошибка при вызове /recommend: {e}")
            await update.message.reply_text("❌ Ошибка получения рекомендаций. Попробуйте позже.")
    else:
        # Если пользователь отправил не число, напоминаем
        await update.message.reply_text(
            "Пожалуйста, отправь ID книги (число) из списка, который я показал после команды /find.\n"
            "Используй /find <название>, чтобы найти книгу."
        )

# --- Создание и настройка Telegram-приложения ---
async def main():
    # Создаём приложение Telegram
    application = Application.builder().token(TOKEN).updater(None).build()

    # Добавляем обработчики
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("find", find_books))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Регистрируем веб-хук (Render сам присвоит переменную RENDER_EXTERNAL_URL)
    await application.bot.set_webhook(f"{URL}/telegram", allowed_updates=Update.ALL_TYPES)

    # Создаём Starlette-приложение для обработки запросов от Telegram
    async def telegram(request: Request) -> Response:
        await application.update_queue.put(Update.de_json(await request.json(), application.bot))
        return Response()

    async def health(request: Request) -> PlainTextResponse:
        return PlainTextResponse("OK")

    starlette_app = Starlette(routes=[
        Route("/telegram", telegram, methods=["POST"]),
        Route("/healthcheck", health, methods=["GET"]),
    ])

    # Запускаем веб-сервер (uvicorn) в фоновом режиме
    import uvicorn
    web = uvicorn.Server(uvicorn.Config(starlette_app, host="0.0.0.0", port=PORT, log_level="info"))

    # Запускаем всё вместе
    async with application:
        await application.start()
        await web.serve()
        await application.stop()

# --- Точка входа ---
if __name__ == "__main__":
    asyncio.run(main())
