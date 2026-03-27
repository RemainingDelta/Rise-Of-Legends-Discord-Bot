import discord
from discord.ext import commands
import os
from dotenv import load_dotenv

from database.mongo import db
from features.config import BOT_VERSION

load_dotenv()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    print(f"✅ ROL Bot v{BOT_VERSION}")

    if db is not None:
        print("✅ MongoDB Connected via 'database.mongo'")
    else:
        print("❌ MongoDB Connection Failed (Check .env and MONGO_URI)")

    # Load tourney cog
    try:
        await bot.load_extension("features.tourney.tourney_commands")
        print("✅ Loaded Feature: Tournaments")
    except Exception as e:
        print(f"❌ Error loading tourney: {e}")

    # Sync slash commands
    try:
        synced = await bot.tree.sync()
        print(f"✅ Slash Commands Synced: {len(synced)} commands available")
    except Exception as e:
        print(f"⚠️ Command Sync Error: {e}")

    print("🚀 Bot Startup Complete!")


if __name__ == "__main__":
    MODE = os.getenv("BOT_MODE", "DEV").upper()
    token = os.getenv("PROD_TOKEN") if MODE == "PROD" else os.getenv("DEV_TOKEN")
    if token:
        bot.run(token)
    else:
        print(f"❌ {MODE}_TOKEN not found in .env")
