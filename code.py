import os
import re
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ConversationHandler, MessageHandler,
    ContextTypes, filters,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("AnimeNetworkTamilBot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_ID", "").strip()
MONGO_URI = os.getenv("MONGO_URI", "").strip()
DEFAULT_CHANNEL_RAW = os.getenv("DEFAULT_CHANNEL_ID", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable")
if not MONGO_URI:
    raise RuntimeError("Missing MONGO_URI environment variable")

ADMIN_IDS = set()
for value in ADMIN_IDS_RAW.split(","):
    value = value.strip()
    if value:
        try:
            ADMIN_IDS.add(int(value))
        except ValueError:
            log.warning("Ignoring invalid ADMIN_ID value: %s", value)

DEFAULT_CHANNEL_ID = None
if DEFAULT_CHANNEL_RAW:
    try:
        DEFAULT_CHANNEL_ID = int(DEFAULT_CHANNEL_RAW)
    except ValueError as exc:
        raise RuntimeError("DEFAULT_CHANNEL_ID must be a numeric Telegram channel ID") from exc

mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=10000)
db = mongo_client["animenetworktamil_quality_bot"]
anime_collection = db["anime_routes"]
processed_collection = db["processed_posts"]

# Remove legacy malformed records before building the unique index.
processed_collection.delete_many({
    "$or": [
        {"source_chat_id": {"$exists": False}},
        {"source_message_id": {"$exists": False}},
        {"source_chat_id": None},
        {"source_message_id": None},
    ]
})
processed_collection.create_index(
    [("source_chat_id", ASCENDING), ("source_message_id", ASCENDING)],
    unique=True,
)
anime_collection.create_index([("anime_key", ASCENDING)], unique=True)

ANIME_NAME, DESTINATION_ID = range(2)


def admin(user_id):
    return user_id in ADMIN_IDS


async def require_admin(update: Update):
    user = update.effective_user
    if not user or not admin(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Admin only.")
        return False
    return True


def anime_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).casefold()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "🎬 AnimeNetworkTamil Quality Bot is online!\\n\\n"
        "Admin commands:\\n"
        "/addanime - Add anime destination channel\\n"
        "/listanime - List routes\\n"
        "/removeanime <anime name> - Remove route\\n"
        "/cancel - Cancel setup\\n\\n"
        "Post an anime name, episode, and quality URLs with a photo in your private source channel."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


async def addanime_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return ConversationHandler.END
    await update.effective_message.reply_text("Send the anime name (example: BLACK CLOVER):")
    return ANIME_NAME


async def addanime_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.effective_message.text or "").strip()
    if not name:
        await update.effective_message.reply_text("Please send a valid anime name:")
        return ANIME_NAME
    context.user_data["route_name"] = name
    await update.effective_message.reply_text(
        "Send the destination channel ID (usually starts with -100). "
        "Make the bot an administrator in that channel with permission to post."
    )
    return DESTINATION_ID


async def addanime_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        channel_id = int((update.effective_message.text or "").strip())
    except ValueError:
        await update.effective_message.reply_text("Channel ID must be numeric. Send it again:")
        return DESTINATION_ID

    name = context.user_data.get("route_name", "").strip()
    key = anime_key(name)
    anime_collection.update_one(
        {"anime_key": key},
        {"$set": {"anime_key": key, "anime_name": name, "destination_channel_id": channel_id}},
        upsert=True,
    )
    context.user_data.clear()
    await update.effective_message.reply_text(
        f"✅ Route saved!\\n🎬 Anime: {name}\\n📢 Destination: {channel_id}"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.effective_message.reply_text("Cancelled.")
    return ConversationHandler.END


async def listanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    routes = list(anime_collection.find().sort("anime_name", ASCENDING))
    if not routes:
        await update.effective_message.reply_text("No routes yet. Use /addanime.")
        return
    lines = ["📚 Anime destination routes:"]
    for route in routes:
        lines.append(f"• {route['anime_name']} → {route['destination_channel_id']}")
    await update.effective_message.reply_text("\\n".join(lines))


async def removeanime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return
    name = " ".join(context.args).strip()
    if not name:
        await update.effective_message.reply_text("Usage: /removeanime BLACK CLOVER")
        return
    result = anime_collection.delete_one({"anime_key": anime_key(name)})
    await update.effective_message.reply_text(
        "✅ Anime route removed." if result.deleted_count else "No matching anime route found."
    )


def parse_caption(caption: str):
    lines = [line.strip() for line in (caption or "").splitlines() if line.strip()]
    anime_name = None
    episode = None
    qualities = {}
    quality_pattern = re.compile(r"^\s*(360p|480p|720p|1080p|1440p|2160p|4k)\s*[-:|]\s*(https?://\S+)\s*$", re.I)
    episode_pattern = re.compile(r"\b(?:episode|ep)\s*[-:#.]?\s*(\d+(?:\.\d+)?)\b", re.I)

    for line in lines:
        match = quality_pattern.match(line)
        if match:
            quality = match.group(1).upper() if match.group(1).lower() == "4k" else match.group(1).lower()
            qualities[quality] = match.group(2).rstrip(".,)")
            continue
        ep_match = episode_pattern.search(line)
        if ep_match:
            episode = ep_match.group(1)
            # Remove episode text and common decorations to identify anime name if present on same line.
            candidate = episode_pattern.sub("", line).strip(" -:|🎬📺🔥⚡")
            if candidate and not anime_name:
                anime_name = candidate
            continue
        if not anime_name:
            candidate = re.sub(r"^[^A-Za-z0-9]+", "", line).strip()
            if candidate and not re.match(r"^(480p|720p|1080p|360p|1440p|2160p|4k)\b", candidate, re.I):
                anime_name = candidate

    return anime_name, episode, qualities


async def handle_source_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.channel_post
    if not message:
        return
    if not (message.photo or message.video or message.document):
        return

    caption = message.caption or ""
    name, episode, qualities = parse_caption(caption)
    if not name or not qualities:
        log.info("Ignoring post: anime name or quality URLs missing.")
        return

    route = anime_collection.find_one({"anime_key": anime_key(name)})
    destination_id = route.get("destination_channel_id") if route else DEFAULT_CHANNEL_ID
    if not destination_id:
        log.warning("No destination configured for anime: %s", name)
        return

    key = {"source_chat_id": message.chat.id, "source_message_id": message.message_id}
    try:
        processed_collection.insert_one({**key, "status": "processing", "anime_name": name})
    except DuplicateKeyError:
        log.info("Duplicate source post ignored: %s/%s", message.chat.id, message.message_id)
        return

    title = f"⚡ {name}"
    if episode:
        title += f"\\n\\n🎬 Episode {episode}"
    title += "\\n📺 Available Qualities"
    quality_order = ["360p", "480p", "720p", "1080p", "1440p", "2160p", "4K"]
    buttons = []
    row = []
    for quality in quality_order:
        if quality in qualities:
            row.append(InlineKeyboardButton(f"▶️ {quality}", url=qualities[quality]))
            if len(row) == 2:
                buttons.append(row)
                row = []
    if row:
        buttons.append(row)
    markup = InlineKeyboardMarkup(buttons)

    try:
        if message.photo:
            await context.bot.send_photo(
                chat_id=destination_id,
                photo=message.photo[-1].file_id,
                caption=title,
                reply_markup=markup,
            )
        elif message.video:
            await context.bot.send_video(
                chat_id=destination_id,
                video=message.video.file_id,
                caption=title,
                reply_markup=markup,
            )
        else:
            await context.bot.send_document(
                chat_id=destination_id,
                document=message.document.file_id,
                caption=title,
                reply_markup=markup,
            )
        processed_collection.update_one(key, {"$set": {"status": "published"}})
        log.info("Published %s episode=%s to %s", name, episode, destination_id)
    except Exception:
        processed_collection.delete_one(key)
        log.exception("Failed to publish source post.")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"OK"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "2")
        self.end_headers()

    def log_message(self, fmt, *args):
        log.info("Health HTTP: " + fmt, *args)


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", port)


def main():
    mongo_client.admin.command("ping")
    start_health_server()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addanime", addanime_start)],
        states={
            ANIME_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, addanime_name)],
            DESTINATION_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, addanime_destination)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))
    app.add_handler(CommandHandler("listanime", listanime))
    app.add_handler(CommandHandler("removeanime", removeanime))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, handle_source_post))

    log.info("AnimeNetworkTamil Quality Bot starting")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
