import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

TOKEN = "ТУТ_ТВІЙ_ТОКЕН"

# ---------------- КЛАВІАТУРА ----------------
def main_menu_keyboard():
    keyboard = [
        [InlineKeyboardButton("Чи підключена аптека до програми", callback_data="check_apteka")],
        [InlineKeyboardButton("Ще щось", callback_data="other_option")],
    ]
    return InlineKeyboardMarkup(keyboard)


# ---------------- СТАРТ ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Вітаю! Оберіть опцію:", reply_markup=main_menu_keyboard()
    )


# ---------------- ОБРОБКА КНОПОК ----------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # відповідаємо Telegram, щоб кнопка не крутилось

    ans = ""
    if query.data == "check_apteka":
        # Тут твоя логіка перевірки
        ans = "Аптека підключена ✅"  # або ❌
    elif query.data == "other_option":
        ans = "Це інша опція"

    # Перевіряємо, що текст не порожній
    if ans:
        await query.edit_message_text(ans, reply_markup=main_menu_keyboard())
    else:
        await query.edit_message_text("Немає даних для відображення.", reply_markup=main_menu_keyboard())


# ---------------- ГОЛОВНА ----------------
async def main():
    app = ApplicationBuilder().token(TOKEN).build()

    # Додаємо обробники
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_handler))

    # Скидання старих вебхуків і апдейтів
    bot = app.bot
    await bot.delete_webhook(drop_pending_updates=True)

    # Запуск бота
    await app.run_polling()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
