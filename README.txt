ANIMENETWORKTAMIL QUALITY BOT

Files:
- code.py: bot logic
- requirements.txt: dependencies
- render.yaml: Render Web Service setup

Render environment variables:
BOT_TOKEN = token from BotFather
ADMIN_ID = your numeric Telegram user ID; multiple IDs comma-separated
MONGO_URI = MongoDB connection string
DEFAULT_CHANNEL_ID = optional fallback destination channel ID

Commands:
 /start
 /help
 /addanime
 /listanime
 /removeanime BLACK CLOVER
 /cancel

How to use:
1. Deploy these files as a Render Web Service.
2. Add the environment variables.
3. Add the bot as an administrator in the private source channel and each destination channel.
4. In the bot's private chat, run /addanime, send anime name, then destination channel ID.
5. Upload a photo/video/document in the source channel with a caption such as:

BLACK CLOVER
Episode 01
480p - https://example.com/480p
720p - https://example.com/720p
1080p - https://example.com/1080p

The bot detects channel posts, matches the anime name to a registered route, and posts the media with quality URL buttons.
A fallback destination is used only when DEFAULT_CHANNEL_ID is set and the anime has no specific route.
Keep your bot token and MongoDB URI private.
