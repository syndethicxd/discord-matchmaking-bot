import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# DUMMY HTTP SERVER FOR RENDER (Fixes "No open ports detected")
# ---------------------------------------------------------------------------

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return  # Отключаем спам в логи от проверок Render

def run_health_check_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# ---------------------------------------------------------------------------
# CONSTANTS & CONFIGS
# ---------------------------------------------------------------------------

MATCH_LOGS_CHANNEL_ID = 123456789012345678  # Укажите ID вашего канала логов

async def init_db():
    pass

def has_matchmaking_mod_role():
    async def predicate(interaction: discord.Interaction):
        return True
    return app_commands.check(predicate)

async def check_and_assign_herald_role(guild: discord.Guild, user_id: int, pts: int):
    pass

# ---------------------------------------------------------------------------
# MODALS & VIEWS FOR MATCHMAKING
# ---------------------------------------------------------------------------

class YoutubeProofModal(discord.ui.Modal, title="Завершение дуэли"):
    youtube_url = discord.ui.TextInput(
        label="Ссылка на YouTube (доказательство)",
        placeholder="https://www.youtube.com/watch?v=...",
        required=True,
        min_length=10
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"✅ **Матч завершён!**\nИгрок {interaction.user.mention} предоставил доказательство победы:\n{self.youtube_url.value}",
            ephemeral=False
        )

class MatchCancelView(discord.ui.View):
    def __init__(self, allowed_users: list[discord.User]):
        super().__init__(timeout=300)
        self.allowed_users = allowed_users

    @discord.ui.button(label="Принять отмену", style=discord.ButtonStyle.danger)
    async def accept_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in [u.id for u in self.allowed_users]:
            await interaction.response.send_message("❌ Вы не можете подтвердить отмену этого матча!", ephemeral=True)
            return

        await interaction.response.send_message("⛔ **Матч отменён по обоюдному согласию. Канал будет удалён через 7 секунд...**")
        self.stop()
        
        await asyncio.sleep(7)
        try:
            await interaction.channel.delete(reason="Матч отменен по обоюдному согласию")
        except Exception as e:
            print(f"Ошибка при удалении канала: {e}")

    @discord.ui.button(label="Отклонить отмену", style=discord.ButtonStyle.secondary)
    async def decline_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in [u.id for u in self.allowed_users]:
            await interaction.response.send_message("❌ Вы не можете отклонить отмену!", ephemeral=True)
            return

        await interaction.response.send_message("❌ **Запрос на отмену матча отклонён.** Продолжайте игру!", ephemeral=True)
        self.stop()

class ActiveMatchView(discord.ui.View):
    def __init__(self, players: list[discord.User]):
        super().__init__(timeout=None)
        self.players = players

    @discord.ui.button(label="Отмена матча", style=discord.ButtonStyle.danger, custom_id="active_match_cancel")
    async def request_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in [p.id for p in self.players]:
            await interaction.response.send_message("❌ Вы не являетесь участником этого матча!", ephemeral=True)
            return

        opponents = [p for p in self.players if p.id != interaction.user.id]
        opponents_mentions = " ".join([p.mention for p in opponents])
        
        view = MatchCancelView(allowed_users=opponents)
        await interaction.response.send_message(
            f"⚠️ {interaction.user.mention} запросил отмену матча. {opponents_mentions}, вы согласны?",
            view=view
        )

    @discord.ui.button(label="Завершить дуэль", style=discord.ButtonStyle.success, custom_id="active_match_finish")
    async def finish_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in [p.id for p in self.players]:
            await interaction.response.send_message("❌ Вы не являетесь участником этого матча!", ephemeral=True)
            return

        await interaction.response.send_modal(YoutubeProofModal())

class MatchInviteView(discord.ui.View):
    def __init__(self, challenger: discord.User, players: list[discord.User], location: str, weapon: str, mode: str):
        super().__init__(timeout=600)
        self.challenger = challenger
        self.players = players
        self.location = location
        self.weapon = weapon
        self.mode = mode

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in [p.id for p in self.players if p.id != self.challenger.id]:
            await interaction.response.send_message("❌ Этот вызов адресован не вам!", ephemeral=True)
            return

        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        for player in self.players:
            overwrites[player] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        channel_name = f"match-{self.mode.lower()}-{self
        
