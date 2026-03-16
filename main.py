import logging
import re
import hashlib
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes
import gspread
import os
import base64
import json

# --- Decode Google Sheets credentials ---
credentials_b64 = os.getenv('GOOGLE_CREDENTIALS')
if credentials_b64:
    credentials_json = base64.b64decode(credentials_b64).decode('utf-8')
    credentials_dict = json.loads(credentials_json)
    with open('credentials.json', 'w') as f:
        json.dump(credentials_dict, f)
# --- Логування ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# --- Google Sheets ---
gc = gspread.service_account(filename='credentials.json')
sheet = gc.open('База знань').sheet1
data = sheet.get_all_records()

# --- Створюємо дерево меню ---
tree = {}
for row in data:
    cat = row['Категорія'].strip()
    sub = row['Підтема'].strip()
    q = row['Питання'].strip()
    ans = row.get('Відповідь', '').strip()
    
    if cat not in tree:
        tree[cat] = {}
    if sub not in tree[cat]:
        tree[cat][sub] = {}
    tree[cat][sub][q] = ans

# --- Безпечний callback ---
def safe_callback(text):
    clean = re.sub(r'\s+', '_', text.strip())
    clean = re.sub(r'[^a-zA-Z0-9_]', '', clean)
    h = hashlib.sha1(text.encode('utf-8')).hexdigest()[:20]
    return f"{clean}_{h}"

# --- Старт ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(cat, callback_data=safe_callback(cat))] for cat in tree]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("Привіт! Обери категорію:", reply_markup=reply_markup)

# --- Обробка кнопок ---
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_cb = query.data

    # --- Категорія ---
    for cat in tree:
        if safe_callback(cat) == data_cb:
            keyboard = [[InlineKeyboardButton(sub, callback_data=safe_callback(f"{cat}|{sub}"))] for sub in tree[cat]]
            keyboard.append([InlineKeyboardButton("Головне меню", callback_data="main_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(f"Категорія: {cat}\nОберіть підтему:", reply_markup=reply_markup)
            return

    # --- Підтема ---
    for cat in tree:
        for sub in tree[cat]:
            if safe_callback(f"{cat}|{sub}") == data_cb:
                keyboard = [[InlineKeyboardButton(q, callback_data=safe_callback(f"{cat}|{sub}|{q}"))] for q in tree[cat][sub]]
                keyboard.append([InlineKeyboardButton("Назад", callback_data=safe_callback(cat))])
                keyboard.append([InlineKeyboardButton("Головне меню", callback_data="main_menu")])
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(f"Підтема: {sub}\nОберіть питання:", reply_markup=reply_markup)
                return

    # --- Питання ---
    for cat in tree:
        for sub in tree[cat]:
            for q, ans in tree[cat][sub].items():
                if safe_callback(f"{cat}|{sub}|{q}") == data_cb:
                    keyboard = [
                        [InlineKeyboardButton("Назад", callback_data=safe_callback(f"{cat}|{sub}"))],
                        [InlineKeyboardButton("Головне меню", callback_data="main_menu")]
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.edit_message_text(ans, reply_markup=reply_markup)
                    return

    # --- Головне меню ---
    if data_cb == "main_menu":
        keyboard = [[InlineKeyboardButton(cat, callback_data=safe_callback(cat))] for cat in tree]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("Привіт! Обери категорію:", reply_markup=reply_markup)

# --- Запуск ---
if __name__ == '__main__':
    TOKEN = os.getenv('TELEGRAM_TOKEN')
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CallbackQueryHandler(button_handler))
    print("Бот запущений...")
    app.run_polling()
