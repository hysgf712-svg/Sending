"""Custom Telegram bot with force-join, a Manybot-style content menu, and multi-message broadcasts.

No user-facing file-uploader, file-link, password-gated download, or upload-group flow remains.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from pyrogram import Client, enums, filters, idle
from pyrogram.errors import FloodWait, MessageNotModified, UserNotParticipant
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
def load_env_file() -> None:
    """Load the project .env for direct launches; host environment has priority."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    try:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '\"'):
                value = value[1:-1]
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as exc:
        print(f"Could not read .env file: {exc}", flush=True)


load_env_file()


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Environment variable {name} is required")
    return value

API_ID = int(required_env("API_ID"))
API_HASH = required_env("API_HASH")
BOT_TOKEN = required_env("BOT_TOKEN")
OWNER_ID = int(required_env("OWNER_ID"))

PROJECT_DIR = Path(__file__).resolve().parent
_raw_data_dir = Path(os.getenv("DATA_DIR", str(PROJECT_DIR / "data")))
DATA_DIR = _raw_data_dir if _raw_data_dir.is_absolute() else PROJECT_DIR / _raw_data_dir
DATA_DIR.mkdir(parents=True, exist_ok=True)
_raw_db_path = Path(os.getenv("DB_PATH", str(DATA_DIR / "bot.sqlite3")))
DB_PATH = _raw_db_path if _raw_db_path.is_absolute() else PROJECT_DIR / _raw_db_path
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("custom_bot")

app = Client(
    "CustomizableBot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    parse_mode=enums.ParseMode.DISABLED,
)

# ---------------------------------------------------------------------------
# Persistent local SQLite storage
# ---------------------------------------------------------------------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA foreign_keys=ON")
conn.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id    INTEGER PRIMARY KEY,
        username   TEXT,
        first_name TEXT,
        last_name  TEXT,
        first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen  TEXT DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS user_buttons (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        label        TEXT NOT NULL,
        kind         TEXT NOT NULL,
        url          TEXT NOT NULL DEFAULT '',
        content_json TEXT NOT NULL DEFAULT '[]',
        position     INTEGER NOT NULL DEFAULT 0
    );
    """
)
conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
conn.commit()

DEFAULT_START_CONTENT: List[Dict[str, Any]] = [
    {"kind": "text", "text": "سلام 👋\nبه ربات خوش آمدید."}
]
DEFAULT_SETTINGS: Dict[str, str] = {
    "start_content": json.dumps(DEFAULT_START_CONTENT, ensure_ascii=False),
    "force_channels": "[]",
    "force_text": "⛔ برای استفاده از ربات، ابتدا عضو کانال‌های زیر شوید:",
    "join_button_template": "🔔 عضویت در {title}",
    "check_button_text": "✅ بررسی عضویت",
}

for _key, _value in DEFAULT_SETTINGS.items():
    conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (_key, _value))
conn.commit()

# Temporary workflows are intentionally in memory; permanent settings/buttons/users are in SQLite.
admin_states: Dict[int, Dict[str, Any]] = {}
MAX_USER_BUTTONS = 30
MAX_SEQUENCE_ITEMS = 50


def get_setting(key: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is not None:
        return str(row["value"])
    return DEFAULT_SETTINGS.get(key, "")


def set_setting(key: str, value: Any) -> None:
    if isinstance(value, (list, dict)):
        serialized = json.dumps(value, ensure_ascii=False)
    else:
        serialized = str(value)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, serialized),
    )
    conn.commit()


def get_json_setting(key: str, fallback: Any) -> Any:
    try:
        return json.loads(get_setting(key))
    except (json.JSONDecodeError, TypeError):
        return fallback


def is_admin(user_id: int) -> bool:
    return user_id == OWNER_ID or conn.execute(
        "SELECT 1 FROM admins WHERE user_id=?", (user_id,)
    ).fetchone() is not None


def add_user(user: Any) -> None:
    if not user:
        return
    conn.execute(
        """INSERT INTO users (user_id, username, first_name, last_name)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
             username=excluded.username,
             first_name=excluded.first_name,
             last_name=excluded.last_name,
             last_seen=CURRENT_TIMESTAMP""",
        (user.id, user.username, user.first_name, user.last_name),
    )
    conn.commit()


def force_channels() -> List[str]:
    value = get_json_setting("force_channels", [])
    return [str(item) for item in value] if isinstance(value, list) else []


def get_start_content() -> List[Dict[str, Any]]:
    value = get_json_setting("start_content", DEFAULT_START_CONTENT)
    if not isinstance(value, list) or not value:
        return DEFAULT_START_CONTENT.copy()
    return value


def get_buttons() -> List[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM user_buttons ORDER BY position, id"
    ).fetchall()


def get_button(button_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM user_buttons WHERE id=?", (button_id,)).fetchone()


def save_button(
    label: str,
    kind: str,
    url: str = "",
    content: Optional[List[Dict[str, Any]]] = None,
    button_id: Optional[int] = None,
) -> int:
    content_json = json.dumps(content or [], ensure_ascii=False)
    if button_id is None:
        row = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM user_buttons").fetchone()
        cur = conn.execute(
            "INSERT INTO user_buttons (label, kind, url, content_json, position) VALUES (?, ?, ?, ?, ?)",
            (label, kind, url, content_json, int(row["p"])),
        )
        button_id = int(cur.lastrowid)
    else:
        conn.execute(
            "UPDATE user_buttons SET label=?, kind=?, url=?, content_json=? WHERE id=?",
            (label, kind, url, content_json, button_id),
        )
    conn.commit()
    return button_id


def update_button_fields(button_id: int, **fields: str) -> None:
    allowed = {"label", "url", "kind", "content_json"}
    values = {key: value for key, value in fields.items() if key in allowed}
    if not values:
        return
    assignments = ", ".join(f"{key}=?" for key in values)
    conn.execute(
        f"UPDATE user_buttons SET {assignments} WHERE id=?",
        [*values.values(), button_id],
    )
    conn.commit()


def delete_button(button_id: int) -> None:
    conn.execute("DELETE FROM user_buttons WHERE id=?", (button_id,))
    conn.commit()


# ---------------------------------------------------------------------------
# Force subscription
# ---------------------------------------------------------------------------
def parse_chat_id(value: str) -> Any:
    value = str(value).strip()
    if value.lstrip("-").isdigit():
        return int(value)
    return value


def normalize_channel(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("https://t.me/") or value.startswith("http://t.me/"):
        value = value.split("t.me/", 1)[1].split("?", 1)[0].strip("/")
        if value.startswith("+") or value.startswith("joinchat/"):
            raise ValueError("برای کانال خصوصی، آیدی عددی -100… را وارد کنید؛ لینک دعوت به‌تنهایی کافی نیست.")
        value = "@" + value
    if value.startswith("@") and len(value) > 1:
        return value
    if value.lstrip("-").isdigit():
        return value
    raise ValueError("آیدی معتبر بفرستید؛ نمونه: @channel یا -1001234567890")


async def get_chat_invite(client: Client, channel: str) -> Tuple[str, str]:
    chat_id = parse_chat_id(channel)
    try:
        chat = await client.get_chat(chat_id)
        title = chat.title or chat.first_name or channel
        if chat.username:
            return f"https://t.me/{chat.username}", title
        link = chat.invite_link
        if not link:
            link = await client.export_chat_invite_link(chat_id)
        return link, title
    except Exception as exc:
        logger.warning("Could not resolve force-join link for %s: %s", channel, exc)
        return "https://t.me/", channel


async def check_membership(client: Client, user_id: int) -> Tuple[bool, List[Dict[str, str]]]:
    missing: List[Dict[str, str]] = []
    for channel in force_channels():
        chat_id = parse_chat_id(channel)
        try:
            member = await client.get_chat_member(chat_id, user_id)
            if member.status in (enums.ChatMemberStatus.LEFT, enums.ChatMemberStatus.BANNED):
                link, title = await get_chat_invite(client, channel)
                missing.append({"id": channel, "link": link, "title": title})
        except UserNotParticipant:
            link, title = await get_chat_invite(client, channel)
            missing.append({"id": channel, "link": link, "title": title})
        except Exception as exc:
            # Fail closed: if membership cannot be verified, do not silently bypass the requirement.
            logger.warning("Membership check failed for %s / user %s: %s", channel, user_id, exc)
            link, title = await get_chat_invite(client, channel)
            missing.append({"id": channel, "link": link, "title": title})
    return not missing, missing


def force_join_markup(missing: Sequence[Dict[str, str]]) -> ReplyKeyboardMarkup:
    """Reply-keyboard check button; channel links are shown as clickable text URLs."""
    check_label = get_setting("check_button_text") or "✅ بررسی عضویت"
    return ReplyKeyboardMarkup(
        [[KeyboardButton(check_label[:64])]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


async def show_force_join(client: Client, chat_id: int, missing: Sequence[Dict[str, str]]) -> None:
    text = get_setting("force_text") or DEFAULT_SETTINGS["force_text"]
    template = get_setting("join_button_template") or "🔔 عضویت در {title}"
    for i, channel in enumerate(missing, 1):
        try:
            label = template.format(title=channel["title"])
        except Exception:
            label = f"🔔 {channel['title']}"
        # Reply-keyboard buttons cannot open URLs; Telegram auto-links these URLs in the message.
        text += f"\n\n{i}. {label}\n{channel['link']}"
    await client.send_message(chat_id, text, reply_markup=force_join_markup(missing))


# ---------------------------------------------------------------------------
# Reusable message content
# ---------------------------------------------------------------------------
def make_content_item(message: Message) -> Optional[Dict[str, Any]]:
    """Store a source-message reference plus file-id/text fallbacks.

    The reference allows Telegram to reproduce text formatting and almost any message type;
    the file_id fallback keeps common media usable if the admin later removes the source message.
    """
    text = message.text or ""
    caption = message.caption or ""
    if not text and not caption and not message.media:
        return None

    item: Dict[str, Any] = {
        "source_chat_id": int(message.chat.id),
        "source_message_id": int(message.id),
        "kind": "text" if text else "copy",
        "text": text,
        "caption": caption,
    }

    media_fields = (
        "photo", "video", "document", "animation", "audio", "voice",
        "video_note", "sticker",
    )
    for field in media_fields:
        media = getattr(message, field, None)
        if media:
            item["kind"] = field
            item["file_id"] = getattr(media, "file_id", "")
            break

    location = getattr(message, "location", None)
    if location:
        item["kind"] = "location"
        item["latitude"] = location.latitude
        item["longitude"] = location.longitude
    contact = getattr(message, "contact", None)
    if contact:
        item["kind"] = "contact"
        item["phone_number"] = contact.phone_number
        item["first_name"] = contact.first_name
        item["last_name"] = contact.last_name or ""
        item["vcard"] = contact.vcard or ""
    return item


async def send_fallback(
    client: Client,
    chat_id: int,
    item: Dict[str, Any],
    reply_markup: Optional[Any] = None,
) -> Message:
    kind = item.get("kind", "copy")
    text = item.get("text", "")
    caption = item.get("caption", "")
    file_id = item.get("file_id", "")

    if kind == "text" and text:
        return await client.send_message(chat_id, text, reply_markup=reply_markup)
    if kind == "location":
        return await client.send_location(
            chat_id,
            latitude=float(item["latitude"]),
            longitude=float(item["longitude"]),
            reply_markup=reply_markup,
        )
    if kind == "contact":
        return await client.send_contact(
            chat_id,
            phone_number=item["phone_number"],
            first_name=item["first_name"],
            last_name=item.get("last_name", ""),
            vcard=item.get("vcard", ""),
            reply_markup=reply_markup,
        )
    if file_id:
        if kind == "photo":
            return await client.send_photo(chat_id, file_id, caption=caption or None, reply_markup=reply_markup)
        if kind == "video":
            return await client.send_video(chat_id, file_id, caption=caption or None, reply_markup=reply_markup)
        if kind == "document":
            return await client.send_document(chat_id, file_id, caption=caption or None, reply_markup=reply_markup)
        if kind == "animation":
            return await client.send_animation(chat_id, file_id, caption=caption or None, reply_markup=reply_markup)
        if kind == "audio":
            return await client.send_audio(chat_id, file_id, caption=caption or None, reply_markup=reply_markup)
        if kind == "voice":
            return await client.send_voice(chat_id, file_id, caption=caption or None, reply_markup=reply_markup)
        if kind == "video_note":
            return await client.send_video_note(chat_id, file_id, reply_markup=reply_markup)
        if kind == "sticker":
            return await client.send_sticker(chat_id, file_id, reply_markup=reply_markup)
    if text or caption:
        return await client.send_message(chat_id, text or caption, reply_markup=reply_markup)
    raise RuntimeError("پیام منبع دیگر در دسترس نیست و نسخهٔ جایگزین ذخیره‌شده ندارد.")


async def send_content_item(
    client: Client,
    chat_id: int,
    item: Dict[str, Any],
    reply_markup: Optional[Any] = None,
) -> Message:
    source_chat_id = item.get("source_chat_id")
    source_message_id = item.get("source_message_id")
    if source_chat_id and source_message_id:
        try:
            return await client.copy_message(
                chat_id=chat_id,
                from_chat_id=int(source_chat_id),
                message_id=int(source_message_id),
                reply_markup=reply_markup,
            )
        except FloodWait:
            raise
        except Exception as exc:
            logger.info("Source message copy failed; trying saved fallback: %s", exc)
    return await send_fallback(client, chat_id, item, reply_markup=reply_markup)


async def send_sequence(
    client: Client,
    chat_id: int,
    items: Sequence[Dict[str, Any]],
    reply_markup: Optional[Any] = None,
) -> int:
    if not items:
        items = DEFAULT_START_CONTENT
    sent_count = 0
    for index, item in enumerate(items):
        markup = reply_markup if index == len(items) - 1 else None
        try:
            await send_content_item(client, chat_id, item, reply_markup=markup)
            sent_count += 1
        except FloodWait as exc:
            await asyncio.sleep(max(1, int(getattr(exc, "value", 1))) + 1)
            try:
                await send_content_item(client, chat_id, item, reply_markup=markup)
                sent_count += 1
            except Exception as retry_exc:
                logger.warning("Could not send content item to %s: %s", chat_id, retry_exc)
        except Exception as exc:
            logger.warning("Could not send content item to %s: %s", chat_id, exc)
        if index + 1 < len(items):
            await asyncio.sleep(0.12)
    return sent_count


# ---------------------------------------------------------------------------
# Inline keyboards and admin screens
# ---------------------------------------------------------------------------
def user_home_keyboard() -> Optional[ReplyKeyboardMarkup]:
    """Member buttons appear in Telegram's persistent menu keyboard, not inline."""
    buttons = get_buttons()
    if not buttons:
        return None
    rows: List[List[KeyboardButton]] = []
    row: List[KeyboardButton] = []
    for button in buttons:
        row.append(KeyboardButton(str(button["label"])[:64]))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return ReplyKeyboardMarkup(
        rows,
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🏠 پیام شروع اعضا", callback_data="start_menu")],
            [InlineKeyboardButton("🔘 دکمه‌ها و پاسخ‌ها", callback_data="buttons_menu")],
            [InlineKeyboardButton("📢 ارسال همگانی چندپیامه", callback_data="broadcast")],
            [InlineKeyboardButton("🔗 جوین اجباری", callback_data="force_menu")],
            [InlineKeyboardButton("👮 مدیران", callback_data="admins_menu"), InlineKeyboardButton("📊 آمار", callback_data="stats")],
        ]
    )


def start_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✏️ ساخت/ویرایش پیام شروع", callback_data="start_edit")],
            [InlineKeyboardButton("👁 پیش‌نمایش برای اعضا", callback_data="start_preview")],
            [InlineKeyboardButton("♻️ بازگردانی متن پیش‌فرض", callback_data="start_reset")],
            [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")],
        ]
    )


def buttons_menu_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for button in get_buttons():
        label = str(button["label"])[:24]
        icon = "🔗" if button["kind"] == "url" else "💬"
        rows.append(
            [
                InlineKeyboardButton(f"{icon} {label}", callback_data=f"edit:{button['id']}"),
                InlineKeyboardButton("🗑", callback_data=f"del:{button['id']}"),
            ]
        )
    if len(get_buttons()) < MAX_USER_BUTTONS:
        rows.append([InlineKeyboardButton("➕ افزودن دکمه", callback_data="button_add")])
    rows.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")])
    return InlineKeyboardMarkup(rows)


def button_edit_keyboard(button_id: int, kind: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("✏️ تغییر متن دکمه", callback_data=f"rename:{button_id}")],
    ]
    if kind == "url":
        rows.append([InlineKeyboardButton("🔗 تغییر لینک", callback_data=f"editurl:{button_id}")])
    else:
        rows.append([InlineKeyboardButton("📝 تغییر پیام/فایل پاسخ", callback_data=f"editcontent:{button_id}")])
    rows.append([InlineKeyboardButton("🔙 فهرست دکمه‌ها", callback_data="buttons_menu")])
    return InlineKeyboardMarkup(rows)


def force_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ افزودن کانال/گروه", callback_data="force_add"), InlineKeyboardButton("➖ حذف", callback_data="force_remove")],
            [InlineKeyboardButton("🗑 حذف همه", callback_data="force_clear")],
            [InlineKeyboardButton("✏️ ویرایش متن جوین", callback_data="force_text")],
            [InlineKeyboardButton("🔘 دکمه‌های عضویت/بررسی", callback_data="force_labels")],
            [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")],
        ]
    )


def admins_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ افزودن مدیر", callback_data="admin_add"), InlineKeyboardButton("➖ حذف مدیر", callback_data="admin_remove")],
            [InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")],
        ]
    )


def wizard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✅ اتمام", callback_data="wizard_done"), InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]
    )


def button_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("💬 پاسخ متنی/فایل", callback_data="button_type_msg"), InlineKeyboardButton("🔗 لینک", callback_data="button_type_url")]]
    )


def broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🚀 ارسال برای همه", callback_data="broadcast_confirm"), InlineKeyboardButton("❌ لغو", callback_data="broadcast_cancel")]]
    )


async def send_admin_panel(client: Client, chat_id: int) -> None:
    await client.send_message(chat_id, "⚙️ پنل مدیریت", reply_markup=admin_panel_keyboard())


async def send_welcome(client: Client, chat_id: int) -> None:
    await send_sequence(client, chat_id, get_start_content(), reply_markup=user_home_keyboard())


async def open_collection_prompt(
    client: Client,
    chat_id: int,
    prompt_text: str,
    state: Dict[str, Any],
    markup: Optional[InlineKeyboardMarkup] = None,
) -> Message:
    prompt = await client.send_message(chat_id, prompt_text, reply_markup=markup or wizard_keyboard())
    state["prompt_id"] = prompt.id
    admin_states[chat_id] = state
    return prompt


async def update_state_prompt(
    client: Client,
    user_id: int,
    state: Dict[str, Any],
    text: str,
    markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    prompt_id = state.get("prompt_id")
    if not prompt_id:
        await client.send_message(user_id, text, reply_markup=markup)
        return
    try:
        await client.edit_message_text(user_id, int(prompt_id), text, reply_markup=markup)
    except MessageNotModified:
        pass
    except Exception as exc:
        logger.debug("Prompt update failed for %s: %s", user_id, exc)


async def begin_collection_from_callback(
    client: Client,
    cb: CallbackQuery,
    state_name: str,
    prompt_text: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    state: Dict[str, Any] = {"state": state_name, "items": []}
    if extra:
        state.update(extra)
    await open_collection_prompt(client, cb.from_user.id, prompt_text, state)


# ---------------------------------------------------------------------------
# User commands and callbacks
# ---------------------------------------------------------------------------
@app.on_message(filters.command("start") & filters.private)
async def start_command(client: Client, message: Message) -> None:
    user = message.from_user
    if not user:
        return
    uid = user.id
    if is_admin(uid):
        add_user(user)
        admin_states.pop(uid, None)
        await send_admin_panel(client, uid)
        return
    joined, missing = await check_membership(client, uid)
    if not joined:
        await show_force_join(client, uid, missing)
        return
    add_user(user)
    await send_welcome(client, uid)


@app.on_message(filters.command(["panel", "admin"]) & filters.private)
async def panel_command(client: Client, message: Message) -> None:
    user = message.from_user
    if user and is_admin(user.id):
        admin_states.pop(user.id, None)
        await send_admin_panel(client, user.id)
    elif user:
        await message.reply_text("این دستور فقط برای مدیر ربات است.")


@app.on_message(filters.command("done") & filters.private)
async def done_command(client: Client, message: Message) -> None:
    user = message.from_user
    if not user or not is_admin(user.id):
        return
    state = admin_states.get(user.id, {})
    if str(state.get("state", "")).startswith("collect_"):
        await finish_collection(client, user.id)
    elif state.get("state") == "broadcast_confirm":
        await message.reply_text("پیش‌نمایش ارسال آماده است؛ از دکمهٔ «ارسال برای همه» استفاده کنید.")
    else:
        await message.reply_text("در حال حاضر مرحلهٔ چندپیامه‌ای برای اتمام وجود ندارد.")


@app.on_message(filters.command("cancel") & filters.private)
async def cancel_command(client: Client, message: Message) -> None:
    user = message.from_user
    if not user or not is_admin(user.id):
        return
    state = admin_states.pop(user.id, None)
    if state:
        await update_state_prompt(client, user.id, state, "❌ عملیات لغو شد.", admin_panel_keyboard())
    else:
        await message.reply_text("عملیات فعالی برای لغو وجود ندارد.")


@app.on_callback_query()
async def callback_handler(client: Client, cb: CallbackQuery) -> None:
    uid = cb.from_user.id
    data = cb.data or ""

    # The mandatory-subscription check is available to every user.
    if data == "force_check":
        joined, missing = await check_membership(client, uid)
        if joined:
            await cb.answer("عضویت تأیید شد ✅")
            try:
                await cb.message.delete()
            except Exception:
                pass
            add_user(cb.from_user)
            await send_welcome(client, uid)
        else:
            await cb.answer("هنوز عضویت در همهٔ کانال‌ها تأیید نشده است.", show_alert=True)
            try:
                await cb.message.delete()
            except Exception:
                pass
            await show_force_join(client, uid, missing)
        return

    # Custom member buttons.
    if data.startswith("b:"):
        await cb.answer()
        if not is_admin(uid):
            joined, missing = await check_membership(client, uid)
            if not joined:
                await show_force_join(client, uid, missing)
                return
        try:
            button_id = int(data.split(":", 1)[1])
        except ValueError:
            return
        button = get_button(button_id)
        if not button or button["kind"] != "message":
            return
        try:
            content = json.loads(button["content_json"] or "[]")
        except json.JSONDecodeError:
            content = []
        await send_sequence(client, uid, content, reply_markup=user_home_keyboard())
        return

    if not is_admin(uid):
        await cb.answer()
        return

    await cb.answer()

    # Main admin navigation
    if data == "admin_panel":
        await cb.message.edit_text("⚙️ پنل مدیریت", reply_markup=admin_panel_keyboard())
    elif data == "start_menu":
        content = get_start_content()
        await cb.message.edit_text(
            f"🏠 پیام شروع اعضا\n\nتعداد پیام/فایل ذخیره‌شده: {len(content)}\n"
            "می‌توانید متن، عکس، ویدیو، فایل و انواع پیام دیگر را پشت‌سرهم بفرستید.",
            reply_markup=start_menu_keyboard(),
        )
    elif data == "start_edit":
        await begin_collection_from_callback(
            client,
            cb,
            "collect_start",
            "✏️ محتوای شروع را بفرستید؛ می‌توانید چند متن یا فایل را پشت‌سرهم ارسال کنید.\n"
            "برای ذخیره /done یا دکمهٔ «اتمام» را بزنید؛ برای لغو /cancel.",
        )
    elif data == "start_preview":
        await send_sequence(client, uid, get_start_content(), reply_markup=user_home_keyboard())
    elif data == "start_reset":
        set_setting("start_content", DEFAULT_START_CONTENT)
        await cb.message.edit_text("✅ پیام شروع به متن پیش‌فرض برگشت.", reply_markup=start_menu_keyboard())
    elif data == "buttons_menu":
        buttons = get_buttons()
        details = "\n".join(
            f"• {row['label']} — {'لینک' if row['kind'] == 'url' else 'پاسخ سفارشی'}"
            for row in buttons[:30]
        ) or "هنوز دکمه‌ای ساخته نشده است."
        await cb.message.edit_text(
            f"🔘 دکمه‌های اعضا\n\n{details}\n\n"
            "هر دکمه می‌تواند به لینک برود یا چند پیام/فایل سفارشی بفرستد.",
            reply_markup=buttons_menu_keyboard(),
        )
    elif data == "button_add":
        if len(get_buttons()) >= MAX_USER_BUTTONS:
            await cb.message.reply_text(f"حداکثر {MAX_USER_BUTTONS} دکمه قابل ساخت است.")
            return
        await open_collection_prompt(
            client,
            uid,
            "➕ عنوان دکمه را به‌صورت یک پیام متنی بفرستید (حداکثر 64 نویسه).",
            {"state": "awaiting_button_label"},
            markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
        )
    elif data.startswith("edit:"):
        button_id = int(data.split(":", 1)[1])
        button = get_button(button_id)
        if button:
            kind_text = "پیام/فایل سفارشی" if button["kind"] == "message" else f"لینک: {button['url']}"
            await cb.message.edit_text(
                f"ویرایش دکمه: {button['label']}\nنوع: {kind_text}",
                reply_markup=button_edit_keyboard(button_id, button["kind"]),
            )
    elif data.startswith("del:"):
        button_id = int(data.split(":", 1)[1])
        delete_button(button_id)
        await cb.message.edit_text("✅ دکمه حذف شد.", reply_markup=buttons_menu_keyboard())
    elif data.startswith("rename:"):
        button_id = int(data.split(":", 1)[1])
        if get_button(button_id):
            await open_collection_prompt(
                client,
                uid,
                "✏️ عنوان جدید دکمه را بفرستید.",
                {"state": "awaiting_button_rename", "button_id": button_id},
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
            )
    elif data.startswith("editurl:"):
        button_id = int(data.split(":", 1)[1])
        if get_button(button_id):
            await open_collection_prompt(
                client,
                uid,
                "🔗 لینک جدید را بفرستید؛ فقط http(s) یا tg:// پذیرفته می‌شود.",
                {"state": "awaiting_button_url_edit", "button_id": button_id},
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
            )
    elif data.startswith("editcontent:"):
        button_id = int(data.split(":", 1)[1])
        button = get_button(button_id)
        if button and button["kind"] == "message":
            await begin_collection_from_callback(
                client,
                cb,
                "collect_button_update",
                "📝 پاسخ جدید این دکمه را بفرستید؛ می‌توانید چند متن یا فایل پشت‌سرهم بفرستید.\n"
                "برای ذخیره /done یا دکمهٔ «اتمام» را بزنید.",
                extra={"button_id": button_id},
            )
    elif data == "button_type_msg":
        state = admin_states.get(uid, {})
        if state.get("state") == "awaiting_button_type":
            state.update({"state": "collect_button_content", "items": []})
            await update_state_prompt(
                client,
                uid,
                state,
                "💬 پاسخ دکمه را بفرستید؛ هر تعداد متن/فایل خواستید.\nبرای ذخیره /done یا «اتمام» را بزنید.",
                wizard_keyboard(),
            )
    elif data == "button_type_url":
        state = admin_states.get(uid, {})
        if state.get("state") == "awaiting_button_type":
            state["state"] = "awaiting_button_url"
            await update_state_prompt(
                client,
                uid,
                state,
                "🔗 لینک دکمه را بفرستید؛ فقط http(s) یا tg:// پذیرفته می‌شود.",
                InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
            )
    elif data == "wizard_done":
        await finish_collection(client, uid)
    elif data == "wizard_cancel":
        state = admin_states.pop(uid, None)
        if state:
            await update_state_prompt(client, uid, state, "❌ عملیات لغو شد.", admin_panel_keyboard())
    elif data == "broadcast":
        await begin_collection_from_callback(
            client,
            cb,
            "collect_broadcast",
            "📢 پیام‌های ارسال همگانی را یکی‌یکی بفرستید؛ متن، عکس، ویدیو، فایل و پیام‌های دیگر قابل ارسال‌اند.\n"
            "همهٔ پیام‌ها به‌ترتیب و در یک نوبت برای هر عضو می‌روند.\n"
            "برای دیدن پیش‌نمایش و تأیید، /done یا «اتمام» را بزنید.",
        )
    elif data == "broadcast_confirm":
        state = admin_states.get(uid, {})
        if state.get("state") != "broadcast_confirm":
            await cb.message.reply_text("ارسال آماده‌ای پیدا نشد؛ از پنل دوباره شروع کنید.")
            return
        items = list(state.get("items", []))
        admin_states.pop(uid, None)
        await cb.message.edit_text("⏳ ارسال همگانی شروع شد؛ لطفاً پیام وضعیت را نگه دارید.")
        asyncio.create_task(run_broadcast(client, uid, items, cb.message))
    elif data == "broadcast_cancel":
        state = admin_states.pop(uid, None)
        if state:
            await update_state_prompt(client, uid, state, "❌ ارسال همگانی لغو شد.", admin_panel_keyboard())
    elif data == "force_menu":
        channels = force_channels()
        listing = "\n".join(f"{i}. {channel}" for i, channel in enumerate(channels, 1)) or "هیچ کانالی تنظیم نشده است."
        await cb.message.edit_text(
            f"🔗 عضویت اجباری\n\n{listing}\n\n"
            f"متن پیام: {get_setting('force_text')}",
            reply_markup=force_menu_keyboard(),
        )
    elif data == "force_add":
        await open_collection_prompt(
            client,
            uid,
            "➕ آیدی کانال/گروه را بفرستید؛ نمونه: @channel یا -1001234567890.\n"
            "ربات باید در کانال ادمین باشد.",
            {"state": "awaiting_force_add"},
            markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
        )
    elif data == "force_remove":
        channels = force_channels()
        if not channels:
            await cb.message.reply_text("کانالی برای حذف تنظیم نشده است.")
        else:
            listing = "\n".join(f"{i}. {channel}" for i, channel in enumerate(channels, 1))
            await open_collection_prompt(
                client,
                uid,
                f"➖ شماره یا آیدی کانالی را برای حذف بفرستید:\n{listing}",
                {"state": "awaiting_force_remove"},
                markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
            )
    elif data == "force_clear":
        set_setting("force_channels", [])
        await cb.message.edit_text("✅ فهرست عضویت اجباری پاک شد.", reply_markup=force_menu_keyboard())
    elif data == "force_text":
        await open_collection_prompt(
            client,
            uid,
            "✏️ متن پیام جوین اجباری را بفرستید.",
            {"state": "awaiting_force_text"},
            markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
        )
    elif data == "force_labels":
        await cb.message.edit_text(
            "🔘 متن دکمه‌های جوین اجباری را انتخاب کنید. برای نام کانال، از {title} استفاده کنید.",
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("✏️ متن دکمهٔ عضویت", callback_data="force_join_label")],
                    [InlineKeyboardButton("✏️ متن دکمهٔ بررسی", callback_data="force_check_label")],
                    [InlineKeyboardButton("🔙 بازگشت", callback_data="force_menu")],
                ]
            ),
        )
    elif data == "force_join_label":
        await open_collection_prompt(
            client,
            uid,
            f"✏️ متن دکمهٔ عضویت را بفرستید. می‌توانید از {{title}} استفاده کنید.\nفعلی: {get_setting('join_button_template')}",
            {"state": "awaiting_force_join_label"},
            markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
        )
    elif data == "force_check_label":
        await open_collection_prompt(
            client,
            uid,
            f"✏️ متن دکمهٔ بررسی عضویت را بفرستید.\nفعلی: {get_setting('check_button_text')}",
            {"state": "awaiting_force_check_label"},
            markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
        )
    elif data == "admins_menu":
        rows = conn.execute("SELECT user_id FROM admins ORDER BY user_id").fetchall()
        admins = "\n".join(f"• {row['user_id']}" for row in rows) or "فقط مالک ربات مدیر است."
        await cb.message.edit_text(f"👮 مدیران ربات\n\nمالک: {OWNER_ID}\n{admins}", reply_markup=admins_menu_keyboard())
    elif data == "admin_add":
        await open_collection_prompt(
            client,
            uid,
            "➕ آیدی عددی مدیر جدید را بفرستید.",
            {"state": "awaiting_admin_add"},
            markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
        )
    elif data == "admin_remove":
        await open_collection_prompt(
            client,
            uid,
            "➖ آیدی عددی مدیری را که می‌خواهید حذف کنید بفرستید.",
            {"state": "awaiting_admin_remove"},
            markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="wizard_cancel")]]),
        )
    elif data == "stats":
        user_count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        button_count = conn.execute("SELECT COUNT(*) AS n FROM user_buttons").fetchone()["n"]
        await cb.message.edit_text(
            f"📊 آمار ربات\n\n👥 اعضای ثبت‌شده: {user_count}\n"
            f"🔘 دکمه‌های ساخته‌شده: {button_count}\n"
            f"🔗 کانال‌های جوین اجباری: {len(force_channels())}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 پنل مدیریت", callback_data="admin_panel")]]),
        )


# ---------------------------------------------------------------------------
# Admin message workflows
# ---------------------------------------------------------------------------
async def finish_collection(client: Client, user_id: int) -> None:
    state = admin_states.get(user_id)
    if not state:
        return
    state_name = state.get("state")
    items = state.get("items", [])

    if state_name in ("collect_start", "collect_button_content", "collect_button_update", "collect_broadcast") and not items:
        await update_state_prompt(client, user_id, state, "هنوز پیامی دریافت نشده؛ ابتدا محتوا بفرستید.", wizard_keyboard())
        return

    if state_name == "collect_start":
        set_setting("start_content", items)
        admin_states.pop(user_id, None)
        await update_state_prompt(client, user_id, state, f"✅ پیام شروع با {len(items)} پیام/فایل ذخیره شد.", start_menu_keyboard())
    elif state_name == "collect_button_content":
        button_id = save_button(state["label"], "message", content=items)
        admin_states.pop(user_id, None)
        await update_state_prompt(client, user_id, state, f"✅ دکمهٔ «{state['label']}» ساخته شد (شناسهٔ {button_id}).", buttons_menu_keyboard())
    elif state_name == "collect_button_update":
        button_id = int(state["button_id"])
        update_button_fields(button_id, content_json=json.dumps(items, ensure_ascii=False))
        admin_states.pop(user_id, None)
        await update_state_prompt(client, user_id, state, "✅ پاسخ دکمه به‌روزرسانی شد.", buttons_menu_keyboard())
    elif state_name == "collect_broadcast":
        state["state"] = "broadcast_confirm"
        admin_states[user_id] = state
        user_count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        await update_state_prompt(
            client,
            user_id,
            state,
            f"📢 پیش‌نمایش آماده است.\n\nتعداد پیام‌ها: {len(items)}\nگیرندگان فعلی: {user_count}\n"
            "با تأیید، همهٔ پیام‌ها به‌ترتیب برای هر عضو ارسال می‌شوند.",
            broadcast_confirm_keyboard(),
        )


def find_member_button(label: str) -> Optional[sqlite3.Row]:
    normalized = (label or "").strip().casefold()
    for button in get_buttons():
        if str(button["label"]).strip().casefold() == normalized:
            return button
    return None


def is_member_menu_text(text: str) -> bool:
    normalized = (text or "").strip().casefold()
    check_label = (get_setting("check_button_text") or "✅ بررسی عضویت").strip().casefold()
    return normalized == check_label or find_member_button(normalized) is not None


async def handle_member_menu_message(client: Client, message: Message) -> None:
    user = message.from_user
    text = (message.text or "").strip()
    if not user or not text:
        return
    uid = user.id
    check_label = (get_setting("check_button_text") or "✅ بررسی عضویت").strip().casefold()
    button = find_member_button(text)
    is_check = text.casefold() == check_label

    # Members must pass the forced-join check before receiving any custom response.
    if not is_admin(uid):
        joined, missing = await check_membership(client, uid)
        if not joined:
            await show_force_join(client, uid, missing)
            return
        add_user(user)

    if is_check:
        await message.reply_text("عضویت تأیید شد ✅", reply_markup=user_home_keyboard())
        await send_welcome(client, uid)
        return

    if button:
        if button["kind"] == "url":
            # Reply-keyboard buttons cannot open URLs directly, so send the clickable URL as a message.
            await client.send_message(uid, str(button["url"]), reply_markup=user_home_keyboard())
            return
        try:
            content = json.loads(button["content_json"] or "[]")
        except json.JSONDecodeError:
            content = []
        await send_sequence(client, uid, content, reply_markup=user_home_keyboard())
        return

    if not is_admin(uid):
        await message.reply_text("لطفاً یکی از گزینه‌های منوی پایین را انتخاب کنید.", reply_markup=user_home_keyboard())


@app.on_message(
    filters.private
    & ~filters.command(["start", "panel", "admin", "done", "cancel"])
)
async def admin_workflow_message(client: Client, message: Message) -> None:
    user = message.from_user
    if not user:
        return
    uid = user.id
    state = admin_states.get(uid)
    if not is_admin(uid):
        await handle_member_menu_message(client, message)
        return
    if not state:
        if message.text and is_member_menu_text(message.text):
            await handle_member_menu_message(client, message)
        return

    state_name = state.get("state")
    text = message.text or ""
    stripped = text.strip()

    if state_name in ("collect_start", "collect_button_content", "collect_button_update", "collect_broadcast"):
        if len(state.get("items", [])) >= MAX_SEQUENCE_ITEMS:
            await message.reply_text(f"حداکثر {MAX_SEQUENCE_ITEMS} پیام در هر مجموعه پذیرفته می‌شود؛ برای ذخیره /done را بفرستید.")
            return
        item = make_content_item(message)
        if not item:
            await message.reply_text("این پیام محتوا ندارد؛ لطفاً متن، فایل یا رسانه بفرستید.")
            return
        state.setdefault("items", []).append(item)
        admin_states[uid] = state
        names = {
            "collect_start": "محتوای شروع",
            "collect_button_content": "پاسخ دکمه",
            "collect_button_update": "پاسخ دکمه",
            "collect_broadcast": "پیام ارسال همگانی",
        }
        await update_state_prompt(
            client,
            uid,
            state,
            f"✅ {len(state['items'])} {names.get(state_name, 'پیام')} دریافت شد.\n"
            "ادامه بدهید یا /done و دکمهٔ «اتمام» را بزنید.",
            wizard_keyboard(),
        )
        return

    if state_name == "awaiting_button_label":
        label = stripped
        if not label:
            await message.reply_text("عنوان دکمه نباید خالی باشد.")
            return
        if len(label) > 64:
            await message.reply_text("عنوان دکمه حداکثر 64 نویسه باشد.")
            return
        check_label = (get_setting("check_button_text") or "✅ بررسی عضویت").strip().casefold()
        if label.casefold() == check_label or any(str(row["label"]).casefold() == label.casefold() for row in get_buttons()):
            await message.reply_text("این عنوان قبلاً برای دکمه‌ای استفاده شده؛ عنوان یکتایی بفرستید.")
            return
        state["label"] = label
        state["state"] = "awaiting_button_type"
        admin_states[uid] = state
        await update_state_prompt(client, uid, state, "نوع دکمه را انتخاب کنید:", button_type_keyboard())
        return

    if state_name == "awaiting_button_type":
        await message.reply_text("لطفاً نوع را با دکمه‌های زیر انتخاب کنید.")
        return

    if state_name in ("awaiting_button_url", "awaiting_button_url_edit"):
        url = stripped
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https", "tg") or (parsed.scheme in ("http", "https") and not parsed.netloc):
            await message.reply_text("لینک معتبر نیست؛ با https://، http:// یا tg:// شروع شود.")
            return
        if state_name == "awaiting_button_url":
            button_id = save_button(state["label"], "url", url=url)
            result = f"✅ دکمهٔ لینک «{state['label']}» ساخته شد (شناسهٔ {button_id})."
        else:
            update_button_fields(int(state["button_id"]), url=url)
            result = "✅ لینک دکمه به‌روزرسانی شد."
        admin_states.pop(uid, None)
        await update_state_prompt(client, uid, state, result, buttons_menu_keyboard())
        return

    if state_name == "awaiting_button_rename":
        label = stripped
        if not label or len(label) > 64:
            await message.reply_text("عنوان باید بین 1 تا 64 نویسه باشد.")
            return
        button_id = int(state["button_id"])
        check_label = (get_setting("check_button_text") or "✅ بررسی عضویت").strip().casefold()
        if label.casefold() == check_label or any(
            int(row["id"]) != button_id and str(row["label"]).casefold() == label.casefold()
            for row in get_buttons()
        ):
            await message.reply_text("این عنوان برای دکمه‌ای دیگر استفاده شده؛ عنوان یکتایی بفرستید.")
            return
        update_button_fields(button_id, label=label)
        admin_states.pop(uid, None)
        await update_state_prompt(client, uid, state, "✅ عنوان دکمه به‌روزرسانی شد.", buttons_menu_keyboard())
        return

    if state_name == "awaiting_force_add":
        try:
            channel = normalize_channel(stripped)
            channels = force_channels()
            if channel in channels:
                await message.reply_text("این کانال از قبل در فهرست است.")
                return
            if len(channels) >= 20:
                await message.reply_text("حداکثر 20 کانال برای جوین اجباری قابل تنظیم است.")
                return
            chat_id = parse_chat_id(channel)
            member = await client.get_chat_member(chat_id, "me")
            if member.status not in (enums.ChatMemberStatus.ADMINISTRATOR, enums.ChatMemberStatus.OWNER):
                await message.reply_text("ربات در این کانال/گروه ادمین نیست؛ ابتدا دسترسی ادمین بدهید.")
                return
            channels.append(channel)
            set_setting("force_channels", channels)
            admin_states.pop(uid, None)
            await update_state_prompt(client, uid, state, f"✅ {channel} به جوین اجباری اضافه شد.", force_menu_keyboard())
        except UserNotParticipant:
            await message.reply_text("ربات عضو این کانال/گروه نیست.")
        except ValueError as exc:
            await message.reply_text(str(exc))
        except Exception as exc:
            await message.reply_text(f"خطا در بررسی کانال: {exc}")
        return

    if state_name == "awaiting_force_remove":
        channels = force_channels()
        target = stripped
        if target.isdigit() and 1 <= int(target) <= len(channels):
            removed = channels.pop(int(target) - 1)
        else:
            try:
                normalized = normalize_channel(target)
            except ValueError:
                normalized = target
            if normalized not in channels:
                await message.reply_text("این کانال در فهرست پیدا نشد؛ شماره یا آیدی دقیق را بفرستید.")
                return
            removed = normalized
            channels.remove(normalized)
        set_setting("force_channels", channels)
        admin_states.pop(uid, None)
        await update_state_prompt(client, uid, state, f"✅ {removed} حذف شد.", force_menu_keyboard())
        return

    if state_name == "awaiting_force_text":
        if not text:
            await message.reply_text("لطفاً متن را به‌صورت پیام متنی بفرستید.")
            return
        set_setting("force_text", text)
        admin_states.pop(uid, None)
        await update_state_prompt(client, uid, state, "✅ متن جوین اجباری ذخیره شد.", force_menu_keyboard())
        return

    if state_name in ("awaiting_force_join_label", "awaiting_force_check_label"):
        label = stripped
        if not label or len(label) > 64:
            await message.reply_text("متن دکمه باید بین 1 تا 64 نویسه باشد.")
            return
        key = "join_button_template" if state_name == "awaiting_force_join_label" else "check_button_text"
        if key == "check_button_text" and any(
            str(row["label"]).casefold() == label.casefold() for row in get_buttons()
        ):
            await message.reply_text("متن دکمهٔ بررسی نباید با عنوان یک دکمهٔ منو یکسان باشد.")
            return
        set_setting(key, label)
        admin_states.pop(uid, None)
        await update_state_prompt(client, uid, state, "✅ متن دکمه ذخیره شد.", force_menu_keyboard())
        return

    if state_name in ("awaiting_admin_add", "awaiting_admin_remove"):
        if not stripped.isdigit():
            await message.reply_text("آیدی عددی معتبر بفرستید.")
            return
        target_id = int(stripped)
        if state_name == "awaiting_admin_add":
            conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (target_id,))
            message_text = f"✅ مدیر {target_id} اضافه شد."
        else:
            if target_id == OWNER_ID:
                await message.reply_text("مالک اصلی قابل حذف نیست.")
                return
            conn.execute("DELETE FROM admins WHERE user_id=?", (target_id,))
            message_text = f"✅ مدیر {target_id} حذف شد."
        conn.commit()
        admin_states.pop(uid, None)
        await update_state_prompt(client, uid, state, message_text, admins_menu_keyboard())
        return


# ---------------------------------------------------------------------------
# Multi-message broadcast
# ---------------------------------------------------------------------------
BLOCKED_ERROR_NAMES = {
    "UserIsBlocked",
    "InputUserDeactivated",
    "UserDeactivated",
    "UserDeactivatedBan",
    "PeerIdInvalid",
    "ChatWriteForbidden",
    "Forbidden",
}


def is_permanently_unreachable(exc: Exception) -> bool:
    return exc.__class__.__name__ in BLOCKED_ERROR_NAMES


def progress_bar(percent: int) -> str:
    filled = max(0, min(10, percent // 10))
    return "█" * filled + "░" * (10 - filled)


async def run_broadcast(
    client: Client,
    admin_id: int,
    items: Sequence[Dict[str, Any]],
    status_message: Message,
) -> None:
    user_ids = [int(row["user_id"]) for row in conn.execute("SELECT user_id FROM users ORDER BY user_id").fetchall()]
    total = len(user_ids)
    if not total:
        await status_message.edit_text("❌ هنوز عضوی برای ارسال همگانی ثبت نشده است.", reply_markup=admin_panel_keyboard())
        return

    success = 0
    failed = 0
    removed = 0
    processed = 0
    last_percent = -5
    started_at = time.monotonic()
    await status_message.edit_text(
        f"⏳ ارسال {len(items)} پیام برای {total} عضو شروع شد.\n"
        "هر بسته به‌ترتیب برای هر عضو فرستاده می‌شود."
    )

    for user_id in user_ids:
        user_ok = True
        for item in items:
            try:
                await send_content_item(client, user_id, item)
            except FloodWait as exc:
                wait_seconds = max(1, int(getattr(exc, "value", 1)))
                logger.warning("FloodWait while broadcasting; sleeping %s seconds", wait_seconds)
                await asyncio.sleep(wait_seconds + 1)
                try:
                    await send_content_item(client, user_id, item)
                except Exception as retry_exc:
                    user_ok = False
                    if is_permanently_unreachable(retry_exc):
                        conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
                        removed += 1
                    logger.info("Broadcast retry failed for %s: %s", user_id, retry_exc)
                    break
            except Exception as exc:
                user_ok = False
                if is_permanently_unreachable(exc):
                    conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
                    removed += 1
                logger.info("Broadcast failed for %s: %s", user_id, exc)
                break
            # Conservative global send rate; Telegram limits apply across the bot, not per recipient.
            await asyncio.sleep(0.045)

        if user_ok:
            success += 1
        else:
            failed += 1
        processed += 1

        percent = int(processed * 100 / total)
        if percent >= last_percent + 5 or processed == total:
            last_percent = (percent // 5) * 5
            elapsed = int(time.monotonic() - started_at)
            rate = processed / max(1, elapsed)
            eta = int((total - processed) / rate) if rate > 0 else 0
            status = (
                f"📢 در حال ارسال همگانی\n\n[{progress_bar(percent)}] {percent}%\n\n"
                f"👥 کل: {total}\n🔄 بررسی‌شده: {processed}\n"
                f"✅ موفق: {success}\n❌ ناموفق: {failed}\n"
                f"🗑 حذف‌شده (مسدود/غیرفعال): {removed}\n"
                f"⏱ زمان سپری‌شده: {elapsed} ثانیه | زمان تقریبی باقی‌مانده: {eta} ثانیه"
            )
            try:
                await status_message.edit_text(status)
            except MessageNotModified:
                pass
            except FloodWait as exc:
                await asyncio.sleep(max(1, int(getattr(exc, "value", 1))))
            except Exception as exc:
                logger.debug("Could not update broadcast progress: %s", exc)

    conn.commit()
    elapsed = int(time.monotonic() - started_at)
    await status_message.edit_text(
        f"✅ ارسال همگانی تمام شد.\n\n"
        f"📨 تعداد پیام در هر بسته: {len(items)}\n"
        f"👥 گیرندگان اولیه: {total}\n"
        f"✅ ارسال کامل: {success}\n❌ ناموفق/ناقص: {failed}\n"
        f"🗑 حذف‌شده از فهرست اعضا: {removed}\n"
        f"⏱ مدت: {elapsed} ثانیه",
        reply_markup=admin_panel_keyboard(),
    )


BACKUP_INTERVAL_SECONDS = 24 * 60 * 60


def create_database_snapshot(target_path: Path) -> None:
    """Create a consistent SQLite snapshot, including any active WAL changes."""
    source = sqlite3.connect(DB_PATH, timeout=30)
    destination = sqlite3.connect(target_path, timeout=30)
    try:
        source.backup(destination)
        destination.commit()
    finally:
        destination.close()
        source.close()


async def send_owner_backup(client: Client) -> None:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-UTC")
    backup_path = DATA_DIR / f"custom-bot-backup-{timestamp}.sqlite3"
    try:
        await asyncio.to_thread(create_database_snapshot, backup_path)
        await client.send_document(
            chat_id=OWNER_ID,
            document=str(backup_path),
            caption=f"🗄 بکاپ دیتابیس ربات | {timestamp}",
        )
        logger.info("Daily database backup sent to owner %s", OWNER_ID)
    finally:
        try:
            backup_path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not remove temporary backup %s: %s", backup_path, exc)


async def periodic_owner_backup(client: Client) -> None:
    """Send an initial backup, then repeat every 24 hours after each successful send."""
    while True:
        try:
            await send_owner_backup(client)
            await asyncio.sleep(BACKUP_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except FloodWait as exc:
            wait_seconds = max(1, int(getattr(exc, "value", 1)))
            logger.warning("FloodWait while sending owner backup; retrying in %s seconds", wait_seconds)
            await asyncio.sleep(wait_seconds + 1)
        except Exception as exc:
            # A Telegram bot cannot start a private conversation. If the owner has not
            # pressed /start yet, retry hourly rather than waiting a full day.
            logger.warning("Owner backup failed; will retry in one hour: %s", exc)
            await asyncio.sleep(60 * 60)


async def main() -> None:
    logger.info("Connecting Telegram bot...")
    await app.start()
    backup_task = None
    try:
        me = await app.get_me()
        logger.info("Bot ready: @%s | local database: %s", me.username or "unknown", DB_PATH)
        backup_task = asyncio.create_task(periodic_owner_backup(app), name="daily-owner-db-backup")
        await idle()
    finally:
        if backup_task:
            backup_task.cancel()
            await asyncio.gather(backup_task, return_exceptions=True)
        try:
            await app.stop()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
