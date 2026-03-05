import asyncio
import calendar
import contextlib
import html
import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    PollAnswerHandler,
    filters,
)

MAX_POLL_OPTIONS = 10
POLL_QUESTION = "chasch no?"
CREATE_POLL_BUTTON = "Umfrag machä"
NOOP = "x"
DEFAULT_STATS_PATH = "data/stats.json"
DEFAULT_REMINDER_PATH = "data/reminders.json"
POLL_IDLE_TIMEOUT_SECONDS = 20
MAX_TRACKED_BOT_MESSAGES_PER_CHAT = 250
REMINDER_INTERVAL_SECONDS = 5 * 24 * 60 * 60
REMINDER_LOOP_SLEEP_SECONDS = 60
REMINDER_MENTION_CHUNK_SIZE = 8
INTRO_TEXT = (
    "Hoii i machä poll\n\n"
    "1. /start startet mi\n"
    "2. „Umfrag machä\"\n"
    "3. /cancel zum abbreche\n"
    "4. /wäg uf mini nachricht antworte zum sä lösche\n"
    "5. /hilf zeigt das menu\n"
    "6. /chillmau -> nüm pinge\n"
    "7. /nostress -> mini Umfrag nüm reminderä"
)

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


def resolve_stats_path() -> Path:
    raw = os.getenv("POLLINATOR_STATS_PATH", DEFAULT_STATS_PATH).strip() or DEFAULT_STATS_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def load_stats() -> dict:
    defaults = {"total_polls": 0, "group_ids": []}
    stats_path = resolve_stats_path()
    if not stats_path.exists():
        return defaults

    try:
        raw = json.loads(stats_path.read_text(encoding="utf-8"))
        total = int(raw.get("total_polls", 0))
        group_ids = [int(chat_id) for chat_id in raw.get("group_ids", [])]
        return {"total_polls": max(total, 0), "group_ids": sorted(set(group_ids))}
    except Exception:
        return defaults


def save_stats(stats: dict) -> None:
    stats_path = resolve_stats_path()
    stats_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "total_polls": int(stats.get("total_polls", 0)),
        "group_ids": [int(chat_id) for chat_id in stats.get("group_ids", [])],
    }
    tmp_path = stats_path.with_suffix(stats_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(stats_path)


def record_poll_stats(chat) -> None:
    stats = load_stats()
    stats["total_polls"] = int(stats.get("total_polls", 0)) + 1

    chat_type = getattr(chat, "type", "")
    chat_id = getattr(chat, "id", None)
    if chat_type in ("group", "supergroup") and chat_id is not None:
        groups = set(int(item) for item in stats.get("group_ids", []))
        groups.add(int(chat_id))
        stats["group_ids"] = sorted(groups)

    save_stats(stats)


def get_stats_snapshot() -> tuple[int, int]:
    stats = load_stats()
    total = int(stats.get("total_polls", 0))
    groups = len(set(int(item) for item in stats.get("group_ids", [])))
    return total, groups


def resolve_reminder_path() -> Path:
    raw = os.getenv("POLLINATOR_REMINDER_PATH", DEFAULT_REMINDER_PATH).strip() or DEFAULT_REMINDER_PATH
    path = Path(raw)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    return path


def load_reminder_state() -> dict:
    defaults = {"polls": {}, "chats": {}}
    reminder_path = resolve_reminder_path()
    if not reminder_path.exists():
        return defaults

    try:
        raw = json.loads(reminder_path.read_text(encoding="utf-8"))
        polls = raw.get("polls", {})
        chats = raw.get("chats", {})
        if not isinstance(polls, dict) or not isinstance(chats, dict):
            return defaults
        return {"polls": polls, "chats": chats}
    except Exception:
        return defaults


def save_reminder_state(state: dict) -> None:
    reminder_path = resolve_reminder_path()
    reminder_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "polls": state.get("polls", {}),
        "chats": state.get("chats", {}),
    }
    tmp_path = reminder_path.with_suffix(reminder_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(reminder_path)


def get_reminder_lock(application: Application) -> asyncio.Lock:
    lock = application.bot_data.get("reminder_lock")
    if lock is None:
        lock = asyncio.Lock()
        application.bot_data["reminder_lock"] = lock
    return lock


def ensure_chat_state(state: dict, chat_id: int) -> dict:
    chats = state.setdefault("chats", {})
    chat_key = str(chat_id)
    chat = chats.setdefault(chat_key, {})
    chat.setdefault("known_users", {})
    chat.setdefault("chilled_users", [])
    return chat


def upsert_known_user(state: dict, chat_id: int, user) -> None:
    if user is None:
        return
    chat = ensure_chat_state(state, chat_id)
    known = chat.setdefault("known_users", {})
    known[str(user.id)] = {
        "id": int(user.id),
        "username": (user.username or ""),
        "first_name": (user.first_name or "User"),
        "is_bot": bool(user.is_bot),
    }


def upsert_known_user_meta(state: dict, chat_id: int, meta: dict) -> None:
    if not meta or meta.get("id") is None:
        return
    chat = ensure_chat_state(state, chat_id)
    known = chat.setdefault("known_users", {})
    known[str(int(meta["id"]))] = {
        "id": int(meta["id"]),
        "username": (meta.get("username") or ""),
        "first_name": (meta.get("first_name") or "User"),
        "is_bot": bool(meta.get("is_bot", False)),
    }


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def user_ping(meta: dict) -> tuple[str, bool]:
    username = (meta.get("username") or "").strip()
    if username:
        return (f"@{username.lstrip('@')}", False)
    user_id = int(meta.get("id", 0))
    first_name = html.escape((meta.get("first_name") or "du"))
    return (f'<a href="tg://user?id={user_id}">{first_name}</a>', True)


async def remember_chat_user(
    application: Application,
    chat_id: int,
    user,
) -> None:
    lock = get_reminder_lock(application)
    async with lock:
        state = load_reminder_state()
        upsert_known_user(state, chat_id, user)
        save_reminder_state(state)


async def register_poll_reminder(
    application: Application,
    chat_id: int,
    message_id: int,
    poll_id: str,
    initiator: Optional[dict],
    series_id: str,
) -> None:
    lock = get_reminder_lock(application)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    async with lock:
        state = load_reminder_state()
        polls = state.setdefault("polls", {})
        chat = ensure_chat_state(state, chat_id)
        if initiator is not None:
            upsert_known_user_meta(state, chat_id, initiator)
        polls[poll_id] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "initiator_id": int(initiator["id"]) if initiator and initiator.get("id") is not None else None,
            "series_id": series_id,
            "enabled": True,
            "created_at": now_ts,
            "next_reminder_at": now_ts + REMINDER_INTERVAL_SECONDS,
            "voters": [],
        }
        chat.setdefault("known_users", {})
        chat.setdefault("chilled_users", [])
        save_reminder_state(state)


async def update_poll_vote(application: Application, poll_id: str, user, has_vote: bool) -> None:
    lock = get_reminder_lock(application)
    async with lock:
        state = load_reminder_state()
        poll = state.get("polls", {}).get(poll_id)
        if poll is None:
            return

        chat_id = int(poll.get("chat_id"))
        upsert_known_user(state, chat_id, user)

        voters = set(int(item) for item in poll.get("voters", []))
        if has_vote:
            voters.add(int(user.id))
        else:
            voters.discard(int(user.id))
        poll["voters"] = sorted(voters)
        save_reminder_state(state)


async def set_user_chilled(application: Application, chat_id: int, user) -> None:
    lock = get_reminder_lock(application)
    async with lock:
        state = load_reminder_state()
        upsert_known_user(state, chat_id, user)
        chat = ensure_chat_state(state, chat_id)
        chilled = set(int(item) for item in chat.get("chilled_users", []))
        chilled.add(int(user.id))
        chat["chilled_users"] = sorted(chilled)
        save_reminder_state(state)


async def disable_poll_reminders(
    application: Application,
    chat_id: int,
    requester_id: int,
    replied_poll_id: Optional[str],
) -> tuple[bool, str]:
    lock = get_reminder_lock(application)
    async with lock:
        state = load_reminder_state()
        polls = state.setdefault("polls", {})
        target = None

        if replied_poll_id:
            target = polls.get(replied_poll_id)
            if target is None or int(target.get("chat_id", 0)) != int(chat_id):
                return (False, "Poll nöd gfunde. Bitte uf de Poll antworte.")
        else:
            candidates = [
                poll
                for poll in polls.values()
                if int(poll.get("chat_id", 0)) == int(chat_id)
                and bool(poll.get("enabled", True))
                and int(poll.get("initiator_id", 0) or 0) == int(requester_id)
            ]
            if not candidates:
                return (False, "I ha kei aktivi Umfrag vo dir gfunde.")
            if len(candidates) > 1:
                return (False, "Bitte uf d Umfrag antworte und /nostress schicke.")
            target = candidates[0]

        if int(target.get("initiator_id", 0) or 0) != int(requester_id):
            return (False, "Nur dr Initiant cha /nostress für die Umfrag mache.")

        series_id = target.get("series_id")
        changed = 0
        for poll in polls.values():
            if int(poll.get("chat_id", 0)) != int(chat_id):
                continue
            same_series = series_id and poll.get("series_id") == series_id
            if same_series or (replied_poll_id and poll is target) or (not series_id and poll is target):
                if bool(poll.get("enabled", True)):
                    poll["enabled"] = False
                    changed += 1

        save_reminder_state(state)
        if changed:
            return (True, "Alles guet, kei Reminder meh für die Umfrag.")
        return (True, "Die Umfrag isch scho uf nöd-stresse.")


async def claim_due_poll_ids(application: Application) -> list[str]:
    lock = get_reminder_lock(application)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    async with lock:
        state = load_reminder_state()
        polls = state.setdefault("polls", {})
        due: list[str] = []
        changed = False
        for poll_id, poll in polls.items():
            if not bool(poll.get("enabled", True)):
                continue
            next_ts = int(poll.get("next_reminder_at", 0) or 0)
            if next_ts and next_ts <= now_ts:
                due.append(poll_id)
                while next_ts <= now_ts:
                    next_ts += REMINDER_INTERVAL_SECONDS
                poll["next_reminder_at"] = next_ts
                changed = True

        if changed:
            save_reminder_state(state)
        return due


async def build_reminder_payload(application: Application, poll_id: str) -> Optional[dict]:
    lock = get_reminder_lock(application)
    async with lock:
        state = load_reminder_state()
        poll = state.get("polls", {}).get(poll_id)
        if poll is None or not bool(poll.get("enabled", True)):
            return None

        chat_id = int(poll.get("chat_id", 0))
        chat = ensure_chat_state(state, chat_id)
        known = chat.get("known_users", {})
        chilled = set(int(item) for item in chat.get("chilled_users", []))
        voters = set(int(item) for item in poll.get("voters", []))

        targets = []
        for user_id, meta in known.items():
            uid = int(user_id)
            if meta.get("is_bot"):
                continue
            if uid in chilled or uid in voters:
                continue
            targets.append(meta)

        return {
            "chat_id": chat_id,
            "message_id": int(poll.get("message_id", 0)),
            "targets": targets,
        }


async def send_reminder_for_poll(application: Application, poll_id: str) -> None:
    payload = await build_reminder_payload(application, poll_id)
    if payload is None:
        return

    pings: list[str] = []
    needs_html = False
    for meta in payload["targets"]:
        mention, is_html = user_ping(meta)
        pings.append(mention)
        needs_html = needs_html or is_html

    if not pings:
        return

    for part in chunked(pings, REMINDER_MENTION_CHUNK_SIZE):
        text = "chasch no? " + " ".join(part)
        await application.bot.send_message(
            chat_id=payload["chat_id"],
            text=text,
            reply_to_message_id=payload["message_id"],
            allow_sending_without_reply=True,
            parse_mode="HTML" if needs_html else None,
        )


async def reminder_loop(application: Application) -> None:
    while True:
        try:
            for poll_id in await claim_due_poll_ids(application):
                await send_reminder_for_poll(application, poll_id)
        except Exception:
            logging.exception("Reminder loop failed.")
        await asyncio.sleep(REMINDER_LOOP_SLEEP_SECONDS)


async def post_init(application: Application) -> None:
    get_reminder_lock(application)
    if application.bot_data.get("reminder_task") is None:
        application.bot_data["reminder_task"] = asyncio.create_task(reminder_loop(application))


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("reminder_task")
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def clear_poll_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("poll_start", None)
    context.user_data.pop("poll_end", None)
    context.user_data.pop("view_month_start", None)
    context.user_data.pop("view_month_end", None)
    context.user_data.pop("poll_initiator", None)
    context.user_data.pop("cleanup_message_ids", None)
    context.user_data.pop("poll_timeout_token", None)


def remember_bot_message(context: ContextTypes.DEFAULT_TYPE, message) -> None:
    if message is None:
        return

    by_chat = context.application.bot_data.setdefault("recent_bot_messages", {})
    chat_key = str(message.chat_id)
    ids = by_chat.setdefault(chat_key, [])
    if message.message_id not in ids:
        ids.append(message.message_id)
    if len(ids) > MAX_TRACKED_BOT_MESSAGES_PER_CHAT:
        del ids[:-MAX_TRACKED_BOT_MESSAGES_PER_CHAT]


def forget_bot_message_id(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    by_chat = context.application.bot_data.setdefault("recent_bot_messages", {})
    chat_key = str(chat_id)
    ids = by_chat.get(chat_key, [])
    by_chat[chat_key] = [mid for mid in ids if mid != message_id]
    if not by_chat[chat_key]:
        by_chat.pop(chat_key, None)


async def reply_text_tracked(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    **kwargs,
):
    sent = await message.reply_text(text, **kwargs)
    remember_bot_message(context, sent)
    return sent


async def reply_poll_tracked(
    message,
    context: ContextTypes.DEFAULT_TYPE,
    **kwargs,
):
    sent = await message.reply_poll(**kwargs)
    remember_bot_message(context, sent)
    return sent


async def send_message_tracked(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
    **kwargs,
):
    sent = await context.bot.send_message(chat_id=chat_id, text=text, **kwargs)
    remember_bot_message(context, sent)
    return sent


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
        forget_bot_message_id(context, chat_id=chat_id, message_id=message_id)

    context.user_data.pop("cleanup_message_ids", None)


def arm_poll_timeout(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    token = os.urandom(8).hex()
    context.user_data["poll_timeout_token"] = token
    asyncio.create_task(poll_timeout_worker(context, chat_id, token))


async def poll_timeout_worker(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    token: str,
) -> None:
    await asyncio.sleep(POLL_IDLE_TIMEOUT_SECONDS)
    if context.user_data.get("poll_timeout_token") != token:
        return

    try:
        # Clear sticky reply keyboard without leaving a visible message behind.
        remover = await send_message_tracked(
            context,
            chat_id=chat_id,
            text=".",
            reply_markup=ReplyKeyboardRemove(),
        )
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=remover.message_id)
        except Exception:
            pass
        forget_bot_message_id(context, chat_id=chat_id, message_id=remover.message_id)
    except Exception:
        pass

    await cleanup_non_poll_messages(context, chat_id=chat_id, keep_message_ids=set())
    clear_poll_state(context)


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
    initiator = context.user_data.get("poll_initiator")
    series_id = os.urandom(8).hex()
    poll_message_ids: set[int] = set()
    for idx, chunk in enumerate(chunks, start=1):
        part = None if total == 1 else f"({idx}/{total})"
        poll_message = await reply_poll_tracked(
            message,
            context,
            question=build_question(time_note, part=part),
            options=chunk,
            is_anonymous=False,
            allows_multiple_answers=True,
        )
        poll_message_ids.add(poll_message.message_id)
        if poll_message.poll is not None:
            await register_poll_reminder(
                context.application,
                chat_id=poll_message.chat_id,
                message_id=poll_message.message_id,
                poll_id=poll_message.poll.id,
                initiator=initiator,
                series_id=series_id,
            )

    try:
        record_poll_stats(message.chat)
    except Exception:
        logging.exception("Could not write poll stats.")

    await cleanup_non_poll_messages(
        context, chat_id=message.chat_id, keep_message_ids=poll_message_ids
    )
    clear_poll_state(context)
    return True


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    sent = await reply_text_tracked(
        message,
        context,
        INTRO_TEXT,
        reply_markup=ReplyKeyboardMarkup(
            [[CREATE_POLL_BUTTON]], resize_keyboard=True, one_time_keyboard=False
        ),
    )
    context.user_data["start_hint_message_id"] = sent.message_id
    context.user_data["start_hint_chat_id"] = sent.chat_id


async def begin_poll_picker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.effective_message
    if message is None:
        return ConversationHandler.END

    hint_message_id = context.user_data.pop("start_hint_message_id", None)
    hint_chat_id = context.user_data.pop("start_hint_chat_id", None)
    if hint_message_id is not None:
        try:
            await context.bot.delete_message(
                chat_id=hint_chat_id or message.chat_id,
                message_id=hint_message_id,
            )
        except Exception:
            pass
        forget_bot_message_id(
            context,
            chat_id=hint_chat_id or message.chat_id,
            message_id=hint_message_id,
        )

    clear_poll_state(context)
    track_for_cleanup(context, message)
    arm_poll_timeout(context, message.chat_id)
    user = update.effective_user
    if user is not None:
        context.user_data["poll_initiator"] = {
            "id": int(user.id),
            "username": user.username or "",
            "first_name": user.first_name or "User",
            "is_bot": bool(user.is_bot),
        }

    today = date.today()
    view_month = date(today.year, today.month, 1)
    context.user_data["view_month_start"] = view_month.isoformat()

    picker_message = await reply_text_tracked(
        message,
        context,
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
    if query.message is not None:
        arm_poll_timeout(context, query.message.chat_id)
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
    if query.message is not None:
        arm_poll_timeout(context, query.message.chat_id)
    data = query.data
    start_iso = context.user_data.get("poll_start")
    if not start_iso:
        await query.edit_message_text("Starttag fehlt. Bitte nomal uf 'Umfrag machä'.")
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
    if query.message is not None:
        arm_poll_timeout(context, query.message.chat_id)

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
    arm_poll_timeout(context, message.chat_id)
    note = (message.text or "").strip()
    if not note:
        await reply_text_tracked(message, context, "Bitte gib e chli Angab ii, z.B. 'ab 19:00'.")
        return WAIT_TIME_TEXT

    ok = await send_poll_from_state(message, context, time_note=note)
    if not ok:
        await reply_text_tracked(message, context, "Da hät nöd klappt. Bitte nomal starte.")
        return ConversationHandler.END

    return ConversationHandler.END


async def noop_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        if query.message is not None:
            arm_poll_timeout(context, query.message.chat_id)
    return PICK_START


async def noop_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        if query.message is not None:
            arm_poll_timeout(context, query.message.chat_id)
    return PICK_END


async def noop_time(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query:
        await query.answer()
        if query.message is not None:
            arm_poll_timeout(context, query.message.chat_id)
    return PICK_TIME_OPTION


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    clear_poll_state(context)
    if update.effective_message:
        await reply_text_tracked(update.effective_message, context, "Abbroche.")
    return ConversationHandler.END


async def poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return

    user = update.effective_user
    if user is not None:
        context.user_data["poll_initiator"] = {
            "id": int(user.id),
            "username": user.username or "",
            "first_name": user.first_name or "User",
            "is_bot": bool(user.is_bot),
        }
    track_for_cleanup(context, update.message)
    if len(context.args) < 2:
        await start_command(update, context)
        return

    today = date.today()

    try:
        start_day = parse_day(context.args[0], today)
        end_day = parse_day(context.args[1], today)
    except ValueError:
        await reply_text_tracked(update.message, context, "Datum nöd erkannt. Nimm YYYY-MM-DD, today oder +N.")
        return

    if end_day < start_day:
        await reply_text_tracked(update.message, context, "Endtag muess gliich oder spöter si.")
        return

    day_count = (end_day - start_day).days + 1
    if day_count < 2:
        await reply_text_tracked(update.message, context, "Bitte mind. 2 Täg wähle.")
        return

    time_note = " ".join(context.args[2:]).strip()
    context.user_data["poll_start"] = start_day.isoformat()
    context.user_data["poll_end"] = end_day.isoformat()
    ok = await send_poll_from_state(update.message, context, time_note=time_note or None)
    if not ok:
        await reply_text_tracked(update.message, context, "Da hät nöd klappt. Bitte nomal starte.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    total_polls, group_count = get_stats_snapshot()
    await reply_text_tracked(
        message,
        context,
        f"Bis jetzt gmacht:\n- Polls: {total_polls}\n- Gruppä: {group_count}"
    )


async def chillmau_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return

    await set_user_chilled(context.application, message.chat_id, user)
    await reply_text_tracked(message, context, "Alles guet, i ping di nüm.")


async def nostress_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    user = update.effective_user
    if message is None or user is None:
        return

    replied_poll_id = None
    if message.reply_to_message is not None and message.reply_to_message.poll is not None:
        replied_poll_id = message.reply_to_message.poll.id

    ok, response = await disable_poll_reminders(
        context.application,
        chat_id=message.chat_id,
        requester_id=user.id,
        replied_poll_id=replied_poll_id,
    )
    if ok:
        await reply_text_tracked(message, context, response)
        return
    await reply_text_tracked(message, context, response)


async def remember_seen_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None or user is None:
        return
    if chat.type not in ("group", "supergroup"):
        return
    await remember_chat_user(context.application, chat.id, user)


async def poll_answer_update_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    answer = update.poll_answer
    if answer is None:
        return
    await update_poll_vote(
        context.application,
        poll_id=answer.poll_id,
        user=answer.user,
        has_vote=bool(answer.option_ids),
    )


async def del_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    reply = message.reply_to_message
    if reply is not None:
        try:
            await context.bot.delete_message(chat_id=message.chat_id, message_id=reply.message_id)
        except Exception:
            pass
        forget_bot_message_id(context, chat_id=message.chat_id, message_id=reply.message_id)

    try:
        await context.bot.delete_message(chat_id=message.chat_id, message_id=message.message_id)
    except Exception:
        pass


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

    app = (
        Application.builder()
        .token(token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    picker = ConversationHandler(
        entry_points=[
            CommandHandler("newpoll", begin_poll_picker),
            MessageHandler(filters.Regex(r"^Umfrag machä$"), begin_poll_picker),
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

    app.add_handler(MessageHandler(filters.ALL, remember_seen_user_handler), group=-1)
    app.add_handler(PollAnswerHandler(poll_answer_update_handler))
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("hilf", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("poll", poll_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("chillmau", chillmau_command))
    app.add_handler(CommandHandler("nostress", nostress_command))
    app.add_handler(CommandHandler("del", del_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/(wäg|waeg)(?:@[A-Za-z0-9_]+)?$"), del_command))
    app.add_handler(picker)

    app.run_polling()


if __name__ == "__main__":
    main()
