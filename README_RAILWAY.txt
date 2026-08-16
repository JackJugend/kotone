Railway setup:
1. Add variable DISCORD_TOKEN with the bot token.
2. Add variable DATA_DIR=/app/data
3. Attach a Volume mounted at /app/data
4. Deploy. Start command is read from railway.json.

The included data.json is your latest uploaded state. On the first start with an empty Railway volume, bot.py copies it to /app/data/data.json. Existing volume data is never overwritten.
