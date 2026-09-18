import logging
import os
import sqlite3
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Настройки ----------
# Токен бота (получить у @BotFather) и список ID администраторов
# берутся из переменных окружения, чтобы не хранить их в коде.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

# Путь к файлу базы данных. Если задана переменная окружения DB_PATH (например,
# указывающая на подключённый Railway Volume — постоянное хранилище), используется
# она. Иначе база хранится рядом с bot.py, но тогда она стирается при каждом
# передеплое, если диск не подключён.
DB_PATH = os.environ.get(
    "DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "broker_bot.db"),
)


# ---------- Работа с базой данных ----------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            msg_type TEXT NOT NULL,
            text TEXT,
            file_id TEXT,
            caption TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            last_seen_id INTEGER NOT NULL DEFAULT 0,
            username TEXT
        )
        """
    )
    # Миграция: добавляем новые колонки, если их ещё нет — нужно для баз,
    # созданных до появления отслеживания активности пользователей.
    for column, col_type in [
        ("full_name", "TEXT"),
        ("first_seen", "TEXT"),
        ("last_start", "TEXT"),
        ("last_request", "TEXT"),
    ]:
        try:
            cur.execute(f"ALTER TABLE users ADD COLUMN {column} {col_type}")
        except sqlite3.OperationalError:
            pass  # колонка уже существует — ничего делать не нужно

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sent_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            telegram_message_id INTEGER NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def save_message(msg_type, text=None, file_id=None, caption=None):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO messages (msg_type, text, file_id, caption, created_at) VALUES (?, ?, ?, ?, ?)",
        (msg_type, text, file_id, caption, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


def register_user(user_id, username, full_name):
    conn = get_conn()
    cur = conn.cursor()
    now = datetime.utcnow().isoformat()
    cur.execute(
        "INSERT OR IGNORE INTO users (user_id, last_seen_id, username, full_name, first_seen) "
        "VALUES (?, 0, ?, ?, ?)",
        (user_id, username, full_name, now),
    )
    # если пользователь уже был в базе — обновляем его имя/username на случай изменений
    cur.execute(
        "UPDATE users SET username = ?, full_name = ? WHERE user_id = ?",
        (username, full_name, user_id),
    )
    conn.commit()
    conn.close()


def touch_start(user_id):
    """Отмечает момент, когда пользователь нажал /start."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_start = ? WHERE user_id = ?",
        (datetime.utcnow().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def touch_request(user_id):
    """Отмечает момент, когда пользователь запросил сообщения через кнопку."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE users SET last_request = ? WHERE user_id = ?",
        (datetime.utcnow().isoformat(), user_id),
    )
    conn.commit()
    conn.close()


def get_all_users():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users ORDER BY COALESCE(last_start, first_seen) DESC")
    rows = cur.fetchall()
    conn.close()
    return rows


def format_dt(iso_str):
    if not iso_str:
        return "ещё не было"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime("%d.%m.%Y %H:%M UTC")
    except ValueError:
        return iso_str


def get_last_seen(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT last_seen_id FROM users WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row["last_seen_id"] if row else 0


def set_last_seen(user_id, msg_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE users SET last_seen_id = ? WHERE user_id = ?", (msg_id, user_id))
    conn.commit()
    conn.close()


def get_new_messages(after_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM messages WHERE id > ? ORDER BY id ASC", (after_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_messages():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM messages ORDER BY id ASC")
    rows = cur.fetchall()
    conn.close()
    return rows


def record_sent_message(user_id, telegram_message_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sent_messages (user_id, telegram_message_id) VALUES (?, ?)",
        (user_id, telegram_message_id),
    )
    conn.commit()
    conn.close()


def get_all_sent_messages():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT * FROM sent_messages")
    rows = cur.fetchall()
    conn.close()
    return rows


def clear_sent_messages():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM sent_messages")
    conn.commit()
    conn.close()


# ---------- Клавиатура ----------
MAIN_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("📥 Получить новые сообщения", callback_data="get_new")],
        [InlineKeyboardButton("📚 Получить все сообщения", callback_data="get_all")],
    ]
)


# ---------- Хендлеры ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    register_user(user.id, user.username or "", user.full_name)
    touch_start(user.id)
    is_admin = user.id in ADMIN_IDS

    if is_admin:
        text = (
            "Привет! Вы администратор.\n\n"
            "Просто отправляйте мне текст, документы, фото, видео, аудио или "
            "голосовые — я сохраню их, и пользователи смогут забрать их по кнопке."
        )
        await update.message.reply_text(text)
    else:
        text = "Привет! Нажмите кнопку ниже, чтобы получить материалы от администратора."
        await update.message.reply_text(text, reply_markup=MAIN_KEYBOARD)


async def clear_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /clear — доступна только админу.
    Удаляет все сохранённые сообщения/файлы из базы бота, а также пытается
    удалить их из чатов пользователей, которым они уже были отправлены.
    """
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return  # обычные пользователи не должны даже знать об этой команде

    await update.message.reply_text("🧹 Начинаю очистку, это может занять немного времени...")

    sent_rows = get_all_sent_messages()
    deleted = 0
    failed = 0
    for row in sent_rows:
        try:
            await context.bot.delete_message(
                chat_id=row["user_id"], message_id=row["telegram_message_id"]
            )
            deleted += 1
        except Exception:
            # Сообщение могло быть уже удалено пользователем вручную,
            # либо Telegram не разрешает удалить слишком старое сообщение.
            failed += 1

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM messages")
    conn.commit()
    conn.close()
    clear_sent_messages()

    report = (
        "🗑 Готово!\n"
        "Все сохранённые сообщения и файлы удалены из бота.\n"
        f"Удалено сообщений у пользователей: {deleted}"
    )
    if failed:
        report += f"\nНе удалось удалить: {failed} (возможно, слишком старые или уже удалены вручную)"

    await update.message.reply_text(report)


async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /users — доступна только админу. Показывает всех, кто пользовался ботом."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        return

    rows = get_all_users()
    if not rows:
        await update.message.reply_text("Пока никто не заходил в бота.")
        return

    blocks = [f"👥 Всего пользователей: {len(rows)}"]
    for row in rows:
        name = row["full_name"] or "Без имени"
        username = f"@{row['username']}" if row["username"] else "без username"
        blocks.append(
            f"• {name} ({username}, id: {row['user_id']})\n"
            f"   Первый заход: {format_dt(row['first_seen'])}\n"
            f"   Последний /start: {format_dt(row['last_start'])}\n"
            f"   Последний запрос сообщений: {format_dt(row['last_request'])}"
        )

    text = "\n\n".join(blocks)
    # Telegram ограничивает сообщение 4096 символами — при необходимости делим на части
    for i in range(0, len(text), 3500):
        await update.message.reply_text(text[i : i + 3500])


async def admin_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит любой контент от админа и сохраняет его. Остальным подсказывает про кнопки."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text(
            "Отправка контента доступна только администратору.\n"
            "Используйте кнопки ниже, чтобы получить сообщения:",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    msg = update.message

    if msg.text:
        save_message("text", text=msg.text)
    elif msg.document:
        save_message("document", file_id=msg.document.file_id, caption=msg.caption)
    elif msg.photo:
        save_message("photo", file_id=msg.photo[-1].file_id, caption=msg.caption)
    elif msg.video:
        save_message("video", file_id=msg.video.file_id, caption=msg.caption)
    elif msg.audio:
        save_message("audio", file_id=msg.audio.file_id, caption=msg.caption)
    elif msg.voice:
        save_message("voice", file_id=msg.voice.file_id, caption=msg.caption)
    elif msg.animation:
        save_message("animation", file_id=msg.animation.file_id, caption=msg.caption)
    else:
        await msg.reply_text("Этот тип контента пока не поддерживается ботом.")
        return

    await msg.reply_text("✅ Сохранено. Теперь это доступно пользователям через кнопку.")


async def send_message_row(context, chat_id, row):
    msg_type = row["msg_type"]
    sent = None
    if msg_type == "text":
        sent = await context.bot.send_message(chat_id=chat_id, text=row["text"])
    elif msg_type == "document":
        sent = await context.bot.send_document(chat_id=chat_id, document=row["file_id"], caption=row["caption"])
    elif msg_type == "photo":
        sent = await context.bot.send_photo(chat_id=chat_id, photo=row["file_id"], caption=row["caption"])
    elif msg_type == "video":
        sent = await context.bot.send_video(chat_id=chat_id, video=row["file_id"], caption=row["caption"])
    elif msg_type == "audio":
        sent = await context.bot.send_audio(chat_id=chat_id, audio=row["file_id"], caption=row["caption"])
    elif msg_type == "voice":
        sent = await context.bot.send_voice(chat_id=chat_id, voice=row["file_id"], caption=row["caption"])
    elif msg_type == "animation":
        sent = await context.bot.send_animation(chat_id=chat_id, animation=row["file_id"], caption=row["caption"])

    if sent is not None:
        record_sent_message(chat_id, sent.message_id)


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    register_user(user.id, user.username or "", user.full_name)
    touch_request(user.id)

    if query.data == "get_new":
        last_seen = get_last_seen(user.id)
        rows = get_new_messages(last_seen)
        if not rows:
            await query.message.reply_text("Новых сообщений нет.", reply_markup=MAIN_KEYBOARD)
            return
        for row in rows:
            await send_message_row(context, user.id, row)
        set_last_seen(user.id, rows[-1]["id"])
        await query.message.reply_text(
            f"Готово! Отправлено сообщений: {len(rows)}", reply_markup=MAIN_KEYBOARD
        )

    elif query.data == "get_all":
        rows = get_all_messages()
        if not rows:
            await query.message.reply_text("Сообщений пока нет.", reply_markup=MAIN_KEYBOARD)
            return
        for row in rows:
            await send_message_row(context, user.id, row)
        set_last_seen(user.id, rows[-1]["id"])
        await query.message.reply_text(
            f"Готово! Отправлено сообщений: {len(rows)}", reply_markup=MAIN_KEYBOARD
        )


def main():
    init_db()
    if BOT_TOKEN == "PUT_YOUR_TOKEN_HERE":
        raise RuntimeError(
            "Не задан токен бота. Установите переменную окружения BOT_TOKEN."
        )
    if not ADMIN_IDS:
        logger.warning(
            "Переменная ADMIN_IDS пуста — никто не сможет отправлять контент как администратор."
        )

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("clear", clear_history))
    app.add_handler(CommandHandler("users", list_users))
    app.add_handler(CallbackQueryHandler(button_handler))
    # Ловим любой не-командный контент (текст, файлы, фото и т.д.)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, admin_content_handler))

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
