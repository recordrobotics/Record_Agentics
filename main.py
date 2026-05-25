import asyncio
import discord
from discord.ext import commands
from dotenv import load_dotenv
import os

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

COGS = [
    "cogs.request_handler",        # load first — other cogs reference it
    "cogs.calendar_panel",
    "cogs.agenda_achievements",    # unified agenda + achievements
    "cogs.signups",
]

# @bot.event
# async def on_ready():
#     print(f"Logged in as {bot.user} (ID: {bot.user.id})")
#     try:
#         synced = await bot.tree.sync()
#         print(f"Synced {len(synced)} slash commands")
#     except Exception as e:
#         print(f"Command sync failed: {e}")
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        guild = discord.Object(id=1006751650028994700)  # paste your server ID
        bot.tree.copy_global_to(guild=guild)
        synced = await bot.tree.sync(guild=guild)
        print(f"Synced {len(synced)} slash commands to guild")
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
        await bot.start(os.getenv("DISCORD_TOKEN"))

asyncio.run(main())
