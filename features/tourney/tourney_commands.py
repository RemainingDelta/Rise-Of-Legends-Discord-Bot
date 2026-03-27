from discord.ext import commands


class TourneyCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        print("Tourney cog ready")


async def setup(bot):
    await bot.add_cog(TourneyCommands(bot))
