import asyncio
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =====================================================================
# Настройки (задаются на Railway во вкладке Variables)
# =====================================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

# Если больше 0 — бот сам удаляет всё, что отправил пользователям, спустя
# столько часов (максимум 46, потому что Telegram не даёт удалять
# сообщения старше 48 часов). 0 = автоудаление выключено.
AUTO_DELETE_HOURS = min(float(os.environ.get("AUTO_DELETE_HOURS", "0") or 0), 46)

# Telegram разрешает боту удалять сообщения только младше 48 часов.
# Берём запас в 1 час.
SAFE_WINDOW = 47 * 3600

# =====================================================================
# Где хранится база данных
# =====================================================================
# Railway сам задаёт RAILWAY_VOLUME_MOUNT_PATH, когда к сервису подключён
# Volume (постоянный диск). Тогда база лежит на нём и переживает любые
# перезапуски и обновления кода.
_volume = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
if _volume:
    DB_PATH = os.path.join(_volume, "broker_bot.db")
    PERSISTENT = True
elif os.environ.get("DB_PATH"):
    DB_PATH = os.environ["DB_PATH"]
    PERSISTENT = True
else:
    DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "broker_bot.db")
    PERSISTENT = False  # данные будут стираться при каждом перезапуске!

_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                msg_type TEXT NOT NULL,
                text TEXT,
                file_id TEXT,
                caption TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                last_seen_id INTEGER NOT NULL DEFAULT 0,
                first_seen REAL,
                last_start REAL,
                last_request REAL,
                blocked INTEGER NOT NULL DEFAULT 0,
                banned INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        try:  # для баз, созданных до появления блокировки пользователей
            c.execute("ALTER TABLE users ADD COLUMN banned INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Журнал всего, что бот отправил пользователям: нужен, чтобы потом
        # можно было удалить эти сообщения из чатов.
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                telegram_message_id INTEGER NOT NULL,
                sent_at REAL NOT NULL
            )
            """
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_sent_at ON sent_messages(sent_at)")


# =====================================================================
# Работа с данными
# =====================================================================
def save_message(msg_type, text=None, file_id=None, caption=None):
    with db() as c:
        c.execute(
            "INSERT INTO messages (msg_type, text, file_id, caption, created_at) VALUES (?, ?, ?, ?, ?)",
            (msg_type, text, file_id, caption, time.time()),
        )


def register_user(user):
    with db() as c:
        c.execute(
            "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
            (user.id, time.time()),
        )
        c.execute(
            "UPDATE users SET username = ?, full_name = ?, blocked = 0 WHERE user_id = ?",
            (user.username or "", user.full_name or "", user.id),
        )


def touch_user(user_id, field):
    assert field in ("last_start", "last_request")
    with db() as c:
        c.execute(f"UPDATE users SET {field} = ? WHERE user_id = ?", (time.time(), user_id))


def get_last_seen(user_id):
    with db() as c:
        row = c.execute("SELECT last_seen_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return row["last_seen_id"] if row else 0


def set_last_seen(user_id, msg_id):
    with db() as c:
        c.execute("UPDATE users SET last_seen_id = ? WHERE user_id = ?", (msg_id, user_id))


def is_banned(user_id):
    with db() as c:
        row = c.execute("SELECT banned FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return bool(row and row["banned"])


def set_banned(user_id, flag):
    with db() as c:
        if flag:
            # запись создаём, даже если человек ещё ни разу не заходил в бота
            c.execute(
                "INSERT OR IGNORE INTO users (user_id, first_seen) VALUES (?, ?)",
                (user_id, time.time()),
            )
        c.execute("UPDATE users SET banned = ? WHERE user_id = ?", (1 if flag else 0, user_id))


def mark_blocked(user_id):
    with db() as c:
        c.execute("UPDATE users SET blocked = 1 WHERE user_id = ?", (user_id,))


def messages_after(after_id):
    with db() as c:
        return c.execute("SELECT * FROM messages WHERE id > ? ORDER BY id ASC", (after_id,)).fetchall()


def record_sent(user_id, telegram_message_id):
    with db() as c:
        c.execute(
            "INSERT INTO sent_messages (user_id, telegram_message_id, sent_at) VALUES (?, ?, ?)",
            (user_id, telegram_message_id, time.time()),
        )


def fmt_dt(ts):
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def get_stats():
    now = time.time()
    with db() as c:
        stored = c.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        users = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        blocked = c.execute("SELECT COUNT(*) AS n FROM users WHERE blocked = 1").fetchone()["n"]
        banned = c.execute("SELECT COUNT(*) AS n FROM users WHERE banned = 1").fetchone()["n"]
        tracked = c.execute("SELECT COUNT(*) AS n FROM sent_messages").fetchone()["n"]
        deletable = c.execute(
            "SELECT COUNT(*) AS n FROM sent_messages WHERE sent_at > ?", (now - SAFE_WINDOW,)
        ).fetchone()["n"]
        chats = c.execute("SELECT COUNT(DISTINCT user_id) AS n FROM sent_messages").fetchone()["n"]
    return {
        "stored": stored,
        "users": users,
        "blocked": blocked,
        "banned": banned,
        "tracked": tracked,
        "deletable": deletable,
        "chats": chats,
    }


# =====================================================================
# Отправка и удаление
# =====================================================================
BG_TASKS = set()


def spawn(coro):
    """Запускает долгую задачу в фоне, чтобы бот не переставал отвечать."""
    task = asyncio.create_task(coro)
    BG_TASKS.add(task)
    task.add_done_callback(BG_TASKS.discard)


async def send_row(bot, chat_id, row):
    """Отправляет сохранённое сообщение пользователю и запоминает его ID."""
    t = row["msg_type"]
    if t == "text":
        sent = await bot.send_message(chat_id=chat_id, text=row["text"])
    elif t == "document":
        sent = await bot.send_document(chat_id=chat_id, document=row["file_id"], caption=row["caption"])
    elif t == "photo":
        sent = await bot.send_photo(chat_id=chat_id, photo=row["file_id"], caption=row["caption"])
    elif t == "video":
        sent = await bot.send_video(chat_id=chat_id, video=row["file_id"], caption=row["caption"])
    elif t == "audio":
        sent = await bot.send_audio(chat_id=chat_id, audio=row["file_id"], caption=row["caption"])
    elif t == "voice":
        sent = await bot.send_voice(chat_id=chat_id, voice=row["file_id"], caption=row["caption"])
    elif t == "animation":
        sent = await bot.send_animation(chat_id=chat_id, animation=row["file_id"], caption=row["caption"])
    else:
        return None
    record_sent(chat_id, sent.message_id)
    return sent


async def say(bot, chat_id, text, reply_markup=None):
    """Отправляет служебное сообщение пользователю и тоже запоминает его."""
    sent = await bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)
    record_sent(chat_id, sent.message_id)
    return sent


async def deliver(bot, user_id, rows):
    """Отправляет пользователю список сообщений. Возвращает (отправлено, доступен_ли_чат)."""
    sent = 0
    for row in rows:
        for attempt in range(2):
            try:
                await send_row(bot, user_id, row)
                sent += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Forbidden:
                mark_blocked(user_id)
                return sent, False
            except TelegramError as e:
                logger.warning("Не удалось отправить %s пользователю %s: %s", row["id"], user_id, e)
                break
        await asyncio.sleep(0.05)
    return sent, True


async def try_delete(bot, chat_id, message_id):
    for attempt in range(2):
        try:
            return await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramError:
            return False
    return False


async def delete_tracked(bot, older_than_hours=None, user_id=None):
    """Удаляет из чатов пользователей всё, что бот им отправил.

    Возвращает (удалено, не_удалось, слишком_старые).
    "Слишком старые" — те, что старше ~48 часов: Telegram не позволяет их удалить.
    """
    now = time.time()
    query, conds, params = "SELECT * FROM sent_messages", [], []
    if older_than_hours is not None:
        conds.append("sent_at <= ?")
        params.append(now - older_than_hours * 3600)
    if user_id is not None:
        conds.append("user_id = ?")
        params.append(user_id)
    if conds:
        query += " WHERE " + " AND ".join(conds)
    with db() as c:
        rows = c.execute(query, params).fetchall()

    deleted = failed = expired = 0
    for r in rows:
        if now - r["sent_at"] >= SAFE_WINDOW:
            expired += 1
        else:
            if await try_delete(bot, r["user_id"], r["telegram_message_id"]):
                deleted += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)

    ids = [(r["id"],) for r in rows]
    with db() as c:
        c.executemany("DELETE FROM sent_messages WHERE id = ?", ids)
    return deleted, failed, expired


def format_delete_report(deleted, failed, expired):
    text = f"Удалено из чатов пользователей: {deleted}"
    if failed:
        text += f"\nНе удалось (уже удалены вручную или чат недоступен): {failed}"
    if expired:
        text += (
            f"\nНе удалить, т.к. прошло больше 48 часов (ограничение Telegram): {expired}"
        )
    return text


# =====================================================================
# Клавиатуры и тексты
# =====================================================================
USER_KEYBOARD = InlineKeyboardMarkup(
    [
        [InlineKeyboardButton("📥 Получить новые сообщения", callback_data="get_new")],
        [InlineKeyboardButton("📚 Получить все сообщения", callback_data="get_all")],
    ]
)

ADMIN_HELP = (
    "Вы администратор. Просто отправляйте мне текст, документы, фото, видео, "
    "аудио или голосовые — я сохраню их, и пользователи заберут их по кнопке.\n\n"
    "Команды:\n"
    "/push — сразу разослать всем пользователям то, что они ещё не получали\n"
    "/wipe — удалить у ВСЕХ пользователей из чатов всё, что бот им присылал "
    "(сохранённые файлы останутся в боте)\n"
    "/clear — удалить сообщения из чатов пользователей И все сохранённые файлы\n"
    "/users — список пользователей и их активность\n"
    "/ban ID — закрыть пользователю доступ и удалить у него сообщения бота "
    "(можно несколько ID через пробел)\n"
    "/unban ID — вернуть доступ\n"
    "/banned — список заблокированных\n"
    "/stats — статистика и состояние хранилища\n"
    "/help — эта подсказка\n\n"
    "⚠️ Telegram позволяет удалять сообщения только в течение 48 часов после "
    "отправки. Более старые удалить невозможно."
)


BANNED_TEXT = "⛔ Доступ к боту закрыт администратором."


def storage_line():
    if PERSISTENT:
        return "💾 Хранилище: постоянное (данные сохраняются)"
    return (
        "⚠️ Хранилище: ВРЕМЕННОЕ! Диск (Volume) не подключён — данные будут "
        "стираться при каждом перезапуске. Подключите Volume на Railway."
    )


# =====================================================================
# Команды
# =====================================================================
def is_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in ADMIN_IDS


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS and is_banned(user.id):
        await update.message.reply_text(BANNED_TEXT)
        return
    register_user(user)
    touch_user(user.id, "last_start")
    if user.id in ADMIN_IDS:
        await say(context.bot, user.id, ADMIN_HELP, USER_KEYBOARD)
    else:
        await say(
            context.bot,
            user.id,
            "Привет! Нажмите кнопку ниже, чтобы получить материалы от администратора.",
            USER_KEYBOARD,
        )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update):
        await say(context.bot, update.effective_user.id, ADMIN_HELP, USER_KEYBOARD)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    s = get_stats()
    auto = f"каждые {AUTO_DELETE_HOURS:g} ч" if AUTO_DELETE_HOURS > 0 else "выключено"
    await update.message.reply_text(
        f"📊 Статистика\n\n"
        f"Сохранено сообщений/файлов: {s['stored']}\n"
        f"Пользователей: {s['users']} (заблокировали бота: {s['blocked']}, закрыт доступ: {s['banned']})\n"
        f"Отправлено пользователям и отслеживается: {s['tracked']} в {s['chats']} чатах\n"
        f"  из них ещё можно удалить (младше 48 ч): {s['deletable']}\n"
        f"Автоудаление через N часов: {auto}\n\n"
        f"{storage_line()}"
    )


async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    with db() as c:
        rows = c.execute(
            "SELECT * FROM users ORDER BY COALESCE(last_request, last_start, first_seen) DESC"
        ).fetchall()
    if not rows:
        await update.message.reply_text("Пока никто не заходил в бота.")
        return

    blocks = []
    for r in rows:
        name = r["full_name"] or "Без имени"
        uname = f"@{r['username']}" if r["username"] else "без username"
        flag = ""
        if r["banned"]:
            flag = " ⛔ ДОСТУП ЗАКРЫТ"
        elif r["blocked"]:
            flag = " 🚫 заблокировал бота"
        blocks.append(
            f"• {name} ({uname}, id: {r['user_id']}){flag}\n"
            f"   Первый заход: {fmt_dt(r['first_seen'])}\n"
            f"   Последний /start: {fmt_dt(r['last_start'])}\n"
            f"   Последний запрос сообщений: {fmt_dt(r['last_request'])}"
        )

    chunk = f"👥 Всего пользователей: {len(rows)}"
    for block in blocks:
        if len(chunk) + len(block) + 2 > 3500:
            await update.message.reply_text(chunk)
            chunk = block
        else:
            chunk += "\n\n" + block
    await update.message.reply_text(chunk)


def parse_ids(args):
    ids, bad = [], []
    for a in args:
        for part in a.replace(",", " ").split():
            try:
                ids.append(int(part))
            except ValueError:
                bad.append(part)
    return list(dict.fromkeys(ids)), bad


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    ids, bad = parse_ids(context.args)
    if not ids:
        await update.message.reply_text(
            "Укажите ID пользователя:\n/ban 123456789\n"
            "Или сразу нескольких: /ban 111 222 333\n"
            "ID можно посмотреть в команде /users."
        )
        return
    skipped = [i for i in ids if i in ADMIN_IDS]
    ids = [i for i in ids if i not in ADMIN_IDS]
    notes = ""
    if skipped:
        notes += "\nАдминистраторов блокировать нельзя, пропущены: " + ", ".join(map(str, skipped))
    if bad:
        notes += "\nНе похоже на ID, пропущено: " + ", ".join(bad)
    if not ids:
        await update.message.reply_text("Некого блокировать." + notes)
        return
    for uid in ids:
        set_banned(uid, True)  # доступ закрывается сразу
    await update.message.reply_text(
        f"⛔ Доступ закрыт: {len(ids)}. Удаляю их сообщения в фоне, пришлю отчёт." + notes
    )
    spawn(run_ban(context.bot, update.effective_chat.id, ids))


async def run_ban(bot, report_chat_id, ids):
    deleted = failed = expired = 0
    for uid in ids:
        d, f, e = await delete_tracked(bot, user_id=uid)
        deleted, failed, expired = deleted + d, failed + f, expired + e
    text = f"⛔ Заблокировано пользователей: {len(ids)}\n" + format_delete_report(deleted, failed, expired)
    try:
        await bot.send_message(chat_id=report_chat_id, text=text)
    except TelegramError:
        pass


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    ids, bad = parse_ids(context.args)
    if not ids:
        await update.message.reply_text("Укажите ID: /unban 123456789")
        return
    for uid in ids:
        set_banned(uid, False)
    text = f"✅ Доступ возвращён: {len(ids)}"
    if bad:
        text += "\nНе похоже на ID, пропущено: " + ", ".join(bad)
    await update.message.reply_text(text)


async def cmd_banned(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    with db() as c:
        rows = c.execute("SELECT * FROM users WHERE banned = 1").fetchall()
    if not rows:
        await update.message.reply_text("Заблокированных пользователей нет.")
        return
    lines = [f"⛔ Заблокировано: {len(rows)}"]
    for r in rows:
        uname = f"@{r['username']}" if r["username"] else "без username"
        lines.append(f"• {r['full_name'] or 'Без имени'} ({uname}, id: {r['user_id']})")
    await update.message.reply_text("\n".join(lines) + "\n\nВернуть доступ: /unban ID")


def confirm_keyboard(action):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Да, удалить", callback_data=f"admin:{action}"),
                InlineKeyboardButton("Отмена", callback_data="admin:cancel"),
            ]
        ]
    )


async def cmd_wipe(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    s = get_stats()
    await update.message.reply_text(
        f"Удалить из чатов ВСЕХ пользователей всё, что бот им присылал?\n\n"
        f"Отслеживается сообщений: {s['tracked']} (в {s['chats']} чатах)\n"
        f"Из них можно удалить (младше 48 ч): {s['deletable']}\n"
        f"Остальные удалить невозможно — ограничение Telegram.\n\n"
        f"Сохранённые файлы в боте останутся, пользователи смогут получить их снова.",
        reply_markup=confirm_keyboard("wipe"),
    )


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    s = get_stats()
    await update.message.reply_text(
        f"Удалить ВСЁ?\n\n"
        f"1) Сообщения из чатов пользователей (можно удалить: {s['deletable']} из {s['tracked']})\n"
        f"2) Все сохранённые в боте файлы и тексты ({s['stored']} шт.)\n\n"
        f"Это необратимо.",
        reply_markup=confirm_keyboard("clear"),
    )


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    await update.message.reply_text("📤 Начинаю рассылку в фоне. Пришлю отчёт, когда закончу.")
    spawn(run_push(context.bot, update.effective_chat.id))


async def run_push(bot, report_chat_id):
    with db() as c:
        users = c.execute(
            "SELECT user_id, last_seen_id FROM users WHERE blocked = 0 AND banned = 0"
        ).fetchall()

    reached = total = unreachable = 0
    for u in users:
        rows = messages_after(u["last_seen_id"])
        if not rows:
            continue
        sent, ok = await deliver(bot, u["user_id"], rows)
        total += sent
        if ok:
            reached += 1
            set_last_seen(u["user_id"], rows[-1]["id"])
        else:
            unreachable += 1
    text = f"✅ Рассылка завершена.\nПолучили новое: {reached} польз., отправлено сообщений: {total}"
    if unreachable:
        text += f"\nНедоступны (заблокировали бота): {unreachable}"
    try:
        await bot.send_message(chat_id=report_chat_id, text=text)
    except TelegramError:
        pass


async def run_wipe(bot, report_chat_id, clear_storage):
    deleted, failed, expired = await delete_tracked(bot)
    text = "🗑 Готово.\n" + format_delete_report(deleted, failed, expired)
    if clear_storage:
        with db() as c:
            c.execute("DELETE FROM messages")
            c.execute("UPDATE users SET last_seen_id = 0")
        text += "\nСохранённые в боте файлы и тексты удалены."
    try:
        await bot.send_message(chat_id=report_chat_id, text=text)
    except TelegramError:
        pass


async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id not in ADMIN_IDS:
        await query.answer("Только для администратора", show_alert=True)
        return
    await query.answer()
    action = query.data.split(":", 1)[1]
    if action == "cancel":
        await query.edit_message_text("Отменено.")
        return
    if action in ("wipe", "clear"):
        await query.edit_message_text("⏳ Удаляю в фоне. Пришлю отчёт, когда закончу.")
        spawn(run_wipe(context.bot, query.message.chat_id, clear_storage=(action == "clear")))


# =====================================================================
# Пользовательские кнопки
# =====================================================================
async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    if user.id not in ADMIN_IDS and is_banned(user.id):
        await query.answer(BANNED_TEXT, show_alert=True)
        return
    await query.answer()
    register_user(user)
    touch_user(user.id, "last_request")

    if query.data == "get_new":
        rows = messages_after(get_last_seen(user.id))
        empty_text = "Новых сообщений нет."
    else:
        rows = messages_after(0)
        empty_text = "Сообщений пока нет."

    if not rows:
        await say(context.bot, user.id, empty_text, USER_KEYBOARD)
        return

    sent, ok = await deliver(context.bot, user.id, rows)
    if ok:
        set_last_seen(user.id, rows[-1]["id"])
        await say(context.bot, user.id, f"Готово! Отправлено сообщений: {sent}", USER_KEYBOARD)


# =====================================================================
# Приём контента от администратора
# =====================================================================
async def content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        if is_banned(user.id):
            await update.message.reply_text(BANNED_TEXT)
            return
        register_user(user)
        await say(
            context.bot,
            user.id,
            "Отправка контента доступна только администратору.\n"
            "Используйте кнопки ниже, чтобы получить сообщения:",
            USER_KEYBOARD,
        )
        return

    m = update.message
    if m.text:
        save_message("text", text=m.text)
    elif m.document:
        save_message("document", file_id=m.document.file_id, caption=m.caption)
    elif m.photo:
        save_message("photo", file_id=m.photo[-1].file_id, caption=m.caption)
    elif m.video:
        save_message("video", file_id=m.video.file_id, caption=m.caption)
    elif m.audio:
        save_message("audio", file_id=m.audio.file_id, caption=m.caption)
    elif m.voice:
        save_message("voice", file_id=m.voice.file_id, caption=m.caption)
    elif m.animation:
        save_message("animation", file_id=m.animation.file_id, caption=m.caption)
    else:
        await m.reply_text("Этот тип контента пока не поддерживается ботом.")
        return

    await m.reply_text("✅ Сохранено. Пользователи получат это по кнопке (или сразу — по команде /push).")


# =====================================================================
# Фоновые задачи и запуск
# =====================================================================
async def auto_cleanup(context: ContextTypes.DEFAULT_TYPE):
    if AUTO_DELETE_HOURS <= 0:
        return
    deleted, failed, expired = await delete_tracked(context.bot, older_than_hours=AUTO_DELETE_HOURS)
    if deleted or failed or expired:
        logger.info("Автоудаление: удалено=%s, не удалось=%s, устарело=%s", deleted, failed, expired)


async def on_startup(app: Application):
    s = get_stats()
    text = (
        "🤖 Бот запущен.\n"
        f"В базе: сообщений {s['stored']}, пользователей {s['users']}.\n"
        f"{storage_line()}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await app.bot.send_message(chat_id=admin_id, text=text)
        except TelegramError as e:
            logger.warning("Не удалось уведомить админа %s: %s", admin_id, e)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан токен бота. Добавьте переменную BOT_TOKEN.")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS пуст — никто не сможет отправлять контент.")

    init_db()
    logger.info("База данных: %s (постоянная: %s)", DB_PATH, PERSISTENT)

    app = Application.builder().token(BOT_TOKEN).post_init(on_startup).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("banned", cmd_banned))
    app.add_handler(CommandHandler("wipe", cmd_wipe))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CallbackQueryHandler(admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(user_callback, pattern=r"^get_(new|all)$"))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, content_handler))

    if AUTO_DELETE_HOURS > 0:
        if app.job_queue is None:
            logger.warning("JobQueue недоступен — автоудаление не будет работать.")
        else:
            app.job_queue.run_repeating(auto_cleanup, interval=900, first=60)
            logger.info("Автоудаление включено: старше %s ч", AUTO_DELETE_HOURS)

    logger.info("Bot started")
    app.run_polling()


if __name__ == "__main__":
    main()
