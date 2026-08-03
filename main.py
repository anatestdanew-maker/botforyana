import base64
import hashlib
import html
import json
import logging
import os
import re
from typing import Any

import gspread
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

KNOWLEDGE_SPREADSHEET = "База знань"
DRUG_SPREADSHEET = "Зведений перелік ДЛ"

DRUG_SHEET_NAMES = [
    "I. Інші ЛЗ",
    "III. Перелік Комбіновані ЛЗ",
    "Реєстр комбіновані ЛЗ",
    "перелік ЛЗ",
    "РЕЄСТР ДОСТУПНІ ЛІКИ",
    "РЕЄСТР ІНСУЛІНИ",
    "РЕЄСТР МЕД ВИРОБИ",
]

INSULIN_SHEET = "РЕЄСТР ІНСУЛІНИ"
MEDICAL_DEVICES_SHEET = "РЕЄСТР МЕД ВИРОБИ"
RESULTS_PER_PAGE = 10

COLUMN_B = 2
COLUMN_C = 3
COLUMN_D = 4
COLUMN_E = 5
COLUMN_F = 6
COLUMN_H = 8
COLUMN_L = 12
COLUMN_O = 15
COLUMN_P = 16

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def prepare_credentials() -> None:
    credentials_b64 = os.getenv("GOOGLE_CREDENTIALS")

    if credentials_b64:
        try:
            credentials_json = base64.b64decode(credentials_b64).decode("utf-8")
            credentials_dict = json.loads(credentials_json)

            with open("credentials.json", "w", encoding="utf-8") as file:
                json.dump(credentials_dict, file)
        except Exception as exc:
            raise RuntimeError("Не вдалося прочитати GOOGLE_CREDENTIALS.") from exc

    if not os.path.exists("credentials.json"):
        raise RuntimeError(
            "Файл credentials.json не знайдено і змінна "
            "GOOGLE_CREDENTIALS не задана."
        )


prepare_credentials()
gc = gspread.service_account(filename="credentials.json")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize(value: Any) -> str:
    text = clean_text(value).casefold()
    return text.replace("’", "'").replace("`", "'")


def normalize_dosage(value: Any) -> str:
    text = normalize(value).replace(",", ".")
    return re.sub(r"\s+", "", text)


def safe_callback(text: str) -> str:
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
    return f"kb_{digest}"


def value_at(row: list[str], column_number: int) -> str:
    index = column_number - 1
    if index < 0 or index >= len(row):
        return ""
    return clean_text(row[index])


def header_at(headers: list[str], column_number: int) -> str:
    return value_at(headers, column_number)


def shorten_button(text: str, max_length: int = 58) -> str:
    text = clean_text(text) or "Назва не вказана"
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def format_money(value: str) -> str:
    value = clean_text(value)
    if not value:
        return ""
    if re.search(r"[A-Za-zА-Яа-яІіЇїЄєҐґ]", value):
        return value
    return f"{value} грн"


def append_field(lines: list[str], icon: str, label: str, value: str) -> None:
    value = clean_text(value)
    if value:
        lines.append(f"{icon} <b>{html.escape(label)}:</b> {html.escape(value)}")


knowledge_sheet = gc.open(KNOWLEDGE_SPREADSHEET).sheet1
knowledge_rows = knowledge_sheet.get_all_records()

knowledge_tree: dict[str, dict[str, dict[str, str]]] = {}

for source_row in knowledge_rows:
    category = clean_text(source_row.get("Категорія", ""))
    subtopic = clean_text(source_row.get("Підтема", ""))
    question = clean_text(source_row.get("Питання", ""))
    answer = clean_text(source_row.get("Відповідь", ""))

    if not category or not subtopic or not question:
        continue

    knowledge_tree.setdefault(category, {})
    knowledge_tree[category].setdefault(subtopic, {})
    knowledge_tree[category][subtopic][question] = answer

knowledge_callbacks: dict[str, tuple[str, ...]] = {}

for category, subtopics in knowledge_tree.items():
    knowledge_callbacks[safe_callback(category)] = ("category", category)

    for subtopic, questions in subtopics.items():
        knowledge_callbacks[safe_callback(f"{category}|{subtopic}")] = (
            "subtopic",
            category,
            subtopic,
        )

        for question in questions:
            knowledge_callbacks[
                safe_callback(f"{category}|{subtopic}|{question}")
            ] = ("question", category, subtopic, question)


drug_book = gc.open(DRUG_SPREADSHEET)

drug_sheets: dict[str, dict[str, Any]] = {}
drug_records: list[dict[str, Any]] = []
drug_records_by_id: dict[int, dict[str, Any]] = {}

next_record_id = 1

for sheet_name in DRUG_SHEET_NAMES:
    worksheet = drug_book.worksheet(sheet_name)
    all_values = worksheet.get_all_values()

    if not all_values:
        logger.warning("Вкладка '%s' порожня.", sheet_name)
        drug_sheets[sheet_name] = {"headers": [], "records": []}
        continue

    headers = [clean_text(value) for value in all_values[0]]
    sheet_records: list[dict[str, Any]] = []

    for sheet_row_number, raw_row in enumerate(all_values[1:], start=2):
        row = [clean_text(value) for value in raw_row]

        active_substance = value_at(row, COLUMN_B)
        trade_name = value_at(row, COLUMN_C)

        if not any(row) or (not active_substance and not trade_name):
            continue

        record = {
            "id": next_record_id,
            "sheet_name": sheet_name,
            "sheet_row_number": sheet_row_number,
            "headers": headers,
            "row": row,
            "active_substance": active_substance,
            "trade_name": trade_name,
            "form": value_at(row, COLUMN_D),
            "dosage": value_at(row, COLUMN_E),
            "package": value_at(row, COLUMN_F),
            "manufacturer": value_at(row, COLUMN_H),
            "retail_price": value_at(row, COLUMN_L),
            "reimbursement": value_at(row, COLUMN_O),
            "copay": value_at(row, COLUMN_P),
        }

        sheet_records.append(record)
        drug_records.append(record)
        drug_records_by_id[next_record_id] = record
        next_record_id += 1

    drug_sheets[sheet_name] = {
        "headers": headers,
        "records": sheet_records,
    }

logger.info("Завантажено %s записів.", len(drug_records))


def search_drugs(search_text: str, mode: str = "all") -> list[dict[str, Any]]:
    query_tokens = [token for token in normalize(search_text).split() if token]

    if not query_tokens:
        return []

    if mode == "insulin":
        allowed_sheets = {INSULIN_SHEET}
    elif mode == "strips":
        allowed_sheets = {MEDICAL_DEVICES_SHEET}
    else:
        allowed_sheets = set(DRUG_SHEET_NAMES)

    results: list[dict[str, Any]] = []

    for record in drug_records:
        if record["sheet_name"] not in allowed_sheets:
            continue

        searchable_text = normalize(
            f"{record['active_substance']} {record['trade_name']}"
        )

        if all(token in searchable_text for token in query_tokens):
            results.append(record)

    normalized_query = normalize(search_text)

    results.sort(
        key=lambda record: (
            0 if normalize(record["trade_name"]) == normalized_query else 1,
            0 if normalize(record["trade_name"]).startswith(normalized_query) else 1,
            normalize(record["trade_name"]),
            normalize(record["dosage"]),
            normalize(record["package"]),
        )
    )

    return results


def find_analogs(record: dict[str, Any]) -> list[dict[str, Any]]:
    active = normalize(record["active_substance"])
    dosage = normalize_dosage(record["dosage"])
    trade_name = normalize(record["trade_name"])

    if not active or not dosage:
        return []

    analogs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()

    for candidate in drug_records:
        if candidate["id"] == record["id"]:
            continue
        if normalize(candidate["active_substance"]) != active:
            continue
        if normalize_dosage(candidate["dosage"]) != dosage:
            continue
        if normalize(candidate["trade_name"]) == trade_name:
            continue

        unique_key = (
            normalize(candidate["trade_name"]),
            normalize_dosage(candidate["dosage"]),
            normalize(candidate["package"]),
            normalize(candidate["manufacturer"]),
        )

        if unique_key in seen:
            continue

        seen.add(unique_key)
        analogs.append(candidate)

    analogs.sort(
        key=lambda item: (
            normalize(item["trade_name"]),
            normalize(item["package"]),
        )
    )
    return analogs


def format_drug_card(record: dict[str, Any]) -> str:
    sheet_name = record["sheet_name"]
    headers = record["headers"]
    lines: list[str] = []

    title = record["trade_name"] or record["active_substance"] or "Назва не вказана"
    title_icon = "🩸" if sheet_name == MEDICAL_DEVICES_SHEET else "💊"
    lines.append(f"{title_icon} <b>{html.escape(title)}</b>")

    if sheet_name != MEDICAL_DEVICES_SHEET:
        append_field(
            lines,
            "🧪",
            header_at(headers, COLUMN_B) or "Діюча речовина",
            record["active_substance"],
        )

    append_field(
        lines,
        "💉" if sheet_name == INSULIN_SHEET else "💊",
        header_at(headers, COLUMN_D) or "Форма випуску",
        record["form"],
    )

    if sheet_name == MEDICAL_DEVICES_SHEET:
        append_field(
            lines,
            "📝",
            header_at(headers, COLUMN_E) or "Опис",
            record["dosage"],
        )
        append_field(
            lines,
            "📦",
            header_at(headers, COLUMN_F) or "Фасування",
            record["package"],
        )
    else:
        append_field(
            lines,
            "📏",
            header_at(headers, COLUMN_E) or "Дозування",
            record["dosage"],
        )
        append_field(
            lines,
            "💉" if sheet_name == INSULIN_SHEET else "📦",
            header_at(headers, COLUMN_F) or (
                "Кількість МО в первинній упаковці"
                if sheet_name == INSULIN_SHEET
                else "Фасування"
            ),
            record["package"],
        )

    append_field(
        lines,
        "🏭",
        header_at(headers, COLUMN_H) or "Виробник",
        record["manufacturer"],
    )
    append_field(
        lines,
        "💰",
        header_at(headers, COLUMN_L) or "Роздрібна ціна",
        format_money(record["retail_price"]),
    )
    append_field(
        lines,
        "💙",
        header_at(headers, COLUMN_O) or "Розмір реімбурсації",
        format_money(record["reimbursement"]),
    )
    append_field(
        lines,
        "💳",
        header_at(headers, COLUMN_P) or "Сума доплати",
        format_money(record["copay"]),
    )

    lines.append(f"📄 <i>{html.escape(sheet_name)}</i>")
    return "\n\n".join(lines)


def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📚 База знань", callback_data="knowledge")],
        [InlineKeyboardButton("💊 Доступні ліки", callback_data="drugs")],
    ])


def drugs_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Пошук", callback_data="drug_search")],
        [InlineKeyboardButton("💉 Інсуліни", callback_data="insulin")],
        [InlineKeyboardButton("🩸 Тест-смужки", callback_data="strips")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")],
    ])


def search_navigation_markup(mode: str) -> InlineKeyboardMarkup:
    callback = {
        "all": "drug_search",
        "insulin": "insulin",
        "strips": "strips",
    }.get(mode, "drug_search")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Новий пошук", callback_data=callback)],
        [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])


def result_title(record: dict[str, Any]) -> str:
    title = record["trade_name"] or record["active_substance"] or "Назва не вказана"

    details = [value for value in [record["dosage"], record["package"]] if value]

    if details:
        title = f"{title} — {'; '.join(details)}"

    if record["sheet_name"] == INSULIN_SHEET:
        prefix = "💉 "
    elif record["sheet_name"] == MEDICAL_DEVICES_SHEET:
        prefix = "🩸 "
    else:
        prefix = "💊 "

    return shorten_button(prefix + title)


async def show_search_results(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    page: int = 0,
    edit: bool = False,
) -> None:
    result_ids: list[int] = context.user_data.get("search_result_ids", [])
    mode = context.user_data.get("search_mode", "all")
    search_text = context.user_data.get("search_text", "")

    total = len(result_ids)
    total_pages = max(1, (total + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    context.user_data["results_page"] = page

    start_index = page * RESULTS_PER_PAGE
    end_index = min(start_index + RESULTS_PER_PAGE, total)

    keyboard: list[list[InlineKeyboardButton]] = []

    for record_id in result_ids[start_index:end_index]:
        record = drug_records_by_id.get(record_id)
        if record:
            keyboard.append([
                InlineKeyboardButton(
                    result_title(record),
                    callback_data=f"drug:{record_id}",
                )
            ])

    page_buttons: list[InlineKeyboardButton] = []

    if page > 0:
        page_buttons.append(
            InlineKeyboardButton("⬅️", callback_data=f"results_page:{page - 1}")
        )

    if end_index < total:
        page_buttons.append(
            InlineKeyboardButton("➡️", callback_data=f"results_page:{page + 1}")
        )

    if page_buttons:
        keyboard.append(page_buttons)

    new_search_callback = {
        "all": "drug_search",
        "insulin": "insulin",
        "strips": "strips",
    }.get(mode, "drug_search")

    keyboard.extend([
        [InlineKeyboardButton("🔎 Новий пошук", callback_data=new_search_callback)],
        [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])

    text = (
        f"🔎 Запит: {search_text}\n"
        f"Знайдено: {total}\n"
        f"Сторінка {page + 1} з {total_pages}"
    )

    markup = InlineKeyboardMarkup(keyboard)

    if edit:
        await message.edit_message_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()
    await update.message.reply_text(
        "Оберіть розділ:",
        reply_markup=main_menu_markup(),
    )


async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    await query.answer()
    data_cb = query.data

    if data_cb == "main_menu":
        context.user_data.clear()
        await query.edit_message_text(
            "Оберіть розділ:",
            reply_markup=main_menu_markup(),
        )
        return

    if data_cb == "knowledge":
        context.user_data.pop("search_mode", None)

        keyboard = [
            [InlineKeyboardButton(category, callback_data=safe_callback(category))]
            for category in knowledge_tree
        ]
        keyboard.append([
            InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")
        ])

        await query.edit_message_text(
            "📚 Оберіть категорію:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data_cb == "drugs":
        context.user_data.pop("search_mode", None)
        await query.edit_message_text(
            "Оберіть дію:",
            reply_markup=drugs_menu_markup(),
        )
        return

    if data_cb in {"drug_search", "insulin", "strips"}:
        mode = {
            "drug_search": "all",
            "insulin": "insulin",
            "strips": "strips",
        }[data_cb]

        context.user_data["search_mode"] = mode
        context.user_data.pop("search_result_ids", None)
        context.user_data.pop("search_text", None)

        prompt = {
            "all": "🔎 Введіть назву препарату або діючу речовину:",
            "insulin": "💉 Введіть назву інсуліну або діючу речовину:",
            "strips": "🩸 Введіть назву тест-смужок:",
        }[mode]

        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    if data_cb.startswith("results_page:"):
        try:
            page = int(data_cb.split(":", 1)[1])
        except ValueError:
            return

        await show_search_results(query, context, page=page, edit=True)
        return

    if data_cb.startswith("drug:"):
        try:
            record_id = int(data_cb.split(":", 1)[1])
        except ValueError:
            return

        record = drug_records_by_id.get(record_id)

        if not record:
            await query.edit_message_text(
                "Запис не знайдено. Виконайте пошук ще раз.",
                reply_markup=drugs_menu_markup(),
            )
            return

        mode = context.user_data.get("search_mode", "all")
        keyboard: list[list[InlineKeyboardButton]] = []

        if record["active_substance"] and record["dosage"]:
            keyboard.append([
                InlineKeyboardButton(
                    "🔄 Підібрати аналоги",
                    callback_data=f"analogs:{record_id}",
                )
            ])

        if context.user_data.get("search_result_ids"):
            current_page = context.user_data.get("results_page", 0)
            keyboard.append([
                InlineKeyboardButton(
                    "⬅️ До результатів",
                    callback_data=f"results_page:{current_page}",
                )
            ])

        keyboard.extend([
            [
                InlineKeyboardButton(
                    "🔎 Новий пошук",
                    callback_data={
                        "all": "drug_search",
                        "insulin": "insulin",
                        "strips": "strips",
                    }.get(mode, "drug_search"),
                )
            ],
            [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
        ])

        await query.edit_message_text(
            format_drug_card(record),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data_cb.startswith("analogs:"):
        try:
            record_id = int(data_cb.split(":", 1)[1])
        except ValueError:
            return

        source_record = drug_records_by_id.get(record_id)

        if not source_record:
            return

        analogs = find_analogs(source_record)

        if not analogs:
            await query.edit_message_text(
                "Аналогів з такою самою діючою речовиною "
                "та дозуванням не знайдено.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Назад до препарату",
                            callback_data=f"drug:{record_id}",
                        )
                    ],
                    [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
                ]),
            )
            return

        context.user_data["search_result_ids"] = [item["id"] for item in analogs]
        context.user_data["search_text"] = (
            f"Аналоги: {source_record['active_substance']} — "
            f"{source_record['dosage']}"
        )
        context.user_data["results_page"] = 0

        await show_search_results(query, context, page=0, edit=True)
        return

    knowledge_action = knowledge_callbacks.get(data_cb)

    if knowledge_action:
        action_type = knowledge_action[0]

        if action_type == "category":
            category = knowledge_action[1]

            keyboard = [
                [
                    InlineKeyboardButton(
                        subtopic,
                        callback_data=safe_callback(f"{category}|{subtopic}"),
                    )
                ]
                for subtopic in knowledge_tree[category]
            ]
            keyboard.extend([
                [InlineKeyboardButton("⬅️ Назад", callback_data="knowledge")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ])

            await query.edit_message_text(
                f"Категорія: {category}\nОберіть підтему:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        if action_type == "subtopic":
            category = knowledge_action[1]
            subtopic = knowledge_action[2]

            keyboard = [
                [
                    InlineKeyboardButton(
                        question,
                        callback_data=safe_callback(
                            f"{category}|{subtopic}|{question}"
                        ),
                    )
                ]
                for question in knowledge_tree[category][subtopic]
            ]
            keyboard.extend([
                [
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data=safe_callback(category),
                    )
                ],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ])

            await query.edit_message_text(
                f"Підтема: {subtopic}\nОберіть питання:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        if action_type == "question":
            category = knowledge_action[1]
            subtopic = knowledge_action[2]
            question = knowledge_action[3]
            answer = knowledge_tree[category][subtopic].get(
                question,
                "Відповідь не вказана.",
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "⬅️ Назад",
                        callback_data=safe_callback(f"{category}|{subtopic}"),
                    )
                ],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]

            await query.edit_message_text(
                answer or "Відповідь не вказана.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )


async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    mode = context.user_data.get("search_mode")

    if not mode:
        return

    search_text = clean_text(update.message.text)

    if not search_text:
        await update.message.reply_text(
            "Введіть назву препарату або діючу речовину."
        )
        return

    results = search_drugs(search_text, mode=mode)

    if not results:
        await update.message.reply_text(
            "❌ Нічого не знайдено.\n\n"
            "Перевірте написання або введіть частину назви.",
            reply_markup=search_navigation_markup(mode),
        )
        return

    context.user_data["search_text"] = search_text
    context.user_data["search_result_ids"] = [item["id"] for item in results]
    context.user_data["results_page"] = 0

    await show_search_results(
        update.message,
        context,
        page=0,
        edit=False,
    )


if __name__ == "__main__":
    token = os.getenv("TELEGRAM_TOKEN")

    if not token:
        raise RuntimeError("Змінна TELEGRAM_TOKEN не задана в Railway.")

    application = ApplicationBuilder().token(token).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    print("Бот запущений...")
    application.run_polling()
