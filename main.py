import os
import re
import html
import logging
import sqlite3
from contextlib import closing
from urllib.parse import urlparse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SOURCE_CHANNEL_ID = os.getenv("SOURCE_CHANNEL_ID", "").strip()
ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip().lstrip("-").isdigit()
}
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/anime_channels.sqlite3")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("anime-auto-publisher")


def connect_db():
    folder = os.path.dirname(DATABASE_PATH)
    if folder:
        os.makedirs(folder, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(connect_db()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anime_routes (
                anime_key TEXT PRIMARY KEY,
                anime_name TEXT NOT NULL,
                channel_id INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()


def normalize_name(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).casefold()


def get_setting(key):
    with closing(connect_db()) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    with closing(connect_db()) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        conn.commit()


def get_route(anime_name):
    with closing(connect_db()) as conn:
        return conn.execute(
            "SELECT anime_name, channel_id FROM anime_routes WHERE anime_key = ?",
            (normalize_name(anime_name),),
        ).fetchone()


def parse_channel_id(value):
    value = value.strip()
    if not re.fullmatch(r"-?\d+", value):
        raise ValueError("Channel must be a numeric Telegram channel ID, e.g. -1001234567890.")
    return int(value)


def parse_post(text):
    """
    Supported examples:
      Tomb Raider King
      Episode 12👇
      480P - https://example.com/480 | 720P - https://example.com/720
      1080P - https://example.com/1080

    Also accepts EP 12, E12, S1 Episode 12, and quality/link pairs on one line.
    """
    if not text:
        return None

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(lines) < 2:
        return None

    episode_re = re.compile(
        r"\b(?:(?:season|s)\s*\d+\s*[-:]?\s*)?"
        r"(?:episode|ep|e)\s*[-:#]?\s*(\d+(?:\.\d+)?)\b",
        re.IGNORECASE,
    )

    episode_index = None
    episode_number = None
    for index, line in enumerate(lines):
        match = episode_re.search(line)
        if match:
            episode_index = index
            episode_number = match.group(1)
            break

    if episode_index is None:
        return None

    anime_name = " ".join(lines[:episode_index]).strip(" \t-–—:|")
    if not anime_name:
        # Allow an episode heading on the first line only if the anime title appears
        # on the line immediately before it (already handled above).
        return None

    qualities = {}
    quality_re = re.compile(
        r"\b(144|240|360|480|540|576|720|1080|1440|2160)\s*[pP]\b"
        r"\s*[-:–—]?\s*(https?://[^\s|<>]+)",
        re.IGNORECASE,
    )
    for line in lines[episode_index + 1:]:
        for match in quality_re.finditer(line):
            quality = f"{match.group(1)}P"
            url = match.group(2).rstrip(".,;)")
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https") and parsed.netloc:
                qualities[quality] = url

    if not qualities:
        # Some posts put download links on the same line as the episode.
        for line in lines[episode_index:]:
            for match in quality_re.finditer(line):
                quality = f"{match.group(1)}P"
                url = match.group(2).rstrip(".,;)")
                parsed = urlparse(url)
                if parsed.scheme in ("http", "https") and parsed.netloc:
                    qualities[quality] = url

    if not qualities:
        return None

    return {
        "anime_name": anime_name,
        "episode_number": episode_number,
        "qualities": qualities,
    }


def make_caption(anime_name, episode_number):
    safe_name = html.escape(anime_name.upper())
    safe_episode = html.escape(episode_number)
    return (
        "╭━━━『 🔥 NEW EPISODE 』━━━╮\n\n"
        f"<blockquote>🎬 Anime: {safe_name}\n"
        f"📺 Episode: {safe_episode}\n"
        "🎧 Audio: Tamil</blockquote>\n\n"
        "╰━━━━━━━━━━━━━━━━━━╯\n\n"
        "📥 <b>DOWNLOAD LINKS</b>"
    )


def make_keyboard(qualities):
    # Keep requested layout: two buttons on row one, remaining button on row two.
    preferred = ["480P", "720P", "1080P", "360P", "240P", "144P", "540P", "576P", "1440P", "2160P"]
    ordered = [q for q in preferred if q in qualities]
    ordered.extend(q for q in qualities if q not in ordered)

    buttons = [
        InlineKeyboardButton(f"📥 {quality}", url=qualities[quality])
        for quality in ordered
    ]
    rows = []
    for index in range(0, len(buttons), 2):
        rows.append(buttons[index:index + 2])
    return InlineKeyboardMarkup(rows)


async def is_admin(update: Update) -> bool:
    user = update.effective_user
    if not user:
        return False
    if not ADMIN_IDS:
        await update.effective_message.reply_text(
            "⚠️ ADMIN_IDS is not configured. Add your Telegram numeric user ID in Render environment variables."
        )
        return False
    if user.id not in ADMIN_IDS:
        await update.effective_message.reply_text("⛔ This command is restricted to the bot admin.")
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🔥 Anime Auto Publisher is running!\n\n"
        "Commands:\n"
        "/addanime Anime Name -1001234567890\n"
        "/removeanime Anime Name\n"
        "/listanime\n"
        "/setdefault -1001234567890\n"
        "/defaultchannel\n"
        "/help"
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def addanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return
    args = context.args
    if len(args) < 2:
        await update.effective_message.reply_text(
            "Usage:\n/addanime Anime Name -1001234567890\n\n"
            "Put the channel ID last. The bot must be an administrator in that channel."
        )
        return

    try:
        channel_id = parse_channel_id(args[-1])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return

    anime_name = " ".join(args[:-1]).strip()
    if not anime_name:
        await update.effective_message.reply_text("Please provide an anime name.")
        return

    try:
        chat = await context.bot.get_chat(channel_id)
        with closing(connect_db()) as conn:
            conn.execute(
                "INSERT INTO anime_routes(anime_key, anime_name, channel_id) VALUES(?, ?, ?) "
                "ON CONFLICT(anime_key) DO UPDATE SET anime_name=excluded.anime_name, channel_id=excluded.channel_id",
                (normalize_name(anime_name), anime_name, channel_id),
            )
            conn.commit()
        await update.effective_message.reply_text(
            f"✅ Registered: {anime_name}\n"
            f"📢 Destination: {chat.title or channel_id}\n"
            f"🆔 ID: {channel_id}"
        )
    except Exception as exc:
        logger.exception("Failed to register route")
        await update.effective_message.reply_text(
            "❌ Couldn't access that channel. Make sure the bot is an administrator there "
            "and that the numeric channel ID is correct.\n"
            f"Details: {exc}"
        )


async def removeanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return
    anime_name = " ".join(context.args).strip()
    if not anime_name:
        await update.effective_message.reply_text("Usage: /removeanime Anime Name")
        return
    with closing(connect_db()) as conn:
        cursor = conn.execute(
            "DELETE FROM anime_routes WHERE anime_key = ?",
            (normalize_name(anime_name),),
        )
        conn.commit()
    if cursor.rowcount:
        await update.effective_message.reply_text(f"🗑️ Removed route for: {anime_name}")
    else:
        await update.effective_message.reply_text(f"ℹ️ No route found for: {anime_name}")


async def listanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return
    with closing(connect_db()) as conn:
        rows = conn.execute(
            "SELECT anime_name, channel_id FROM anime_routes ORDER BY anime_name COLLATE NOCASE"
        ).fetchall()
    if not rows:
        await update.effective_message.reply_text("No anime routes registered yet.")
        return
    lines = ["📚 Registered anime channels:"]
    for row in rows:
        lines.append(f"• {row['anime_name']} — `{row['channel_id']}`")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def setdefault(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return
    if len(context.args) != 1:
        await update.effective_message.reply_text("Usage: /setdefault -1001234567890")
        return
    try:
        channel_id = parse_channel_id(context.args[0])
        chat = await context.bot.get_chat(channel_id)
        set_setting("default_channel_id", channel_id)
        await update.effective_message.reply_text(
            f"✅ Default channel set to {chat.title or channel_id}\n🆔 {channel_id}"
        )
    except Exception as exc:
        logger.exception("Failed to set default channel")
        await update.effective_message.reply_text(
            "❌ Couldn't access that channel. Add the bot as an administrator and check the ID.\n"
            f"Details: {exc}"
        )


async def defaultchannel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update):
        return
    channel_id = get_setting("default_channel_id")
    if not channel_id:
        await update.effective_message.reply_text(
            "No default channel configured. Use /setdefault -1001234567890"
        )
        return
    try:
        chat = await context.bot.get_chat(int(channel_id))
        title = chat.title or str(channel_id)
    except Exception:
        title = "Unable to fetch channel title"
    await update.effective_message.reply_text(f"📢 Default channel: {title}\n🆔 {channel_id}")


async def process_source_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    source_id = SOURCE_CHANNEL_ID
    if not source_id:
        logger.error("SOURCE_CHANNEL_ID is not configured")
        return
    try:
        expected_source_id = int(source_id)
    except ValueError:
        logger.error("SOURCE_CHANNEL_ID must be a numeric channel ID")
        return

    if not update.effective_chat or update.effective_chat.id != expected_source_id:
        return

    parsed = parse_post(message.text)
    if not parsed:
        logger.info("Ignored source post: did not match anime/episode/quality-link format")
        return

    anime_name = parsed["anime_name"]
    route = get_route(anime_name)
    if route:
        destination_id = int(route["channel_id"])
        destination_name = route["anime_name"]
    else:
        default_id = get_setting("default_channel_id")
        if not default_id:
            logger.warning("No route for %s and no default channel configured", anime_name)
            return
        destination_id = int(default_id)
        destination_name = "default channel"

    caption = make_caption(anime_name, parsed["episode_number"])
    keyboard = make_keyboard(parsed["qualities"])

    try:
        await context.bot.send_message(
            chat_id=destination_id,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        logger.info(
            "Published %s Episode %s to %s",
            anime_name,
            parsed["episode_number"],
            destination_name,
        )
    except Exception:
        logger.exception("Failed to publish %s to channel %s", anime_name, destination_id)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Telegram handler error: %s", context.error)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN in Render environment variables.")
    if not SOURCE_CHANNEL_ID or not SOURCE_CHANNEL_ID.lstrip("-").isdigit():
        raise RuntimeError("Set SOURCE_CHANNEL_ID to the numeric ID of your source channel.")
    if not ADMIN_IDS:
        logger.warning("ADMIN_IDS is empty; admin commands will not work until configured.")

    init_db()
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("addanime", addanime))
    application.add_handler(CommandHandler("removeanime", removeanime))
    application.add_handler(CommandHandler("listanime", listanime))
    application.add_handler(CommandHandler("setdefault", setdefault))
    application.add_handler(CommandHandler("defaultchannel", defaultchannel))
    application.add_handler(
        MessageHandler(filters.ChatType.CHANNEL & filters.TEXT, process_source_post)
    )
    application.add_error_handler(error_handler)

    # Webhook-free long polling works on Render Web Services, but requires a lightweight
    # HTTP server for Render's port check. This service uses a simple health endpoint.
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    port = int(os.getenv("PORT", "10000"))

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Anime Auto Publisher is running.")

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Health server listening on port %s", port)

    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
