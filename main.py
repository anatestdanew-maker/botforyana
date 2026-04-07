import logging
import re
import hashlib
import os
import base64
import json

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

import gspread

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ---------------- GOOGLE CREDENTIALS ----------------
credentials_b64 = os.getenv("GOOGLE_CREDENTIALS")

if credentials_b64:
    credentials_json = base64.b64decode(credentials_b64).decode("utf-8")
    credentials_dict = json.loads(credentials_json)

    with open("credentials.json", "w") as f:
        json.dump(credentials_dict, f)

# ---------------- GOOGLE SHEETS ----------------
gc = gspread.service_account(filename="credentials.json")

sheet_faq = gc.open("База знань").sheet1
sheet_programs = gc.open("Соц. Проекти").worksheet("Умови соц.програм")

data_faq = sheet_faq.get_all_records()

# ---------------- BUILD FAQ TREE ----------------
tree = {}

for row in data_faq:

    cat = row["Категорія"].strip()
    sub = row["Підтема"].strip()
    q = row["Питання"].strip()
    ans = row.get("Відповідь", "").strip()

    if cat not in tree:
        tree[cat] = {}

    if sub not in tree[cat]:
        tree[cat][sub] = {}

    tree[cat][sub][q] = ans


# ---------------- SAFE CALLBACK ----------------
def safe_callback(text):

    clean = re.sub(r"\s+", "_", text.strip())
    clean = re.sub(r"[^a-zA-Z0-9_]", "", clean)

    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]

    return f"{clean}_{h}"


# ---------------- START ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        [InlineKeyboardButton(cat, callback_data=safe_callback(cat))]
        for cat in tree
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Привіт! Обери категорію:",
        reply_markup=reply_markup
    )


# ---------------- CHECK APTEKA MENU ----------------
async def check_apteka_start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    programs = sheet_programs.row_values(1)[9:]

    keyboard = [[InlineKeyboardButton("🔍 Пошук програми", callback_data="program_search")]]

    for p in programs:
        keyboard.append(
            [InlineKeyboardButton(p, callback_data=f"program_{safe_callback(p)}")]
        )

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.callback_query.edit_message_text(
        "Оберіть програму:",
        reply_markup=reply_markup
    )


# ---------------- SEARCH APTEKA ----------------
async def search_apteka_input(update: Update, context: ContextTypes.DEFAULT_TYPE):

    program_name = context.user_data.get("program_name")

    if not program_name:
        return

    text = update.message.text.lower()

    terms = [t.strip() for t in text.split(",")]

    city = terms[0] if len(terms) > 0 else ""
    street = terms[1] if len(terms) > 1 else ""
    number = terms[2] if len(terms) > 2 else ""

    data = sheet_programs.get_all_records()

    results = []

    for row in data:

        city_match = city in row["Місто"].lower() if city else True
        street_match = street in row["Адреса"].lower() if street else True
        num_match = number in str(row["№"]) if number else True

        if city_match and street_match and num_match:

            value = str(row.get(program_name, "")).strip()

            if value == "":
                status = "не підключена"

            elif "відключена" in value.lower():
                status = f"відключена ({value})"

            else:
                status = f"підключена ({value})"

            results.append(f"{row['№']} {row['Адреса']} — {status}")

    if results:

        for i in range(0, len(results), 20):
            await update.message.reply_text("\n".join(results[i:i+20]))

    else:
        await update.message.reply_text("Аптеку не знайдено.")


# ---------------- BUTTON HANDLER ----------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    data_cb = query.data

    # -------- MAIN MENU --------
    if data_cb == "main_menu":

        keyboard = [
            [InlineKeyboardButton(cat, callback_data=safe_callback(cat))]
            for cat in tree
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            "Головне меню:",
            reply_markup=reply_markup
        )

        return

    # -------- CATEGORY --------
    for cat in tree:

        if safe_callback(cat) == data_cb:

            keyboard = [
                [InlineKeyboardButton(sub, callback_data=safe_callback(f"{cat}|{sub}"))]
                for sub in tree[cat]
            ]

            keyboard.append(
                [InlineKeyboardButton("Головне меню", callback_data="main_menu")]
            )

            reply_markup = InlineKeyboardMarkup(keyboard)

            await query.edit_message_text(
                f"Категорія: {cat}",
                reply_markup=reply_markup
            )

            return

    # -------- SUBCATEGORY --------
    for cat in tree:
        for sub in tree[cat]:

            if safe_callback(f"{cat}|{sub}") == data_cb:

                if "підключена аптека" in sub.lower():

                    await check_apteka_start(update, context)
                    return

                keyboard = [
                    [InlineKeyboardButton(q, callback_data=safe_callback(f"{cat}|{sub}|{q}"))]
                    for q in tree[cat][sub]
                ]

                keyboard.append(
                    [InlineKeyboardButton("Назад", callback_data=safe_callback(cat))]
                )

                keyboard.append(
                    [InlineKeyboardButton("Головне меню", callback_data="main_menu")]
                )

                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_text(
                    f"Підтема: {sub}",
                    reply_markup=reply_markup
                )

                return

    # -------- QUESTION --------
    for cat in tree:
        for sub in tree[cat]:
            for q in tree[cat][sub]:

                if safe_callback(f"{cat}|{sub}|{q}") == data_cb:

                    ans = tree[cat][sub][q]

                    keyboard = [
                        [InlineKeyboardButton("Назад", callback_data=safe_callback(f"{cat}|{sub}"))],
                        [InlineKeyboardButton("Головне меню", callback_data="main_menu")]
                    ]

                    reply_markup = InlineKeyboardMarkup(keyboard)

                    await query.edit_message_text(
                        ans if ans else "Відповідь відсутня",
                        reply_markup=reply_markup
                    )

                    return

    # -------- PROGRAM SELECT --------
    if data_cb.startswith("program_"):

        program_safe = data_cb.replace("program_", "")

        programs = sheet_programs.row_values(1)[9:]

        for p in programs:

            if safe_callback(p) == program_safe:

                context.user_data["program_name"] = p

                await query.edit_message_text(
                    f"Обрана програма: {p}\n\n"
                    "Введіть:\n"
                    "місто, частину вулиці або номер аптеки\n\n"
                    "Наприклад:\n"
                    "Львів\n"
                    "Васильк\n"
                    "219\n"
                    "Львів, Васильк"
                )

                return

    # -------- PROGRAM SEARCH --------
    if data_cb == "program_search":

        await query.edit_message_text(
            "Введіть частину назви програми:"
        )

        return


# ---------------- SEARCH FAQ ----------------
async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not context.args:

        await update.message.reply_text(
            "Напишіть слово після команди.\n"
            "Наприклад:\n"
            "/search живіт"
        )

        return

    query = " ".join(context.args).lower()

    results = []

    for cat in tree:
        for sub in tree[cat]:
            for q, ans in tree[cat][sub].items():

                if query in q.lower() or query in ans.lower():

                    results.append(
                        f"{cat}\n{sub}\n{q}\n{ans}\n"
                    )

    if results:

        for i in range(0, len(results), 5):

            await update.message.reply_text(
                "\n".join(results[i:i+5])
            )

    else:

        await update.message.reply_text("Нічого не знайдено.")


# ---------------- RUN BOT ----------------
if __name__ == "__main__":

    TOKEN = os.getenv("TELEGRAM_TOKEN")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("search", search))

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, search_apteka_input)
    )

    print("Бот запущений")

    app.run_polling()
