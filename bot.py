import calendar
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

MAX_POLL_OPTIONS = 10
POLL_QUESTION = "chasch no?"
CREATE_POLL_BUTTON = "Umfrog starte"
NOOP = "x"

PICK_START, PICK_END, PICK_TIME_OPTION, WAIT_TIME_TEXT = range(4)

WEEKDAY_HEADER = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
MONTH_NAMES = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def parse_day(token: str, today: date) -> date:
    token = token.strip().lower()
    if token == "today":
        return today
    if token.startswith("+"):
        return today + timedelta(days=int(token[1:]))
    return datetime.strptime(token, "%Y-%m-%d").date()


def format_day(d: date) -> str:
    return f"{WEEKDAY_HEADER[d.weekday()]} {d.day:02d}.{d.month:02d}."


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_start_from_key(key: str) -> date:
    year, month = key.split("-")
    return date(int(year), int(month), 1)


def shift_month(d: date, delta: int) -> date:
    total = d.year * 12 + (d.month - 1) + delta
    year = total // 12
    month = total % 12 + 1
    return date(year, month, 1)


def month_title(d: date) -> str:
    return f"{MONTH_NAMES[d.month - 1]} {d.year}"


def load_token_from_dotenv() -> str:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return ""

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        if key.strip() == "TELEGRAM_BOT_TOKEN":
            return value.strip().strip("\"'")

    return ""


def clear_poll_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("poll_start", None)
    context.user_data.pop("poll_end", None)
    context.user_data.pop("view_month_start", None)
    context.user_data.pop("view_month_end", None)
    context.user_data.pop("cleanup_message_ids", None)


def track_for_cleanup(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    if message is None:
        return
    ids = context.user_data.setdefault("cleanup_message_ids", [])
    if message.message_id not in ids:
        ids.append(message.message_id)


async def cleanup_non_poll_messages(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    keep_message_ids: Optional[set] = None,
) -> None:
    keep = keep_message_ids or set()
    ids = context.user_data.get("cleanup_message_ids", [])
    for message_id in ids:
        if message_id in keep:
            continue
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            # Ignore when bot lacks rights or message is already gone.
            pass

    context.user_data.pop("cleanup_message_ids", None)


def build_question(time_note: Optional[str], part: Optional[str] = None) -> str:
    base = POLL_QUESTION
    if time_note:
        base = f"{POLL_QUESTION} | weli zit? {time_note.strip()}"

    suffix = f" {part}" if part else ""
    max_base_len = max(1, 300 - len(suffix))
    return f"{base[:max_base_len].rstrip()}{suffix}"


def make_calendar_rows(
    month_day: date,
    selectable_days: set[date],
    select_prefix: str,
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(day, callback_data=NOOP) for day in WEEKDAY_HEADER])

    weeks = calendar.monthcalendar(month_day.year, month_day.month)
    for week in weeks:
        week_buttons: list[InlineKeyboardButton] = []
        for day_num in week:
            if day_num == 0:
                week_buttons.append(InlineKeyboardButton(" ", callback_data=NOOP))
                continue

            current = date(month_day.year, month_day.month, day_num)
            if current in selectable_days:
                week_buttons.append(
                    InlineKeyboardButton(str(day_num), callback_data=f"{select_prefix}:{current.isoformat()}")
                )
            else:
                week_buttons.append(InlineKeyboardButton("-", callback_data=NOOP))
        rows.append(week_buttons)

    return rows


def start_keyboard(view_month: date, today: date) -> InlineKeyboardMarkup:
    first_of_current_month = date(today.year, today.month, 1)
    selectable: set[date] = set()
    for day_num in range(1, calendar.monthrange(view_month.year, view_month.month)[1] + 1):
        current = date(view_month.year, view_month.month, day_num)
        if current >= today:
            selectable.add(current)

    rows = make_calendar_rows(view_month, selectable, "s")
    prev_month = shift_month(view_month, -1)
    next_month = shift_month(view_month, 1)

    nav = [
        InlineKeyboardButton(
            "<",
            callback_data=f"sn:{month_key(prev_month)}" if prev_month >= first_of_current_month else NOOP,
        ),
        InlineKeyboardButton(month_title(view_month), callback_data=NOOP),
        InlineKeyboardButton(">", callback_data=f"sn:{month_key(next_month)}"),
    ]
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


def end_keyboard(view_month: date, start_day: date) -> InlineKeyboardMarkup:
    first_end_month = date(start_day.year, start_day.month, 1)
    if view_month < first_end_month:
        view_month = first_end_month

    selectable: set[date] = set()
    max_day = calendar.monthrange(view_month.year, view_month.month)[1]
    for day_num in range(1, max_day + 1):
        current = date(view_month.year, view_month.month, day_num)
        if current > start_day:
            selectable.add(current)

    rows = make_calendar_rows(view_month, selectable, "e")

    prev_month = shift_month(view_month, -1)
    next_month = shift_month(view_month, 1)
    prev_ok = prev_month >= first_end_month

    nav = [
        InlineKeyboardButton("<", callback_data=f"en:{month_key(prev_month)}" if prev_ok else NOOP),
        InlineKeyboardButton(month_title(view_month), callback_data=NOOP),
        InlineKeyboardButton(">", callback_data=f"en:{month_key(next_month)}"),
    ]
    rows.append(nav)
    return InlineKeyboardMarkup(rows)


async def send_poll_from_state(
    message, context: ContextTypes.DEFAULT_TYPE, time_note: Optional[str] = None
) -> bool:
    track_for_cleanup(context, message)

    start_iso = context.user_data.get("poll_start")
    end_iso = context.user_data.get("poll_end")
    if not start_iso or not end_iso:
        return False

    start_day = date.fromisoformat(start_iso)
    end_day = date.fromisoformat(end_iso)
    day_count = (end_day - start_day).days + 1
    if day_count < 2:
        return False

    options = [format_day(start_day + timedelta(days=i)) for i in range(day_count)]

    chunks = [
        options[i : i + MAX_POLL_OPTIONS]
        for i in range(0, len(options), MAX_POLL_OPTIONS)
    ]
    total = len(chunks)
    poll_message_ids: set[int] = set()
    for idx, chunk in enumerate(chunks, start=1):
        part = None if total == 1 else f"({idx}/{total})"
        poll_message = await message.reply_poll(
            question=build_question(time_note, part=part),
            options=chunk,
            is_anonymous=False,
            allows_multiple_answers=True,
        )
        poll_message_ids.add(poll_message.message_id)

    await cleanup_non_poll_messages(
        context, chat_id=message.chat_id, keep_message_ids=poll_message_ids
    )
    clear_poll_state(context)
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = "Drück uf Umfrog starte."
    await update.message.reply_text(
        text,
        reply_markup=ReplyKeyboardMarkup(
            [[CREATE_POLL_BUTTON]], resize_keyboard=True, one_time_keyboard=False
        ),
    )


async def begin_poll_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    clear_poll_state(context)
    track_for_cleanup(context, message)
    today = date.today()
    view_month = date(today.year, today.month, 1)
    context.user_data["view_month_start"] = view_month.isoformat()

    picker_message = await message.reply_text(
        "Wähl Starttag:",
        reply_markup=start_keyboard(view_month, today),
    )
    track_for_cleanup(context, picker_message)
    return PICK_START


async def start_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return ConversationHandler.END

    await query.answer()
    data = query.data
    today = date.today()

    if data.startswith("sn:"):
        view_month = month_start_from_key(data.split(":", 1)[1])
        context.user_data["view_month_start"] = view_month.isoformat()
        await query.edit_message_text(
            "Wähl Starttag:", reply_markup=start_keyboard(view_month, today)
        )
        return PICK_START

    if data.startswith("s:"):
        start_day = date.fromisoformat(data.split(":", 1)[1])
        if start_day < today:
            await query.answer("Bitte en Tag ab hüt näh.", show_alert=True)
            return PICK_START

        context.user_data["poll_start"] = start_day.isoformat()
        end_view = date(start_day.year, start_day.month, 1)
        context.user_data["view_month_end"] = end_view.isoformat()

        await query.edit_message_text(
            "Wähl Endtag:", reply_markup=end_keyboard(end_view, start_day)
        )
        return PICK_END

    return PICK_START


async def end_picker_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return ConversationHandler.END

    await query.answer()
    data = query.data
    start_iso = context.user_data.get("poll_start")
    if not start_iso:
        await query.edit_message_text("Starttag fehlt. Bitte nomal uf 'Umfrog starte'.")
        return ConversationHandler.END

    start_day = date.fromisoformat(start_iso)
    first_end_month = date(start_day.year, start_day.month, 1)

    if data.startswith("en:"):
        view_month = month_start_from_key(data.split(":", 1)[1])
        if view_month < first_end_month:
            view_month = first_end_month
        context.user_data["view_month_end"] = view_month.isoformat()
        await query.edit_message_text(
            "Wähl Endtag:", reply_markup=end_keyboard(view_month, start_day)
        )
        return PICK_END

    if data.startswith("e:"):
        end_day = date.fromisoformat(data.split(":", 1)[1])
        if end_day <= start_day:
            await query.answer("Endtag muess nach em Starttag si.", show_alert=True)
            return PICK_END

        context.user_data["poll_end"] = end_day.isoformat()

        await query.edit_message_text(
            "Optional: Wotsch no 'weli zit?' setze?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("Ohni", callback_data="t:none")],
                    [InlineKeyboardButton("weli zit?", callback_data="t:ask")],
                ]
            ),
        )
        return PICK_TIME_OPTION

    return PICK_END


async def time_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return ConversationHandler.END

    await query.answer()
    track_for_cleanup(context, query.message)

    if query.data == "t:ask":
        await query.edit_message_text("Schrib d Zyt (z.B. 'ab 19:00').")
        return WAIT_TIME_TEXT

    ok = await send_poll_from_state(query.message, context)
    if not ok:
        await query.edit_message_text("Da hät nöd klappt. Bitte nomal starte.")
        return ConversationHandler.END

    return ConversationHandler.END


async def receive_time_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    if message is None:
        return ConversationHandler.END

    track_for_cleanup(context, message)
    note = (message.text or "").strip()
    if not note:
        await message.reply_text("Bitte gib e chli Angab ii, z.B. 'ab 19:00'.")
        return WAIT_TIME_TEXT

    ok = await send_poll_from_state(message, context, time_note=note)
    if not ok:
        await message.reply_text("Da hät nöd klappt. Bitte nomal starte.")
        return ConversationHandler.END

    return ConversationHandler.END


async def noop_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    return PICK_START


async def noop_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    return PICK_END


async def noop_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
    return PICK_TIME_OPTION


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_poll_state(context)
    if update.effective_message:
        await update.effective_message.reply_text("Abbroche.")
    return ConversationHandler.END


async def poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    track_for_cleanup(context, update.message)
    if len(context.args) < 2:
        await start_command(update, context)
        return

    today = date.today()

    try:
        start_day = parse_day(context.args[0], today)
        end_day = parse_day(context.args[1], today)
    except ValueError:
        await update.message.reply_text("Datum nöd erkannt. Nimm YYYY-MM-DD, today oder +N.")
        return

    if end_day < start_day:
        await update.message.reply_text("Endtag muess gliich oder spöter si.")
        return

    day_count = (end_day - start_day).days + 1
    if day_count < 2:
        await update.message.reply_text("Bitte mind. 2 Täg wähle.")
        return

    time_note = " ".join(context.args[2:]).strip()
    context.user_data["poll_start"] = start_day.isoformat()
    context.user_data["poll_end"] = end_day.isoformat()
    ok = await send_poll_from_state(update.message, context, time_note=time_note or None)
    if not ok:
        await update.message.reply_text("Da hät nöd klappt. Bitte nomal starte.")


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or load_token_from_dotenv()
    if not token:
        raise RuntimeError(
            "Set TELEGRAM_BOT_TOKEN (env var oder .env) bevor du dr Bot startisch."
        )

    app = Application.builder().token(token).build()

    picker = ConversationHandler(
        entry_points=[
            CommandHandler("newpoll", begin_poll_picker),
            MessageHandler(filters.Regex(r"^Umfrog starte$"), begin_poll_picker),
        ],
        states={
            PICK_START: [
                CallbackQueryHandler(noop_start, pattern=r"^x$"),
                CallbackQueryHandler(start_picker_callback, pattern=r"^(s:|sn:)")
            ],
            PICK_END: [
                CallbackQueryHandler(noop_end, pattern=r"^x$"),
                CallbackQueryHandler(end_picker_callback, pattern=r"^(e:|en:)")
            ],
            PICK_TIME_OPTION: [
                CallbackQueryHandler(noop_time, pattern=r"^x$"),
                CallbackQueryHandler(time_option_callback, pattern=r"^t:(none|ask)$")
            ],
            WAIT_TIME_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_time_text)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("poll", poll_command))
    app.add_handler(picker)

    app.run_polling()


if __name__ == "__main__":
    main()
