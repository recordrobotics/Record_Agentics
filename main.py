import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
import os

load_dotenv()

from utils import store

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

COGS = [
    "cogs.request_handler",        # load first — other cogs reference it
    "cogs.agenda_achievements",    # unified agenda + achievements
    "cogs.signups",                # auto polls + manual meetings
    "cogs.channel_admin",          # /set_channel
    # Calendar stays disabled for now.
    # "cogs.calendar_panel",
]

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        # Sync the current command set to every server the bot is in. Guild
        # syncs are instant, so old/removed commands disappear right away.
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} commands to {guild.name} ({guild.id})")

        # Wipe any GLOBAL commands left over from an earlier global sync — those
        # linger for up to an hour and show up as duplicate/old commands.
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
        print("Cleared stale global commands.")
    except Exception as e:
        print(f"Command sync failed: {e}")
async def main():
    async with bot:
        for cog in COGS:
            try:
                await bot.load_extension(cog)
                print(f"Loaded {cog}")
            except Exception as e:
                print(f"Failed to load {cog}: {e}")

        # Load all sheet data into memory once, then start the write-behind loop.
        # Panels read from memory, so this must finish before the bot serves them.
        await store.hydrate()
        store.start_flusher()

        try:
            await bot.start(os.getenv("DISCORD_TOKEN"))
        finally:
            # Best-effort flush of anything still pending on shutdown/redeploy.
            await store.stop()

asyncio.run(main())
