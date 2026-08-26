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
SOCIAL_RESULTS_PER_PAGE = 10
SOCIAL_PROGRAMS_PER_PAGE = 10
KNOWLEDGE_SEARCH_RESULTS_PER_PAGE = 10

SOCIAL_SPREADSHEET_ID = "197_It5B9M2d5pX2m3igzrQGF3snHs9mzOzPuAQ_SUjU"

SOCIAL_MAIN_SHEET_CANDIDATES = [
    "Аптеки учасники оновлено 18.11",
    "Аптеки учасники оновлено 15.06",
]

SOCIAL_ZR_SHEET_CANDIDATES = [
    "Аптеки ЗР",
    "ЗР",
]

SOCIAL_CONDITIONS_SHEET_CANDIDATES = [
    "Умови соц.програм",
    "Умови соц програм",
]

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


def preserve_multiline_text(value: Any) -> str:
    """Зберігає переноси рядків у відповідях із Google Sheets."""
    if value is None:
        return ""

    text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Прибираємо зайві пробіли в кожному рядку, але не самі переноси.
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]

    # Якщо пункти з 📌 були вставлені в один рядок, розділяємо їх.
    normalized_lines: list[str] = []
    for line in lines:
        if not line:
            normalized_lines.append("")
            continue

        parts = re.split(r"(?=📌)", line)
        for part in parts:
            part = part.strip()
            if not part:
                continue
            part = re.sub(r"^📌\s*", "📌 ", part)
            normalized_lines.append(part)

    # Не допускаємо більше одного порожнього рядка поспіль.
    result: list[str] = []
    for line in normalized_lines:
        if not line and (not result or not result[-1]):
            continue
        result.append(line)

    return "\n".join(result).strip()


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


def last_named_column(headers: list[str]) -> int | None:
    """Повертає 1-based номер останнього непорожнього заголовка."""
    for index in range(len(headers), 0, -1):
        if clean_text(header_at(headers, index)):
            return index
    return None


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

# Універсальна структура:
# Група (необов'язково) → Категорія → Підкатегорія (необов'язково)
# → Питання → Відповідь.
knowledge_tree: dict[str, Any] = {}
knowledge_entries: list[dict[str, str]] = []
knowledge_callback_to_path: dict[str, tuple[str, ...]] = {}
knowledge_path_to_callback: dict[tuple[str, ...], str] = {}


def knowledge_row_value(
    source_row: dict[str, Any],
    *possible_headers: str,
    preserve_lines: bool = False,
) -> str:
    for header in possible_headers:
        raw_value = source_row.get(header, "")
        value = (
            preserve_multiline_text(raw_value)
            if preserve_lines
            else clean_text(raw_value)
        )
        if value:
            return value
    return ""


for source_row in knowledge_rows:
    group = knowledge_row_value(
        source_row,
        "Група",
        "Рівень 1",
        "Меню",
    )
    category = knowledge_row_value(
        source_row,
        "Категорія",
        "Рівень 2",
    )
    subcategory = knowledge_row_value(
        source_row,
        "Підкатегорія",
        "Підтема",
        "Рівень 3",
    )
    question = knowledge_row_value(
        source_row,
        "Питання",
        "Запитання",
    )
    answer = knowledge_row_value(
        source_row,
        "Відповідь",
        preserve_lines=True,
    )

    if not category or not question:
        continue

    entry = {
        "group": group,
        "category": category,
        "subcategory": subcategory,
        "question": question,
        "answer": answer,
    }
    knowledge_entries.append(entry)

    path_parts = [
        value
        for value in [group, category, subcategory, question]
        if value
    ]

    node = knowledge_tree

    for part in path_parts[:-1]:
        node = node.setdefault(part, {})

    node[path_parts[-1]] = {
        "__answer__": answer or "Відповідь не вказана."
    }


def register_knowledge_callbacks(
    node: dict[str, Any],
    path: tuple[str, ...] = (),
) -> None:
    for title, child in node.items():
        if title == "__answer__":
            continue

        child_path = path + (title,)
        callback = f"kb:{len(knowledge_callback_to_path) + 1}"

        knowledge_callback_to_path[callback] = child_path
        knowledge_path_to_callback[child_path] = callback

        if isinstance(child, dict) and "__answer__" not in child:
            register_knowledge_callbacks(child, child_path)


register_knowledge_callbacks(knowledge_tree)


def knowledge_node(path: tuple[str, ...]) -> dict[str, Any] | None:
    node: Any = knowledge_tree

    for part in path:
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]

    return node if isinstance(node, dict) else None


def knowledge_menu_markup(
    path: tuple[str, ...] = (),
) -> InlineKeyboardMarkup:
    node = knowledge_node(path) if path else knowledge_tree
    keyboard: list[list[InlineKeyboardButton]] = []

    if node:
        for title, child in node.items():
            if title == "__answer__":
                continue

            child_path = path + (title,)
            callback = knowledge_path_to_callback.get(child_path)

            if callback:
                keyboard.append([
                    InlineKeyboardButton(
                        shorten_button(title, max_length=60),
                        callback_data=callback,
                    )
                ])

    if path:
        parent_path = path[:-1]
        back_callback = (
            knowledge_path_to_callback.get(parent_path)
            if parent_path
            else "knowledge"
        )
        keyboard.append([
            InlineKeyboardButton(
                "⬅️ Назад",
                callback_data=back_callback or "knowledge",
            )
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")
        ])

    keyboard.append([
        InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")
    ])

    return InlineKeyboardMarkup(keyboard)


def find_knowledge_answer(
    *,
    category_contains: str,
    question_contains: str,
) -> str:
    category_query = normalize(category_contains)
    question_query = normalize(question_contains)

    for entry in knowledge_entries:
        category_text = normalize(
            " ".join([
                entry["group"],
                entry["category"],
                entry["subcategory"],
            ])
        )
        question_text = normalize(entry["question"])

        if (
            category_query in category_text
            and question_query in question_text
        ):
            return entry["answer"]

    return ""


def search_knowledge(search_text: str) -> list[dict[str, Any]]:
    query = normalize(search_text)
    if not query:
        return []

    tokens = [token for token in query.split() if len(token) >= 2]
    results: list[tuple[int, dict[str, Any]]] = []

    for entry in knowledge_entries:
        path = tuple(
            value
            for value in [
                entry["group"],
                entry["category"],
                entry["subcategory"],
                entry["question"],
            ]
            if value
        )
        callback = knowledge_path_to_callback.get(path)
        if not callback:
            continue

        title_text = normalize(" ".join(path))
        answer_text = normalize(entry["answer"])
        full_text = f"{title_text} {answer_text}"

        score = 0
        if query == normalize(entry["question"]):
            score += 1000
        if query in normalize(entry["question"]):
            score += 500
        if query in title_text:
            score += 300
        if query in answer_text:
            score += 100

        matched_tokens = sum(1 for token in tokens if token in full_text)
        if tokens and matched_tokens == len(tokens):
            score += 200 + matched_tokens * 10
        elif matched_tokens:
            score += matched_tokens * 10

        if score:
            results.append((score, {
                "path": path,
                "callback": callback,
                "question": entry["question"],
                "category": entry["subcategory"] or entry["category"],
            }))

    results.sort(
        key=lambda item: (
            -item[0],
            normalize(item[1]["question"]),
        )
    )

    unique: list[dict[str, Any]] = []
    seen_callbacks: set[str] = set()
    for _, item in results:
        if item["callback"] in seen_callbacks:
            continue
        seen_callbacks.add(item["callback"])
        unique.append(item)

    return unique


def knowledge_search_results_markup(
    results: list[dict[str, Any]],
    page: int = 0,
) -> InlineKeyboardMarkup:
    total_pages = max(
        1,
        (len(results) + KNOWLEDGE_SEARCH_RESULTS_PER_PAGE - 1)
        // KNOWLEDGE_SEARCH_RESULTS_PER_PAGE,
    )
    page = max(0, min(page, total_pages - 1))
    start = page * KNOWLEDGE_SEARCH_RESULTS_PER_PAGE
    end = min(start + KNOWLEDGE_SEARCH_RESULTS_PER_PAGE, len(results))

    keyboard: list[list[InlineKeyboardButton]] = []
    for item in results[start:end]:
        title = item["question"]
        category = item["category"]
        if category:
            title = f"{title} — {category}"
        keyboard.append([
            InlineKeyboardButton(
                shorten_button(title, max_length=60),
                callback_data=item["callback"],
            )
        ])

    pagination: list[InlineKeyboardButton] = []
    if page > 0:
        pagination.append(InlineKeyboardButton(
            "⬅️", callback_data=f"knowledge_search_page:{page - 1}"
        ))
    if end < len(results):
        pagination.append(InlineKeyboardButton(
            "➡️", callback_data=f"knowledge_search_page:{page + 1}"
        ))
    if pagination:
        keyboard.append(pagination)

    keyboard.extend([
        [InlineKeyboardButton("🔍 Новий пошук", callback_data="knowledge_search")],
        [InlineKeyboardButton("📚 База знань", callback_data="knowledge")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])
    return InlineKeyboardMarkup(keyboard)


async def show_knowledge_search_results(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 0,
    edit: bool = False,
) -> None:
    results = context.user_data.get("knowledge_search_results", [])
    search_text = context.user_data.get("knowledge_search_text", "")
    total_pages = max(
        1,
        (len(results) + KNOWLEDGE_SEARCH_RESULTS_PER_PAGE - 1)
        // KNOWLEDGE_SEARCH_RESULTS_PER_PAGE,
    )
    page = max(0, min(page, total_pages - 1))
    context.user_data["knowledge_search_page"] = page

    text = (
        f"🔍 Запит: {search_text}\n"
        f"Знайдено: {len(results)}\n"
        f"Сторінка {page + 1} з {total_pages}"
    )
    markup = knowledge_search_results_markup(results, page)

    if edit:
        await message.edit_message_text(text, reply_markup=markup)
    else:
        await message.reply_text(text, reply_markup=markup)


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
            # Для інсулінів додатково транслюємо останній стовпчик
            # з вкладки «РЕЄСТР ІНСУЛІНИ». Номер колонки не зашитий
            # вручну, тому якщо перед ним додадуть нові колонки,
            # код все одно візьме саме останню.
            "last_column_number": (
                last_named_column(headers)
                if sheet_name == INSULIN_SHEET
                else None
            ),
            "last_column_header": (
                header_at(headers, last_named_column(headers))
                if (
                    sheet_name == INSULIN_SHEET
                    and last_named_column(headers) is not None
                )
                else ""
            ),
            "last_column_value": (
                value_at(row, last_named_column(headers))
                if (
                    sheet_name == INSULIN_SHEET
                    and last_named_column(headers) is not None
                )
                else ""
            ),
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


# =========================================================
# СОЦІАЛЬНІ ПРОГРАМИ
# =========================================================

social_book = gc.open_by_key(SOCIAL_SPREADSHEET_ID)


def get_existing_worksheet(candidates: list[str]):
    """Повертає першу наявну вкладку із заданого списку."""
    existing_titles = {worksheet.title for worksheet in social_book.worksheets()}

    for title in candidates:
        if title in existing_titles:
            return social_book.worksheet(title)

    raise RuntimeError(
        "Не знайдено жодної вкладки: " + ", ".join(candidates)
    )


social_main_sheet = get_existing_worksheet(SOCIAL_MAIN_SHEET_CANDIDATES)
social_zr_sheet = get_existing_worksheet(SOCIAL_ZR_SHEET_CANDIDATES)
social_conditions_sheet = get_existing_worksheet(
    SOCIAL_CONDITIONS_SHEET_CANDIDATES
)


def extract_short_number(department: str, fallback: str = "") -> str:
    """Витягує короткий номер із початку поля «Підрозділ»."""
    match = re.match(r"\s*(\d+)", clean_text(department))

    if match:
        return match.group(1)

    return clean_text(fallback)


def normalize_program_name(value: str) -> str:
    """Нормалізація назв програм для зіставлення."""
    text = normalize(value)
    text = text.replace("’", "'").replace("`", "'")
    text = text.replace("+", " плюс ")
    text = re.sub(r"[«»\"'“”„‟()]", " ", text)
    text = re.sub(r"[^a-zа-яіїєґ0-9]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def normalized_program_tokens(value: str) -> set[str]:
    ignored = {
        "україна", "україни", "програма", "новий", "нова",
        "закрита", "закрито", "діє", "працює", "до", "без",
        "пролонгації", "для", "та", "і", "з", "в", "на",
    }

    return {
        token
        for token in normalize_program_name(value).split()
        if len(token) >= 3 and token not in ignored
    }


def canonical_program_key(program_name: str) -> str:
    """
    Канонічний ключ програми.
    Використовується для відомих варіантів написання.
    """
    text = normalize_program_name(program_name)

    # TEVA: одна програма має різні заголовки у трьох вкладках:
    # «Моменти життя від Тева» — умови,
    # «Моменти життя від Тева Аджові» — основні аптеки,
    # «Моменти життя від ТЕВА» — Аптеки ЗР.
    # Для логіки бота всі вони є однією програмою.
    if text.startswith("моменти життя від тева"):
        return "моменти життя від тева"

    replacements = {
        "астразенека терапія": "астразенека терапія плюс",
        "астразенека терапіяплюс": "астразенека терапія плюс",
        "астразенека терапія плюс": "астразенека терапія плюс",
        "біокодекс асакард": "біокодекс асакард",
        "біокодекс україна асакард": "біокодекс асакард",
    }

    return replacements.get(text, text)



# Явна карта відповідностей назв однієї програми між різними вкладками.
# Це надійніше, ніж fuzzy-пошук за спільними словами.
PROGRAM_ALIASES = {
    # AstraZeneca
    canonical_program_key('АстраЗенека "ТЕРАПІЯ+"'):
        canonical_program_key('АстраЗенека "ТЕРАПІЯПЛЮС"'),

    # Abbott / Heptral
    canonical_program_key('Abbott card "РАЗОМ" (Гептрали)'):
        canonical_program_key('Abbott card "підтримака пацієнтів РАЗОМ"'),
    canonical_program_key('ЕБОТТ КАРД ГЕПТРАЛ'):
        canonical_program_key('Abbott card "підтримака пацієнтів РАЗОМ"'),

    # Mobile Medical
    canonical_program_key('Мобіль Медикал "ЗБЕРЕЖЕМО ЗДОРОВ’Я РАЗОМ"'):
        canonical_program_key('Мобіль Медикал "ЗБЕРЕЖЕМО ЗДОРОВ’Я РАЗОМ"'),

    # TEVA
    canonical_program_key('Моменти життя від ТЕВА'):
        canonical_program_key('ТЕВА "Моменти життя"'),
    canonical_program_key('Моменти життя від Тева  Аджові'):
        canonical_program_key('ТЕВА "Моменти життя"'),

    # Darnytsia etc.
    canonical_program_key('Дарниця "ДОСТУПНИЙ ЗАХИСТ ПЕЧІНКИ І ЖОВЧНОГО МІХУРА"'):
        canonical_program_key('Дарниця "ДОСТУПНИЙ ЗАХИСТ ПЕЧІНКИ І ЖОВЧНОГО МІХУРА" (Урсохол)'),
    canonical_program_key('Дарниця "ШЛЯХ ДО ЗДОРОВОГО СЕРЦЯ"'):
        canonical_program_key('Дарниця "ШЛЯХ ДО ЗДОРОВОГО СЕРЦЯ" (Ефез)'),
    canonical_program_key('Дарниця "ШЛЯХ ДО ЗДОРОВОГО СЕРЦЯ" (Ефез) закрита програма'):
        canonical_program_key('Дарниця "ШЛЯХ ДО ЗДОРОВОГО СЕРЦЯ" (Ефез)'),
    canonical_program_key('Дарниця "ОПАНУЙТЕ СВІЙ ТИСК"'):
        canonical_program_key('Дарниця "Опануйте свій тиск"'),
    canonical_program_key('Дарниця "Опануйте свій тиск" закрита програма'):
        canonical_program_key('Дарниця "Опануйте свій тиск"'),
    canonical_program_key('Дарниця "МІГРЕНЬ НЕ ВИРОК"'):
        canonical_program_key('Дарниця "Мігрень не вирок"'),
    canonical_program_key('Дарниця "Мігрень не вирок" (Ельптан)'):
        canonical_program_key('Дарниця "Мігрень не вирок"'),
    canonical_program_key('Дарниця "ВІЛЬНИЙ РУХ БЕЗ БОЛЮ"'):
        canonical_program_key('Дарниця "Вільний рух без болю"'),
    canonical_program_key('Дарниця "NEUROCARD" (Альфахолін і цитімакс)'):
        canonical_program_key('«NEUROCARD» від виробника Дарниця'),

    # Biocodex
    canonical_program_key('Біокодекс "АСАКАРД"'):
        canonical_program_key('«Асакард» від виробника ТОВ «БІОКОДЕКС УКРАЇНА»'),
    canonical_program_key('БІОКОДЕКС УКРАЇНА "Асакард"'):
        canonical_program_key('«Асакард» від виробника ТОВ «БІОКОДЕКС УКРАЇНА»'),

    # Other names
    canonical_program_key('Артеріум "КЛЮЧ ДО ЖИТТЯ" діє до 31.07.2026'):
        canonical_program_key('Артеріум "КЛЮЧ ДО ЖИТТЯ"'),
    canonical_program_key('Кусум Фарм "MAGIC CARD" НОВИЙ закрита програма'):
        canonical_program_key('Кусум Фарм "MAGIC CARD"'),
    canonical_program_key('"Життя без болю при подагрі" (Єврофеб)'):
        canonical_program_key('Euro Lifecare "Життя без болю при подагрі"'),
    canonical_program_key('Допомога пацієнту (БХФЗ) діє до 01.07.2026'):
        canonical_program_key('БХФЗ "Допомога пацієнту"'),
    canonical_program_key('Допомога пацієнту (БХФЗ) ДІЄ ДО 1.07.2026'):
        canonical_program_key('БХФЗ "Допомога пацієнту"'),
    canonical_program_key('Час бачити (ХілоКеа) Медікард'):
        canonical_program_key('Час бачити'),
    canonical_program_key('Відновлення якості життя (АльфаНормікс)'):
        canonical_program_key('Відновлення якості життя'),
    canonical_program_key('Шлях до відновлення очей (ХілоКеа)'):
        canonical_program_key('Шлях до відновлення очей'),
    canonical_program_key('Шлях до відновлення очей (ХілоКеа) ЗАКРИТА'):
        canonical_program_key('Шлях до відновлення очей'),
    canonical_program_key('Керуй діабетом з ТОЖЕО СОЛОСТАР'):
        canonical_program_key('Керуй діабетом з ТОЖЕО, СОЛІКВА'),

    # KRKA closed program: conditions only, no pharmacies.
    canonical_program_key('КРКА "ЗАХИСТІТЬ СУДИНИ - ЗБЕРЕЖІТЬ СЕРЦЕ" та "ПОКРАЩЕНИЙ КРОВООБІГ ДЛЯ ЗДОРОВ’Я МОЗКУ"'):
        canonical_program_key('КРКА "ЗАХИСТІТЬ СУДИНИ - ЗБЕРЕЖІТЬ СЕРЦЕ" та "ПОКРАЩЕНИЙ КРОВООБІГ ДЛЯ ЗДОРОВ’Я МОЗКУ"'),

    # NIR: must remain a separate "РАЗОМ" program.
    canonical_program_key('«РАЗОМ ДО ЗДОРОВОГО МАЙБУТНЬОГО» від виробника НІР'):
        canonical_program_key('«РАЗОМ ДО ЗДОРОВОГО МАЙБУТНЬОГО» від виробника НІР'),
}


def program_alias_key(value: str) -> str:
    key = canonical_program_key(value)
    return PROGRAM_ALIASES.get(key, key)


def program_similarity(left: str, right: str) -> float:
    left_key = program_alias_key(left)
    right_key = program_alias_key(right)

    if left_key == right_key:
        return 1.0

    left_tokens = normalized_program_tokens(left)
    right_tokens = normalized_program_tokens(right)

    if not left_tokens or not right_tokens:
        return 0.0

    common = left_tokens & right_tokens
    union = left_tokens | right_tokens

    generic_words = {
        "разом",
        "здоровя",
        "здорового",
        "пацієнт",
        "пацієнта",
        "пацієнтів",
        "підтримка",
        "підтримака",
        "майбутнього",
    }

    distinctive_common = {
        token for token in common
        if token not in generic_words
    }

    if not distinctive_common:
        return 0.0

    score = len(common) / len(union)

    if max((len(token) for token in distinctive_common), default=0) >= 7:
        score += 0.35

    return min(score, 1.0)


def resolve_to_main_program(
    program_name: str,
    main_programs: list[str],
) -> str | None:
    """
    Спочатку використовує явні aliases, потім лише безпечний fuzzy fallback.
    """
    if not program_name:
        return None

    key = program_alias_key(program_name)

    for main_name in main_programs:
        if program_alias_key(main_name) == key:
            return main_name

    best_name = None
    best_score = 0.0

    for main_name in main_programs:
        score = program_similarity(program_name, main_name)

        if score > best_score:
            best_score = score
            best_name = main_name

    if best_score >= 0.70:
        return best_name

    return None


def normalize_medicine_name(value: str) -> str:
    """
    Нормалізація лише для пошуку препаратів.
    Українські «і» та «и» прирівнюються.
    """
    text = normalize(value)
    text = text.replace("і", "и")
    text = text.replace("ї", "и")
    text = text.replace("й", "и")
    text = re.sub(r"[^a-zа-яєґ0-9]+", " ", text, flags=re.IGNORECASE)
    return " ".join(text.split())


def package_word(value: str) -> str:
    """Повертає правильну форму слова «упаковка»."""
    match = re.search(r"\d+", clean_text(value))

    if not match:
        return "упаковок"

    number = int(match.group(0))
    last_two = number % 100
    last = number % 10

    if 11 <= last_two <= 14:
        return "упаковок"

    if last == 1:
        return "упаковка"

    if last in {2, 3, 4}:
        return "упаковки"

    return "упаковок"


ZR_PROGRAM_ALIASES = {
    'abbott card разом гептрали': 'ЕБОТТ КАРД ГЕПТРАЛ',
    'астразенека терапія плюс': 'АстраЗенека "ТЕРАПІЯПЛЮС"',
    'мовіхелс свобода руху з мові хелс': 'МовіХелс "СВОБОДА РУХУ З МОВІ ХЕЛС"',
    'астразенека карта надії': 'АстраЗенека "КАРТА НАДІЇ"',
    'ново нордіск розумний старт': 'Ново Нордіск "РОЗУМНИЙ СТАРТ"',
    'геолік захисти своє майбутнє': 'Геолік "ЗАХИСТИ СВОЄ МАЙБУТНЄ"',
    'ромфарм допомога суглобам': 'Ромфарм "ДОПОМОГА СУГЛОБАМ"',
    'рош за межі обмежень за межі рс': 'Рош "З НАДІЄЮ В МАЙБУТНЄ"',
    'артеріум ключ до життя': 'Артеріум "КЛЮЧ ДО ЖИТТЯ" діє до 31.07.2026',
    'мсд ключ надії': 'МСД "КЛЮЧ НАДІЇ"',
    'дарниця шлях до здорового серця': 'Дарниця "ШЛЯХ ДО ЗДОРОВОГО СЕРЦЯ" (Ефез) закрита програма',
    'кусум фарм magic card': 'Кусум Фарм "MAGIC CARD" НОВИЙ закрита програма',
    'мобіль медикал збережемо здоров я разом': 'Мобіль Медикал "ЗБЕРЕЖЕМО ЗДОРОВ’Я РАЗОМ"',
    'астеллас червона калина': 'Астеллас "ЧЕРВОНА КАЛИНА"',
    'дарниця доступний захист печінки і жовчного міхура': 'Дарниця "ДОСТУПНИЙ ЗАХИСТ ПЕЧІНКИ І ЖОВЧНОГО МІХУРА" (Урсохол)',
    'біокодекс асакард': 'БІОКОДЕКС УКРАЇНА "Асакард"',
    'дарниця вільний рух без болю': 'Дарниця "Вільний рух без болю"',
    'дарниця опануйте свій тиск': 'Дарниця "Опануйте свій тиск" закрита програма',
    'дарниця мігрень не вирок': 'Дарниця "Мігрень не вирок" (Ельптан)',
    'життя без болі при подагрі': '"Життя без болю при подагрі" (Єврофеб)',
    'допомога пацієнту бхфз діє до 1 07 2026': 'Допомога пацієнту (БХФЗ) діє до 01.07.2026',
    'дарниця neurocard альфахолін і цитімакс': 'Дарниця "NEUROCARD" (Альфахолін і цитімакс)',
    'моменти життя від тева': 'Моменти життя від Тева  Аджові',
    'пакунок малюка': 'Пакунок малюка (ЗР)',
    'відновлення якості життя альфанормікс': 'Відновлення якості життя (АльфаНормікс)',
    'шлях до відновлення очей хілокеа': 'Шлях до відновлення очей (ХілоКеа) ЗАКРИТА',
}


def zr_alias_key(value: str) -> str:
    return normalize_program_name(value)


def program_status(program_name: str) -> str:
    """active → closing → closed."""
    normalized = normalize(program_name)

    if "закрит" in normalized:
        return "closed"

    if "діє до" in normalized or "працює до" in normalized:
        return "closing"

    return "active"


def program_status_icon(program_name: str) -> str:
    status = program_status(program_name)

    if status == "closed":
        return "🔴"

    if status == "closing":
        return "🟡"

    return "🟢"


def program_sort_key(program_name: str) -> tuple[int, str]:
    order = {
        "active": 0,
        "closing": 1,
        "closed": 2,
    }

    return (
        order[program_status(program_name)],
        normalize(program_name),
    )


def load_social_sheet_records(
    worksheet,
    *,
    is_zr: bool,
    program_start_column: int,
    short_number_column: int,
    department_column: int,
    oblast_column: int,
    city_column: int,
    street_column: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Читає всі рядки незалежно від фільтрів у Google Sheets.
    Номери колонок тут 1-based.
    """
    rows = worksheet.get_all_values()

    if not rows:
        return [], []

    headers = [clean_text(value) for value in rows[0]]
    programs = [
        clean_text(value)
        for value in headers[program_start_column - 1:]
        if clean_text(value)
    ]

    records: list[dict[str, Any]] = []

    for row_number, row in enumerate(rows[1:], start=2):
        if not any(clean_text(value) for value in row):
            continue

        department = value_at(row, department_column)
        fallback_number = value_at(row, short_number_column)
        short_number = extract_short_number(department, fallback_number)

        oblast = value_at(row, oblast_column)
        city = value_at(row, city_column)
        street = value_at(row, street_column)

        for column_number in range(program_start_column, len(headers) + 1):
            program = header_at(headers, column_number)
            participation_value = value_at(row, column_number)

            if not program or not participation_value:
                continue

            if "відключено" in normalize(participation_value):
                continue

            records.append({
                "sheet_name": worksheet.title,
                "row_number": row_number,
                "program": program,
                "participation_value": participation_value,
                "short_number": short_number,
                "department": department,
                "oblast": oblast,
                "city": city,
                "street": street,
                "is_zr": is_zr,
            })

    return records, programs


# Основна вкладка:
# A — №, B — Підрозділ, D — Область, E — Місто, F — Адреса,
# програми починаються з I.
social_main_records, social_main_programs = load_social_sheet_records(
    social_main_sheet,
    is_zr=False,
    program_start_column=9,
    short_number_column=1,
    department_column=2,
    oblast_column=4,
    city_column=5,
    street_column=6,
)

# Вкладка «Аптеки ЗР»:
# A — №, B — Підрозділ, F — Область, G — Місто, H — Адреса,
# програми починаються з K.
social_zr_records, social_zr_programs = load_social_sheet_records(
    social_zr_sheet,
    is_zr=True,
    program_start_column=11,
    short_number_column=1,
    department_column=2,
    oblast_column=6,
    city_column=7,
    street_column=8,
)


def _conditions_row_is_header(col_b: str, col_c: str, col_d: str) -> bool:
    normalized_product = normalize(col_c)
    normalized_limit = normalize(col_b)
    normalized_discount = normalize(col_d)

    return (
        ("препарат" in normalized_product and "дозування" in normalized_product)
        or ("ліміт" in normalized_limit and "знижка" in normalized_discount)
    )


def _program_name_candidate(col_b: str, col_c: str, col_d: str) -> str:
    """Повертає назву програми, але не службові заголовки таблиці."""
    if _conditions_row_is_header(col_b, col_c, col_d):
        return ""

    blocked = {
        "програма",
        "умови",
        "ліміт",
        "ліміт упаковок",
        "препарат",
        "препарат та дозування",
        "препарат або дозування",
        "знижка",
    }

    for value in (col_b, col_c, col_d):
        cleaned = clean_text(value)
        if cleaned and normalize(cleaned) not in blocked:
            return cleaned

    return ""


def parse_social_program_conditions_rows(
    rows: list[list[str]],
) -> dict[str, dict[str, Any]]:
    """
    Парсер вкладки «Умови соц.програм».

    Головне правило структури:
    НАЗВА ПРОГРАМИ
    Ліміт уп/картка | Препарат/Дозування | Знижка, %
    препарати/умови цієї програми...

    Наступний такий самий рядок заголовків = початок наступної програми.

    Абсолютні номери рядків не використовуються, тому вставлення,
    видалення та порожні рядки не ламають розподіл препаратів.
    """
    conditions: dict[str, dict[str, Any]] = {}

    if not rows:
        return conditions

    SERVICE_LABELS = {
        "проект",
        "програма",
        "умови",
        "медіакард/програма",
        "медіакард програма",
        "медіакард",
    }

    def is_table_header(row: list[str]) -> bool:
        limit_text = normalize(value_at(row, 2))
        product_text = normalize(value_at(row, 3))
        discount_text = normalize(value_at(row, 4))

        # Підтримуємо варіанти «Ліміт уп/картка», «Ліміт уп/карта» тощо.
        has_limit = "ліміт" in limit_text and (
            "уп" in limit_text or "карт" in limit_text
        )
        has_product = (
            "препарат" in product_text
            and "дозув" in product_text
        )
        has_discount = "зниж" in discount_text

        return has_limit and has_product and has_discount

    def row_values(row: list[str]) -> list[str]:
        return [clean_text(value) for value in row]

    def is_blank(row: list[str]) -> bool:
        return not any(row_values(row))

    def find_program_title(header_index: int) -> tuple[str, int | None]:
        """
        Назва програми — найближчий змістовний рядок НАД її
        «Ліміт / Препарат / Знижка» заголовком.

        Порожні рядки та службові «ПРОЕКТ», «Програма» пропускаються.
        """
        # Не прив'язуємось до номерів рядків. Шукаємо назад аж до
        # попереднього table-header або початку файла.
        idx = header_index - 1

        while idx >= 0:
            row = rows[idx]

            if is_table_header(row):
                break

            if is_blank(row):
                idx -= 1
                continue

            label_a = normalize(value_at(row, 1))

            # У merged-рядках назва найчастіше лежить у B/C/D.
            candidate = _program_name_candidate(
                clean_text(value_at(row, 2)),
                clean_text(value_at(row, 3)),
                clean_text(value_at(row, 4)),
            )

            if candidate:
                normalized_candidate = normalize(candidate)

                # Не приймаємо службові назви за програму.
                if normalized_candidate not in SERVICE_LABELS:
                    return clean_text(candidate), idx

            # Якщо назва раптом лежить в A, але це не службове слово.
            col_a = clean_text(value_at(row, 1))
            if col_a and label_a not in SERVICE_LABELS:
                # Не беремо випадковий рядок препарату як назву.
                if not clean_text(value_at(row, 3)):
                    return col_a, idx

            idx -= 1

        return "", None

    # Кожен такий заголовок — жорстка межа окремої програми.
    headers = [
        index
        for index, row in enumerate(rows)
        if is_table_header(row)
    ]

    logger.info(
        "У вкладці умов знайдено %s структурних блоків програм.",
        len(headers),
    )

    for block_index, header_index in enumerate(headers):
        next_header = (
            headers[block_index + 1]
            if block_index + 1 < len(headers)
            else len(rows)
        )

        program_name, title_index = find_program_title(header_index)

        if not program_name:
            logger.warning(
                "Пропущено блок біля рядка %s: не знайдено назву програми.",
                header_index + 1,
            )
            continue

        title_row = rows[title_index] if title_index is not None else []
        program_key = normalize(program_name)

        program_data = conditions.setdefault(
            program_key,
            {
                "program": program_name,
                "status": clean_text(value_at(title_row, 11)),
                "categories": [],
                "items": [],
            },
        )

        # КРИТИЧНО: читаємо тільки від header+1 до наступного header.
        # Отже Аджові фізично не може потрапити в ТОЖЕО, а Гептрал —
        # у TEVA/ТОЖЕО, якщо кожен блок має свій B/C/D заголовок.
        for row_index in range(header_index + 1, next_header):
            row = rows[row_index]

            if is_blank(row):
                continue

            # Службові/нові назви між блоками не є препаратами.
            label_a = normalize(value_at(row, 1))
            if label_a in SERVICE_LABELS:
                continue

            limit_value = clean_text(value_at(row, 2))
            product = clean_text(value_at(row, 3))
            discount = clean_text(value_at(row, 4))
            status = clean_text(value_at(row, 11))
            category = clean_text(value_at(row, 12))

            if not product:
                continue

            # Захист від будь-яких повторних заголовків.
            if is_table_header(row):
                continue

            normalized_product = normalize(product)
            if (
                "препарат" in normalized_product
                and "дозув" in normalized_product
            ):
                continue

            if category and category not in program_data["categories"]:
                program_data["categories"].append(category)

            if status and not program_data.get("status"):
                program_data["status"] = status

            program_data["items"].append({
                "limit": limit_value,
                "product": product,
                "discount": discount,
                "category": category,
            })

        logger.info(
            "Програма %r: завантажено %s препаратів.",
            program_name,
            len(program_data["items"]),
        )

    return conditions


def load_social_program_conditions() -> dict[str, dict[str, Any]]:
    rows = social_conditions_sheet.get_all_values()
    return parse_social_program_conditions_rows(rows)


SOCIAL_PROGRAM_CONDITIONS = load_social_program_conditions()
logger.info(
    "Завантажено умови для %s соціальних програм.",
    len(SOCIAL_PROGRAM_CONDITIONS),
)


def find_social_program_conditions(program: str) -> dict[str, Any] | None:
    """
    Зіставляє назви однієї програми між різними вкладками.
    """
    selected_main = resolve_to_main_program(program, SOCIAL_PROGRAMS) or program
    selected_key = program_alias_key(selected_main)

    # Спочатку точний canonical match.
    for stored_name, data in SOCIAL_PROGRAM_CONDITIONS.items():
        stored_program = data.get("program", stored_name)
        if program_alias_key(stored_program) == selected_key:
            return data

    # Fallback для старих/нестандартних назв.
    best_match = None
    best_score = 0.0

    for stored_name, data in SOCIAL_PROGRAM_CONDITIONS.items():
        stored_program = data.get("program", stored_name)
        score = program_similarity(selected_main, stored_program)

        if score > best_score:
            best_score = score
            best_match = data

    if best_score >= 0.55:
        return best_match

    return None


def format_social_program_conditions(program: str) -> str:
    data = find_social_program_conditions(program)
    lines = [
        f"{program_status_icon(program)} <b>{html.escape(program)}</b>",
    ]

    if not data:
        lines.extend([
            "",
            "📝 Умови для цієї програми у вкладці не знайдено.",
        ])
        return "\n".join(lines)

    status = clean_text(data.get("status", ""))

    if status:
        lines.extend([
            "",
            f"📅 <b>Статус:</b> {html.escape(status)}",
        ])

    categories = [
        clean_text(value)
        for value in data.get("categories", [])
        if clean_text(value)
    ]

    if categories:
        lines.extend([
            "",
            "🏷 <b>Категорії товарів:</b>",
        ])
        lines.extend(
            f"• {html.escape(category)}"
            for category in categories
        )

    items = data.get("items", [])

    if items:
        lines.extend([
            "",
            "💊 <b>Умови та препарати:</b>",
        ])

        for item in items:
            product = clean_text(item.get("product", ""))
            limit_value = clean_text(item.get("limit", ""))
            discount = clean_text(item.get("discount", ""))

            if not product:
                continue

            lines.append("")
            lines.append(f"• <b>{html.escape(product)}</b>")

            if limit_value:
                lines.append(
                    f"  📦 Ліміт: {html.escape(limit_value)} "
                    f"{package_word(limit_value)}"
                )

            if discount:
                lines.append(
                    f"  💸 Знижка: {html.escape(discount)}"
                )

    if len(lines) == 1:
        lines.extend([
            "",
            "📝 Детальні умови не вказані.",
        ])

    return "\n".join(lines)


# ВІДОБРАЖУВАНІ назви програм беремо ТІЛЬКИ з вкладки «Умови соц.програм».
# Назви з аптечних вкладок використовуються лише для технічного зіставлення.
SOCIAL_PROGRAMS = sorted(
    {
        clean_text(data.get("program", stored_name))
        for stored_name, data in SOCIAL_PROGRAM_CONDITIONS.items()
        if clean_text(data.get("program", stored_name))
    },
    key=program_sort_key,
)

SOCIAL_PROGRAM_BY_ID: dict[int, str] = {
    index: program
    for index, program in enumerate(SOCIAL_PROGRAMS, start=1)
}


# Назви з вкладки ЗР зіставляємо вручну.
# Відображувані назви не змінюємо: вони залишаються саме такими,
# як у вкладці «Умови соц.програм».
SOCIAL_PROGRAM_BY_ID = {
    index: program
    for index, program in enumerate(SOCIAL_PROGRAMS, start=1)
}

mapped_zr_records: list[dict[str, Any]] = []

for record in social_zr_records:
    source_program = record["program"]
    target_key = program_alias_key(source_program)

    display_program = next(
        (
            program
            for program in SOCIAL_PROGRAMS
            if program_alias_key(program) == target_key
        ),
        None,
    )

    # Fallback на стару ручну карту, але відображувану назву все одно
    # намагаємося взяти з вкладки умов.
    if not display_program:
        alias_key = zr_alias_key(source_program)
        mapped_name = ZR_PROGRAM_ALIASES.get(alias_key)

        if mapped_name:
            mapped_key = program_alias_key(mapped_name)
            display_program = next(
                (
                    program
                    for program in SOCIAL_PROGRAMS
                    if program_alias_key(program) == mapped_key
                ),
                None,
            )

    if not display_program:
        logger.warning(
            "Немає відповідника у вкладці умов для програми ЗР: %s",
            source_program,
        )
        continue

    mapped_record = dict(record)
    mapped_record["source_program"] = source_program
    mapped_record["program"] = display_program
    mapped_record["is_zr"] = True
    mapped_zr_records.append(mapped_record)

for record in social_main_records:
    source_program = record["program"]
    source_key = program_alias_key(source_program)

    display_program = next(
        (
            program
            for program in SOCIAL_PROGRAMS
            if program_alias_key(program) == source_key
        ),
        None,
    )

    record["source_program"] = source_program
    if display_program:
        record["program"] = display_program
    record["is_zr"] = False

SOCIAL_PHARMACY_RECORDS = social_main_records + mapped_zr_records

logger.info(
    "Завантажено %s соціальних програм і %s записів аптек.",
    len(SOCIAL_PROGRAMS),
    len(SOCIAL_PHARMACY_RECORDS),
)



NIR_PROGRAM_KEY = program_alias_key(
    '«РАЗОМ ДО ЗДОРОВОГО МАЙБУТНЬОГО» від виробника НІР'
)

NIR_SPECIAL_PHARMACY = {
    "sheet_name": "SPECIAL",
    "row_number": 0,
    "program": '«РАЗОМ ДО ЗДОРОВОГО МАЙБУТНЬОГО» від виробника НІР',
    "source_program": '«РАЗОМ ДО ЗДОРОВОГО МАЙБУТНЬОГО» від виробника НІР',
    "participation_value": "НОВА ПРОГРАМА, підключена тільки А134",
    "short_number": "134",
    "department": "134 БАМ",
    "oblast": "Волинська",
    "city": "Луцьк",
    "street": "пр. Відродження 26А",
    "is_zr": True,
}


def social_pharmacies_for_program(program: str) -> list[dict[str, Any]]:
    """Повертає аптеки програми, прибираючи дублікати."""
    selected_main = resolve_to_main_program(
        program,
        SOCIAL_PROGRAMS,
    ) or program

    selected_key = program_alias_key(selected_main)

    # НІР: спеціально підключена лише аптека А134.
    if selected_key == NIR_PROGRAM_KEY:
        return [dict(NIR_SPECIAL_PHARMACY)]

    matching = [
        record
        for record in SOCIAL_PHARMACY_RECORDS
        if program_alias_key(record["program"]) == selected_key
    ]

    deduplicated: dict[tuple[str, str, str], dict[str, Any]] = {}

    for record in matching:
        key = (
            normalize(record["short_number"]),
            normalize(record["city"]),
            normalize(record["street"]),
        )

        existing = deduplicated.get(key)

        if existing and record["is_zr"]:
            merged = dict(existing)
            merged["is_zr"] = True
            deduplicated[key] = merged
        elif not existing:
            deduplicated[key] = record

    results = list(deduplicated.values())

    results.sort(
        key=lambda item: (
            normalize(item["oblast"]),
            normalize(item["city"]),
            normalize(item["street"]),
            normalize(item["short_number"]),
        )
    )

    return results


def filter_social_pharmacies(
    program: str,
    filter_mode: str,
    search_text: str,
) -> list[dict[str, Any]]:
    records = social_pharmacies_for_program(program)
    query = normalize(search_text)

    if filter_mode == "all":
        return records

    if not query:
        return []

    field_by_mode = {
        "oblast": "oblast",
        "city": "city",
        "street": "street",
        "number": "short_number",
    }

    field = field_by_mode.get(filter_mode)

    if not field:
        return []

    if filter_mode == "number":
        return [
            record
            for record in records
            if normalize(record[field]) == query
        ]

    return [
        record
        for record in records
        if query in normalize(record[field])
    ]


def social_programs_page_data(
    page: int = 0,
    programs: list[str] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    У тексті залишаємо лише заголовок.
    Перелік соціальних програм показується тільки кнопками.
    """
    source_programs = programs if programs is not None else SOCIAL_PROGRAMS

    total_pages = max(
        1,
        (len(source_programs) + SOCIAL_PROGRAMS_PER_PAGE - 1)
        // SOCIAL_PROGRAMS_PER_PAGE,
    )

    page = max(0, min(page, total_pages - 1))
    start_index = page * SOCIAL_PROGRAMS_PER_PAGE
    end_index = min(
        start_index + SOCIAL_PROGRAMS_PER_PAGE,
        len(source_programs),
    )

    page_programs = source_programs[start_index:end_index]
    lines = ["🤝 Оберіть соціальну програму:"]

    keyboard: list[list[InlineKeyboardButton]] = []

    for program in page_programs:
        icon = program_status_icon(program)

        program_id = next(
            (
                identifier
                for identifier, value in SOCIAL_PROGRAM_BY_ID.items()
                if program_alias_key(value) == program_alias_key(program)
            ),
            None,
        )

        if program_id is not None:
            keyboard.append([
                InlineKeyboardButton(
                    shorten_button(f"{icon} {program}", max_length=60),
                    callback_data=f"social_program:{program_id}",
                )
            ])

    navigation: list[InlineKeyboardButton] = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"social_programs_page:{page - 1}",
            )
        )

    if end_index < len(source_programs):
        navigation.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"social_programs_page:{page + 1}",
            )
        )

    if navigation:
        keyboard.append(navigation)

    keyboard.extend([
        [
            InlineKeyboardButton(
                "🔎 Пошук програми за назвою",
                callback_data="social_program_search",
            )
        ],
        [
            InlineKeyboardButton(
                "💊 Пошук за препаратом",
                callback_data="social_medicine_search",
            )
        ],
        [
            InlineKeyboardButton(
                "📌 Загальні умови",
                callback_data="social_general_conditions",
            )
        ],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])

    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def search_social_programs_by_name(search_text: str) -> list[str]:
    query = normalize_program_name(search_text)

    if not query:
        return []

    matches: dict[str, str] = {}

    for program in SOCIAL_PROGRAMS:
        normalized_program = normalize_program_name(program)

        if query in normalized_program:
            matches[canonical_program_key(program)] = program

    return sorted(
        matches.values(),
        key=program_sort_key,
    )


TEVA_MOMENTS_KEY = "моменти життя від тева"


def _main_program_by_canonical_key(canonical_key: str) -> str | None:
    """Назва програми, яку показуємо користувачу/використовуємо для аптек."""
    matches = [
        program for program in SOCIAL_PROGRAMS
        if canonical_program_key(program) == canonical_key
    ]
    if not matches:
        return None

    # Для основного переліку аптек пріоритет має назва з «Аджові».
    for program in matches:
        if "аджові" in normalize_program_name(program):
            return program
    return matches[0]


def _medicine_program_override(search_text: str) -> str | None:
    query = normalize_medicine_name(search_text)
    if "аджові" in query:
        return _main_program_by_canonical_key(TEVA_MOMENTS_KEY)
    return None

def search_programs_by_medicine(
    search_text: str,
) -> list[dict[str, Any]]:
    """
    Повертає програми та препарати за назвою препарату.

    Препарат береться тільки з того блоку умов, у якому він реально
    знаходиться. Назва програми зіставляється з основним переліком аптек
    через canonical_program_key / resolve_to_main_program.
    """
    query = normalize_medicine_name(search_text)

    if not query:
        return []

    grouped: dict[str, dict[str, Any]] = {}

    for stored_name, condition_data in SOCIAL_PROGRAM_CONDITIONS.items():
        stored_program = condition_data.get("program", stored_name)
        main_program = resolve_to_main_program(
            stored_program,
            SOCIAL_PROGRAMS,
        )

        if not main_program:
            continue

        matching_items: list[dict[str, str]] = []

        for item in condition_data.get("items", []):
            product = clean_text(item.get("product", ""))

            if query not in normalize_medicine_name(product):
                continue

            matching_items.append({
                "product": product,
                "limit": clean_text(item.get("limit", "")),
                "discount": clean_text(item.get("discount", "")),
                "category": clean_text(item.get("category", "")),
            })

        if not matching_items:
            continue

        program_entry = grouped.setdefault(
            main_program,
            {
                "program": main_program,
                "items": [],
            },
        )

        existing_items = {
            (
                normalize_medicine_name(item["product"]),
                normalize(item["limit"]),
                normalize(item["discount"]),
            )
            for item in program_entry["items"]
        }

        for item in matching_items:
            item_key = (
                normalize_medicine_name(item["product"]),
                normalize(item["limit"]),
                normalize(item["discount"]),
            )

            if item_key not in existing_items:
                program_entry["items"].append(item)
                existing_items.add(item_key)

    return sorted(
        grouped.values(),
        key=lambda item: program_sort_key(item["program"]),
    )


def medicine_search_results_markup(
    results: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []

    for result in results:
        program = result["program"]
        program_id = next(
            (
                identifier
                for identifier, value in SOCIAL_PROGRAM_BY_ID.items()
                if program_alias_key(value) == program_alias_key(program)
            ),
            None,
        )

        if program_id is None:
            continue

        keyboard.append([
            InlineKeyboardButton(
                shorten_button(
                    f"🏥 Знайти аптеки: {program}",
                    max_length=60,
                ),
                callback_data=f"social_medicine_pharmacies:{program_id}",
            )
        ])

    keyboard.extend([
        [
            InlineKeyboardButton(
                "💊 Новий пошук за препаратом",
                callback_data="social_medicine_search",
            )
        ],
        [InlineKeyboardButton("⬅️ До переліку програм", callback_data="social_programs")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])

    return InlineKeyboardMarkup(keyboard)


def selected_social_program(context: ContextTypes.DEFAULT_TYPE) -> str:
    return clean_text(context.user_data.get("social_program", ""))


KRKA_CLOSED_PROGRAM_KEY = program_alias_key('КРКА "ЗАХИСТІТЬ СУДИНИ - ЗБЕРЕЖІТЬ СЕРЦЕ" та "ПОКРАЩЕНИЙ КРОВООБІГ ДЛЯ ЗДОРОВ’Я МОЗКУ"')


def is_closed_conditions_only_program(program: str) -> bool:
    return program_alias_key(program) == KRKA_CLOSED_PROGRAM_KEY


def social_program_actions_markup(
    program: str | None = None,
) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("📝 Умови програми", callback_data="social_conditions")],
    ]

    if program and is_closed_conditions_only_program(program):
        keyboard.append([
            InlineKeyboardButton(
                "🔴 Програма закрита",
                callback_data="social_conditions",
            )
        ])
    else:
        keyboard.append([
            InlineKeyboardButton("🏥 Знайти аптеку", callback_data="social_pharmacy_menu")
        ])

    keyboard.extend([
        [InlineKeyboardButton("⬅️ До переліку програм", callback_data="social_programs")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])

    return InlineKeyboardMarkup(keyboard)


def social_pharmacy_search_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏙 Пошук за містом", callback_data="social_filter:city")],
        [InlineKeyboardButton("🗺 Пошук за областю", callback_data="social_filter:oblast")],
        [InlineKeyboardButton("🛣 Пошук за вулицею", callback_data="social_filter:street")],
        [InlineKeyboardButton("🔢 Пошук за коротким № аптеки", callback_data="social_filter:number")],
        [InlineKeyboardButton("📋 Показати всі аптеки", callback_data="social_filter:all")],
        [InlineKeyboardButton("⬅️ Назад до програми", callback_data="social_program_back")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])


def social_results_markup(
    context: ContextTypes.DEFAULT_TYPE,
    page: int,
    total_pages: int,
) -> InlineKeyboardMarkup:
    keyboard: list[list[InlineKeyboardButton]] = []
    navigation: list[InlineKeyboardButton] = []

    if page > 0:
        navigation.append(
            InlineKeyboardButton(
                "⬅️",
                callback_data=f"social_results_page:{page - 1}",
            )
        )

    if page + 1 < total_pages:
        navigation.append(
            InlineKeyboardButton(
                "➡️",
                callback_data=f"social_results_page:{page + 1}",
            )
        )

    if navigation:
        keyboard.append(navigation)

    repeat_labels = {
        "city": "🏙 Інше місто",
        "oblast": "🗺 Інша область",
        "street": "🛣 Інша вулиця",
        "number": "🔢 Інший номер аптеки",
        "all": "🔎 Інший спосіб пошуку",
    }
    repeat_label = repeat_labels.get(
        context.user_data.get("social_filter_mode"),
        "🔎 Інший пошук",
    )

    if context.user_data.get("social_filter_mode") != "all":
        keyboard.append([
            InlineKeyboardButton(
                repeat_label,
                callback_data="social_repeat_search",
            )
        ])

    keyboard.extend([
        [InlineKeyboardButton("🔎 Інший спосіб пошуку", callback_data="social_pharmacy_menu")],
        [InlineKeyboardButton("⬅️ До переліку програм", callback_data="social_programs")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])

    return InlineKeyboardMarkup(keyboard)


def format_social_pharmacy(record: dict[str, Any]) -> str:
    zr_mark = " 💚 <b>ЗР</b>" if record["is_zr"] else ""

    title = (
        f"🏥 <b>Аптека №{html.escape(record['short_number'])}</b>{zr_mark}"
        if record["short_number"]
        else f"🏥 <b>Аптека</b>{zr_mark}"
    )

    location_parts = [
        clean_text(record["oblast"]),
        clean_text(record["city"]),
    ]
    location = ", ".join(part for part in location_parts if part)

    lines = [title]

    if location:
        lines.append(f"📍 {html.escape(location)}")

    if record["street"]:
        lines.append(f"🛣 {html.escape(record['street'])}")

    return "\n".join(lines)


async def show_social_results(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    page: int = 0,
    edit: bool = False,
) -> None:
    results: list[dict[str, Any]] = context.user_data.get(
        "social_results",
        [],
    )
    program = selected_social_program(context)
    filter_label = clean_text(
        context.user_data.get("social_filter_label", "")
    )

    total = len(results)
    total_pages = max(
        1,
        (total + SOCIAL_RESULTS_PER_PAGE - 1)
        // SOCIAL_RESULTS_PER_PAGE,
    )

    page = max(0, min(page, total_pages - 1))
    context.user_data["social_results_page"] = page

    start_index = page * SOCIAL_RESULTS_PER_PAGE
    end_index = min(start_index + SOCIAL_RESULTS_PER_PAGE, total)

    lines = [
        f"🤝 <b>{html.escape(program)}</b>",
    ]

    if filter_label:
        lines.append(f"🔎 {html.escape(filter_label)}")

    lines.append(f"Знайдено аптек: {total}")

    for record in results[start_index:end_index]:
        lines.append("")
        lines.append(format_social_pharmacy(record))

    if total_pages > 1:
        lines.append("")
        lines.append(f"Сторінка {page + 1} з {total_pages}")

    text = "\n".join(lines)
    markup = social_results_markup(context, page, total_pages)

    if edit:
        await message.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
    else:
        await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )


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
            " ".join([
                record["active_substance"],
                record["trade_name"],
                record["form"],
                record["dosage"],
                record["package"],
            ])
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


def search_all_available_drugs_flexible(
    search_text: str,
) -> list[dict[str, Any]]:
    """
    Пошук у всіх підрозділах «Доступних ліків»:
    звичайні препарати, інсуліни та тест-смужки.

    Використовується для підказки з розділу соціальних програм.
    Враховує різницю у написанні «і/и».
    """
    query_tokens = [
        token
        for token in normalize_medicine_name(search_text).split()
        if token
    ]

    if not query_tokens:
        return []

    results: list[dict[str, Any]] = []

    for record in drug_records:
        searchable_text = normalize_medicine_name(
            " ".join([
                record["active_substance"],
                record["trade_name"],
                record["form"],
                record["dosage"],
                record["package"],
            ])
        )

        if all(token in searchable_text for token in query_tokens):
            results.append(record)

    normalized_query = normalize_medicine_name(search_text)

    results.sort(
        key=lambda record: (
            0
            if normalize_medicine_name(record["trade_name"]) == normalized_query
            else 1,
            0
            if normalize_medicine_name(record["trade_name"]).startswith(
                normalized_query
            )
            else 1,
            normalize_medicine_name(record["trade_name"]),
            normalize_medicine_name(record["dosage"]),
            normalize_medicine_name(record["package"]),
        )
    )

    return results


def cross_social_programs_markup(
    results: list[dict[str, Any]],
) -> InlineKeyboardMarkup:
    """Кнопки переходу з Доступних ліків до знайдених соцпрограм."""
    keyboard: list[list[InlineKeyboardButton]] = []

    for result in results:
        program = result["program"]
        program_id = next(
            (
                identifier
                for identifier, value in SOCIAL_PROGRAM_BY_ID.items()
                if program_alias_key(value) == program_alias_key(program)
            ),
            None,
        )

        if program_id is None:
            continue

        keyboard.append([
            InlineKeyboardButton(
                shorten_button(
                    f"🤝 Перейти до програми: {program}",
                    max_length=60,
                ),
                callback_data=f"social_program:{program_id}",
            )
        ])

    keyboard.extend([
        [InlineKeyboardButton("🔎 Новий пошук", callback_data="drug_search")],
        [InlineKeyboardButton("⬅️ До Доступних ліків", callback_data="drugs")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])

    return InlineKeyboardMarkup(keyboard)


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


def medical_device_short_title(record: dict[str, Any]) -> str:
    """Повертає коротку назву тест-смужок для кнопки та картки."""
    # У вкладці "РЕЄСТР МЕД ВИРОБИ" опис із моделлю
    # зберігається у фізичному стовпці C, тобто в record["trade_name"].
    text = clean_text(record.get("trade_name", ""))

    patterns = [
        r"Rightest\s+[A-Za-z0-9+\- ]+\([^)]+\)",
        r"ELSA\s*[A-Za-z0-9+\- ]*\([^)]+\)",
        r"Contour\s+[A-Za-z0-9+\- ]+\([^)]+\)",
        r"Accu[- ]?Chek\s+[A-Za-z0-9+\- ]+\([^)]+\)",
        r"OneTouch\s+[A-Za-z0-9+\- ]+\([^)]+\)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return clean_text(match.group(0))

    # Універсальний запасний варіант:
    # забираємо типовий вступ і залишаємо модель/кількість.
    shortened = re.sub(
        r"^Тест[- ]?смужки\s+для\s+контролю\s+рівня\s+глюкози\s+в\s+крові\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )

    return (
        clean_text(shortened)
        or clean_text(record.get("trade_name", ""))
        or "Назва не вказана"
    )


def format_drug_card(record: dict[str, Any]) -> str:
    sheet_name = record["sheet_name"]
    headers = record["headers"]
    lines: list[str] = []

    if sheet_name == MEDICAL_DEVICES_SHEET:
        title = medical_device_short_title(record)
        title_icon = "🩸"
    else:
        title = record["trade_name"] or record["active_substance"] or "Назва не вказана"
        title_icon = "💉" if sheet_name == INSULIN_SHEET else "💊"

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

    if sheet_name == INSULIN_SHEET:
        last_column_number = record.get("last_column_number")
        last_column_header = clean_text(record.get("last_column_header", ""))
        last_column_value = clean_text(record.get("last_column_value", ""))

        # Не дублюємо колонку P, якщо раптом вона виявиться останньою.
        if (
            last_column_number
            and last_column_number != COLUMN_P
            and last_column_value
        ):
            append_field(
                lines,
                "📌",
                last_column_header or "Додаткова інформація",
                last_column_value,
            )

    return "\n\n".join(lines)


def main_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Швидкий пошук", callback_data="knowledge_search")],
        [InlineKeyboardButton("📚 База знань", callback_data="knowledge")],
        [InlineKeyboardButton("💊 Доступні ліки", callback_data="drugs")],
        [InlineKeyboardButton("🤝 Соціальні програми", callback_data="social_programs")],
    ])


def drugs_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Пошук", callback_data="drug_search")],
        [InlineKeyboardButton("💉 Інсуліни", callback_data="insulin")],
        [InlineKeyboardButton("🩸 Тест-смужки", callback_data="strips")],
        [InlineKeyboardButton("📌 Загальні умови", callback_data="drug_general_conditions")],
        [InlineKeyboardButton("🤖 Офіційний бот МОЗ", url="https://t.me/SpytaiGrytsia_bot")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="main_menu")],
    ])


def search_navigation_markup(mode: str) -> InlineKeyboardMarkup:
    callback = {
        "all": "drug_search",
        "insulin": "insulin_search",
        "strips": "strips_search",
    }.get(mode, "drug_search")

    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Новий пошук", callback_data=callback)],
        [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
        [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
    ])


def result_title(record: dict[str, Any]) -> str:
    if record["sheet_name"] == MEDICAL_DEVICES_SHEET:
        return shorten_button("🩸 " + medical_device_short_title(record))

    title = (
        record["trade_name"]
        or record["active_substance"]
        or "Назва не вказана"
    )

    details = [
        value
        for value in [record["dosage"], record["package"]]
        if value
    ]

    if details:
        title = f"{title} — {'; '.join(details)}"

    prefix = "💉 " if record["sheet_name"] == INSULIN_SHEET else "💊 "
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
        "insulin": "insulin_search",
        "strips": "strips_search",
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

    if data_cb == "knowledge_search":
        context.user_data.clear()
        context.user_data["knowledge_search_mode"] = True

        await query.edit_message_text(
            "🔍 Введіть ключове слово або фразу для пошуку.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📚 База знань", callback_data="knowledge")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    if data_cb.startswith("knowledge_search_page:"):
        try:
            page = int(data_cb.split(":", 1)[1])
        except ValueError:
            return

        await show_knowledge_search_results(
            query,
            context,
            page=page,
            edit=True,
        )
        return

    if data_cb == "social_programs":
        context.user_data.clear()
        text, markup = social_programs_page_data(page=0)

        await query.edit_message_text(
            text,
            reply_markup=markup,
        )
        return

    if data_cb.startswith("social_programs_page:"):
        try:
            page = int(data_cb.split(":", 1)[1])
        except ValueError:
            return

        text, markup = social_programs_page_data(page=page)

        await query.edit_message_text(
            text,
            reply_markup=markup,
        )
        return

    if data_cb == "social_medicine_search":
        context.user_data.clear()
        context.user_data["social_medicine_search_mode"] = True

        await query.edit_message_text(
            "💊 Введіть назву або частину назви препарату:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="social_programs")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    if data_cb == "social_general_conditions":
        answer = find_knowledge_answer(
            category_contains="Соціальні програми Карткові соц програми",
            question_contains="Умови",
        )

        if not answer:
            answer = (
                "📌 Перевіряємо наявність товару в обраній клієнтом аптеці\n"
                "📌 Перевіряємо, чи аптека підключена до програми\n"
                "📌 Відпуск тільки повними упаковками\n"
                "📌 Не діє на інтернет-бронювання\n"
                "📌 Знижка рахується від аптечної вартості, яку клієнт "
                "може дізнатися тільки в аптеці\n"
                "📌 Рецепт/картку виписує тільки лікар\n"
                "📌 В аптеці відпуск — 1 препарат у чек\n"
                "📌 Якщо в аптеці немає світла або інтернету, "
                "препарат відпустити не зможуть"
            )

        await query.edit_message_text(
            "🤝 Загальні умови соціальних програм\n\n" + answer,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="social_programs")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    if data_cb.startswith("social_medicine_pharmacies:"):
        try:
            program_id = int(data_cb.split(":", 1)[1])
        except ValueError:
            return

        program = SOCIAL_PROGRAM_BY_ID.get(program_id)

        if not program:
            return

        context.user_data["social_program"] = program
        context.user_data.pop("social_medicine_search_mode", None)
        context.user_data.pop("social_filter_mode", None)

        await query.edit_message_text(
            f"{program_status_icon(program)} {program}\n\n"
            "Оберіть спосіб пошуку аптек:",
            reply_markup=social_pharmacy_search_markup(),
        )
        return

    if data_cb == "social_program_search":
        context.user_data.clear()
        context.user_data["social_program_search_mode"] = True

        await query.edit_message_text(
            "🔎 Введіть назву або частину назви соціальної програми:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="social_programs")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    if data_cb.startswith("social_program:"):
        try:
            program_id = int(data_cb.split(":", 1)[1])
        except ValueError:
            return

        program = SOCIAL_PROGRAM_BY_ID.get(program_id)

        if not program:
            return

        context.user_data.clear()
        context.user_data["social_program"] = program

        await query.edit_message_text(
            f"{program_status_icon(program)} {program}\n\n"
            "Оберіть дію:",
            reply_markup=social_program_actions_markup(program),
        )
        return

    if data_cb == "social_conditions":
        program = selected_social_program(context)

        if not program:
            return

        await query.edit_message_text(
            format_social_program_conditions(program),
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад до програми", callback_data="social_program_back")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    if data_cb == "social_pharmacy_menu":
        program = selected_social_program(context)

        if not program:
            return

        await query.edit_message_text(
            f"{program_status_icon(program)} {program}\n\n"
            "Оберіть спосіб пошуку аптек:",
            reply_markup=social_pharmacy_search_markup(),
        )
        return

    if data_cb == "social_repeat_search":
        program = selected_social_program(context)

        if not program:
            return

        filter_mode = context.user_data.get("social_filter_mode")

        prompts = {
            "city": "🏙 Введіть назву іншого міста:",
            "oblast": "🗺 Введіть назву іншої області:",
            "street": "🛣 Введіть назву іншої вулиці:",
            "number": "🔢 Введіть інший короткий номер аптеки:",
        }

        prompt = prompts.get(filter_mode)

        context.user_data.pop("social_results", None)
        context.user_data.pop("social_filter_label", None)
        context.user_data.pop("social_results_page", None)

        if prompt:
            await query.edit_message_text(
                prompt,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⬅️ Інший спосіб пошуку", callback_data="social_pharmacy_menu")],
                    [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
                ]),
            )
            return

        await query.edit_message_text(
            f"{program_status_icon(program)} {program}\n\n"
            "Оберіть спосіб пошуку аптек:",
            reply_markup=social_pharmacy_search_markup(),
        )
        return

    if data_cb == "social_program_back":
        program = selected_social_program(context)

        if not program:
            text, markup = social_programs_page_data(page=0)
            await query.edit_message_text(
                text,
                reply_markup=markup,
            )
            return

        context.user_data.pop("social_filter_mode", None)
        context.user_data.pop("social_results", None)
        context.user_data.pop("social_filter_label", None)

        await query.edit_message_text(
            f"{program_status_icon(program)} {program}\n\n"
            "Оберіть дію:",
            reply_markup=social_program_actions_markup(program),
        )
        return

    if data_cb.startswith("social_filter:"):
        filter_mode = data_cb.split(":", 1)[1]
        program = selected_social_program(context)

        if not program:
            return

        if filter_mode == "all":
            context.user_data["social_filter_mode"] = "all"

            results = filter_social_pharmacies(
                program,
                "all",
                "",
            )

            context.user_data["social_results"] = results
            context.user_data["social_filter_label"] = "Усі аптеки"
            context.user_data["social_results_page"] = 0

            await show_social_results(
                query,
                context,
                page=0,
                edit=True,
            )
            return

        prompts = {
            "city": "🏙 Введіть назву міста:",
            "oblast": "🗺 Введіть назву області:",
            "street": "🛣 Введіть назву вулиці:",
            "number": "🔢 Введіть короткий номер аптеки:",
        }

        prompt = prompts.get(filter_mode)

        if not prompt:
            return

        context.user_data["social_filter_mode"] = filter_mode
        context.user_data.pop("social_results", None)
        context.user_data.pop("social_filter_label", None)

        await query.edit_message_text(
            prompt,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="social_program_back")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    if data_cb.startswith("social_results_page:"):
        try:
            page = int(data_cb.split(":", 1)[1])
        except ValueError:
            return

        await show_social_results(
            query,
            context,
            page=page,
            edit=True,
        )
        return

    if data_cb == "knowledge":
        context.user_data.pop("search_mode", None)
        context.user_data.pop("knowledge_search_mode", None)

        await query.edit_message_text(
            "📚 Оберіть розділ:",
            reply_markup=knowledge_menu_markup(),
        )
        return

    if data_cb == "drugs":
        context.user_data.pop("search_mode", None)
        context.user_data.pop("knowledge_search_mode", None)
        await query.edit_message_text(
            "Оберіть дію:",
            reply_markup=drugs_menu_markup(),
        )
        return


    # -------------------------
    # Загальний пошук ліків
    # -------------------------

    if data_cb == "drug_general_conditions":
        answer = find_knowledge_answer(
            category_contains="Доступні ліки Реімбурсація",
            question_contains="Умови",
        )

        if not answer:
            answer = (
                "📌 Уточнюємо, яка кількість вказана в рецепті\n"
                "📌 Якщо в рецепті 3 упаковки, можна відпустити тільки "
                "3 упаковки; якщо товару недостатньо, рецепт погасити неможливо\n"
                "📌 Відпуск тільки повними упаковками\n"
                "📌 Рецепт виписує тільки лікар\n"
                "📌 У SMS надходить номер електронного рецепта "
                "(16 символів) і код підтвердження\n"
                "📌 Оператор не має доступу до перевірки рецепта — "
                "це можливо лише в аптеці\n"
                "📌 Рецепт можна перевірити через лікаря, Дію або Helsi; "
                "консультацію щодо сторонніх застосунків не надаємо\n"
                "📌 Не діє на інтернет-бронювання\n"
                "📌 Усі аптеки підключені до програми"
            )

        await query.edit_message_text(
            "💊 Загальні умови програми «Доступні ліки»\n\n" + answer,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🤖 Офіційний бот МОЗ",
                    url="https://t.me/SpytaiGrytsia_bot",
                )],
                [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    if data_cb == "drug_search":
        context.user_data["search_mode"] = "all"
        context.user_data.pop("search_result_ids", None)
        context.user_data.pop("search_text", None)

        await query.edit_message_text(
            "🔎 Введіть назву препарату або діючу речовину:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    # -------------------------
    # Повний перелік інсулінів
    # -------------------------

    if data_cb == "insulin":
        records = [
            record
            for record in drug_records
            if record["sheet_name"] == INSULIN_SHEET
        ]

        context.user_data["search_mode"] = "insulin"
        context.user_data["search_text"] = "Усі інсуліни"
        context.user_data["search_result_ids"] = [
            record["id"] for record in records
        ]
        context.user_data["results_page"] = 0

        if not records:
            await query.edit_message_text(
                "💉 Перелік інсулінів порожній.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 Пошук інсуліну", callback_data="insulin_search")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
                    [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
                ]),
            )
            return

        await show_search_results(query, context, page=0, edit=True)
        return

    # -------------------------
    # Пошук серед інсулінів
    # -------------------------

    if data_cb == "insulin_search":
        context.user_data["search_mode"] = "insulin"
        context.user_data.pop("search_result_ids", None)
        context.user_data.pop("search_text", None)

        await query.edit_message_text(
            "💉 Введіть назву інсуліну або діючу речовину:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Переглянути весь перелік", callback_data="insulin")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    # -------------------------
    # Повний перелік тест-смужок
    # -------------------------

    if data_cb == "strips":
        records = [
            record
            for record in drug_records
            if record["sheet_name"] == MEDICAL_DEVICES_SHEET
        ]

        context.user_data["search_mode"] = "strips"
        context.user_data["search_text"] = "Усі тест-смужки"
        context.user_data["search_result_ids"] = [
            record["id"] for record in records
        ]
        context.user_data["results_page"] = 0

        if not records:
            await query.edit_message_text(
                "🩸 Перелік тест-смужок порожній.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 Пошук тест-смужок", callback_data="strips_search")],
                    [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
                    [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
                ]),
            )
            return

        await show_search_results(query, context, page=0, edit=True)
        return

    # -------------------------
    # Пошук серед тест-смужок
    # -------------------------

    if data_cb == "strips_search":
        context.user_data["search_mode"] = "strips"
        context.user_data.pop("search_result_ids", None)
        context.user_data.pop("search_text", None)

        await query.edit_message_text(
            "🩸 Введіть назву або модель тест-смужок:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📋 Переглянути весь перелік", callback_data="strips")],
                [InlineKeyboardButton("⬅️ Назад", callback_data="drugs")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ]),
        )
        return

    if data_cb == "cross_to_available_drugs":
        result_ids = context.user_data.get(
            "cross_available_drug_ids",
            [],
        )
        search_text = context.user_data.get(
            "cross_available_search_text",
            "",
        )

        if not result_ids:
            await query.edit_message_text(
                "Записи не знайдено. Виконайте пошук ще раз.",
                reply_markup=main_menu_markup(),
            )
            return

        context.user_data["search_mode"] = "all"
        context.user_data["search_text"] = search_text
        context.user_data["search_result_ids"] = result_ids
        context.user_data["results_page"] = 0

        # Якщо результат лише один — одразу відкриваємо картку.
        if len(result_ids) == 1:
            record_id = result_ids[0]
            record = drug_records_by_id.get(record_id)

            if not record:
                return

            keyboard: list[list[InlineKeyboardButton]] = []

            if record["active_substance"] and record["dosage"]:
                keyboard.append([
                    InlineKeyboardButton(
                        "🔄 Підібрати аналоги",
                        callback_data=f"analogs:{record_id}",
                    )
                ])

            keyboard.extend([
                [InlineKeyboardButton("⬅️ До Доступних ліків", callback_data="drugs")],
                [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
            ])

            await query.edit_message_text(
                format_drug_card(record),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        await show_search_results(
            query,
            context,
            page=0,
            edit=True,
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

    knowledge_path = knowledge_callback_to_path.get(data_cb)

    if knowledge_path:
        node = knowledge_node(knowledge_path)

        if not node:
            return

        if "__answer__" in node:
            answer = preserve_multiline_text(node.get("__answer__", ""))

            parent_path = knowledge_path[:-1]
            back_callback = (
                knowledge_path_to_callback.get(parent_path)
                if parent_path
                else "knowledge"
            )

            await query.edit_message_text(
                answer or "Відповідь не вказана.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "⬅️ Назад",
                            callback_data=back_callback or "knowledge",
                        )
                    ],
                    [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
                ]),
            )
            return

        await query.edit_message_text(
            f"📚 {knowledge_path[-1]}\n\nОберіть пункт:",
            reply_markup=knowledge_menu_markup(knowledge_path),
        )
        return



async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    search_text = clean_text(update.message.text)

    if context.user_data.get("knowledge_search_mode"):
        if not search_text:
            await update.message.reply_text(
                "🔍 Введіть ключове слово або фразу для пошуку."
            )
            return

        results = search_knowledge(search_text)

        if not results:
            await update.message.reply_text(
                "❌ У базі знань нічого не знайдено.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 Спробувати ще раз", callback_data="knowledge_search")],
                    [InlineKeyboardButton("📚 База знань", callback_data="knowledge")],
                    [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
                ]),
            )
            return

        context.user_data.pop("knowledge_search_mode", None)
        context.user_data["knowledge_search_text"] = search_text
        context.user_data["knowledge_search_results"] = results
        context.user_data["knowledge_search_page"] = 0

        await show_knowledge_search_results(
            update.message,
            context,
            page=0,
            edit=False,
        )
        return

    if context.user_data.get("social_medicine_search_mode"):
        results = search_programs_by_medicine(search_text)

        if not results:
            available_results = search_all_available_drugs_flexible(
                search_text
            )

            if available_results:
                context.user_data[
                    "cross_available_drug_ids"
                ] = [
                    record["id"]
                    for record in available_results
                ]
                context.user_data[
                    "cross_available_search_text"
                ] = search_text
                context.user_data.pop(
                    "social_medicine_search_mode",
                    None,
                )

                await update.message.reply_text(
                    "💡 Препарат не бере участі "
                    "у соціальних програмах.\n\n"
                    "💙 Але він входить до програми "
                    "«Доступні ліки».",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "💙 Перейти до препарату",
                                callback_data="cross_to_available_drugs",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "💊 Новий пошук у соцпрограмах",
                                callback_data="social_medicine_search",
                            )
                        ],
                        [
                            InlineKeyboardButton(
                                "🏠 Головне меню",
                                callback_data="main_menu",
                            )
                        ],
                    ]),
                )
                return

            await update.message.reply_text(
                "❌ Препарат не знайдено ні в програмі "
                "«Доступні ліки», ні серед соціальних програм.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "💊 Спробувати ще раз",
                            callback_data="social_medicine_search",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "⬅️ До переліку програм",
                            callback_data="social_programs",
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            "🏠 Головне меню",
                            callback_data="main_menu",
                        )
                    ],
                ]),
            )
            return

        context.user_data["social_medicine_search_mode"] = True
        context.user_data["social_medicine_results"] = results

        lines = [
            f"💊 {search_text}",
            "",
        ]

        for result in results:
            program = result["program"]
            lines.append(
                f"🤝 {program_status_icon(program)} {program}"
            )
            lines.append("")

            for item in result["items"]:
                lines.append(f"• {item['product']}")

                if item["limit"]:
                    lines.append(
                        f"  📦 Ліміт: {item['limit']} "
                        f"{package_word(item['limit'])}"
                    )

                if item["discount"]:
                    lines.append(
                        f"  💸 Знижка: {item['discount']}"
                    )

                if item["category"]:
                    lines.append(
                        f"  🏷 Категорія: {item['category']}"
                    )

                lines.append("")

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=medicine_search_results_markup(results),
        )
        return

    if context.user_data.get("social_program_search_mode"):
        matches = search_social_programs_by_name(search_text)

        if not matches:
            await update.message.reply_text(
                "❌ Соціальну програму не знайдено.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔎 Спробувати ще раз", callback_data="social_program_search")],
                    [InlineKeyboardButton("⬅️ До переліку програм", callback_data="social_programs")],
                    [InlineKeyboardButton("🏠 Головне меню", callback_data="main_menu")],
                ]),
            )
            return

        context.user_data.pop("social_program_search_mode", None)
        context.user_data["social_program_search_results"] = matches

        text, markup = social_programs_page_data(
            page=0,
            programs=matches,
        )

        text = (
            f"🔎 Результати пошуку: {search_text}\n\n"
            + "\n".join(text.splitlines()[2:])
        )

        await update.message.reply_text(
            text,
            reply_markup=markup,
        )
        return

    social_filter_mode = context.user_data.get("social_filter_mode")
    social_program = selected_social_program(context)

    if social_filter_mode and social_program:
        results = filter_social_pharmacies(
            social_program,
            social_filter_mode,
            search_text,
        )

        labels = {
            "city": f"Місто: {search_text}",
            "oblast": f"Область: {search_text}",
            "street": f"Вулиця: {search_text}",
            "number": f"Короткий № аптеки: {search_text}",
        }

        context.user_data["social_results"] = results
        context.user_data["social_filter_label"] = labels.get(
            social_filter_mode,
            search_text,
        )
        context.user_data["social_results_page"] = 0

        await show_social_results(
            update.message,
            context,
            page=0,
            edit=False,
        )
        return

    mode = context.user_data.get("search_mode")

    if not mode:
        return

    if not search_text:
        prompt = {
            "all": "Введіть назву препарату або діючу речовину.",
            "insulin": "Введіть назву інсуліну або діючу речовину.",
            "strips": "Введіть назву або модель тест-смужок.",
        }.get(mode, "Введіть пошуковий запит.")

        await update.message.reply_text(prompt)
        return

    results = search_drugs(search_text, mode=mode)

    if not results:
        social_results = search_programs_by_medicine(search_text)

        if social_results:
            context.user_data.pop("search_mode", None)

            lines = [
                "💡 Препарат відсутній у програмі "
                "«Доступні ліки».",
                "",
                "🤝 Але він бере участь у соціальній програмі:",
                "",
            ]

            for result in social_results:
                program = result["program"]
                lines.append(
                    f"{program_status_icon(program)} {program}"
                )

            await update.message.reply_text(
                "\n".join(lines),
                reply_markup=cross_social_programs_markup(
                    social_results
                ),
            )
            return

        await update.message.reply_text(
            "❌ Препарат не знайдено ні в програмі "
            "«Доступні ліки», ні серед соціальних програм.",
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
