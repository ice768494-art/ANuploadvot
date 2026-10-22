# Anime Auto Publisher Bot

Automatically parses anime episode posts in one Telegram source channel and publishes text plus download buttons to a registered anime channel. Unknown anime go to a configurable default channel.

## Files
- `main.py` — bot
- `requirements.txt` — dependencies
- `render.yaml` — Render Web Service blueprint with persistent disk

## Render environment variables
Set these in the Render dashboard:
- `BOT_TOKEN`: token from BotFather
- `SOURCE_CHANNEL_ID`: numeric ID of the source channel, e.g. `-1001234567890`
- `ADMIN_IDS`: your Telegram numeric user ID (comma-separated IDs allowed)
- `DATABASE_PATH`: `/var/data/anime_channels.sqlite3` (with the disk in render.yaml)

## Telegram permissions
1. Add the bot as an administrator in the source channel so Telegram delivers channel posts to it.
2. Add the bot as an administrator in every destination channel, with permission to post messages.
3. Set a persistent disk mounted at `/var/data` if creating the service manually. SQLite data on a temporary filesystem can be lost on redeploy/restart.

## Commands (admin only)
- `/addanime Tomb Raider King -1001234567890`
- `/removeanime Tomb Raider King`
- `/listanime`
- `/setdefault -1001234567890`
- `/defaultchannel`
- `/help`

The bot validates that it can access a destination when a route/default is set.

## Supported source format
Example:
Tomb Raider King
Episode 12👇

480P - https://example.com/480 | 720P - https://example.com/720
1080P - https://example.com/1080

Also recognizes episode labels such as `EP 12`, `E12`, and `S1 Episode 12`.

## Notes
- The source post must be text-only (posters/media are ignored by design).
- URLs must be complete `http://` or `https://` links.
- Button order prioritizes 480P, 720P, and 1080P, then other detected resolutions.
- Telegram bot API cannot read older channel history; it processes new posts after it is running.
- If an unknown anime has no default channel configured, the post is skipped.
- Keep `BOT_TOKEN` secret. Never paste it into public source code or chat.
