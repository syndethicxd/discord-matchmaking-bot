import os
import sys
import asyncio
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------
MOD_ROLE_NAME = "matchmaking mod"
HERALD_ROLE_NAME = "herald"

MOD_PROOF_CHANNEL_ID = 1557114424576319598  # Замени на ID канала модерации
LOG_CHANNEL_ID = 1557311338378694667        # Замени на ID канала логов

# ---------------------------------------------------------------------------
# HTTP SERVER FOR RENDER HEALTH CHECKS
# ---------------------------------------------------------------------------

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active and running!")

    def log_message(self, format, *args):
        return

def run_health_check_server():
    try:
        port = int(os.getenv("PORT", 8080))
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        print(f">>> [HTTP] Сервер запущен на порту {port}")
        server.serve_forever()
    except Exception as e:
        print(f">>> [HTTP Error] {e}")

# Запускаем HTTP-сервер в фоне
server_thread = threading.Thread(target=run_health_check_server, daemon=True)
server_thread.start()

# ---------------------------------------------------------------------------
# DATA & HELPER FUNCTIONS
# ---------------------------------------------------------------------------

match_counter = 1
user_data = {}

def get_user_stats(user_id: int):
    if user_id not in user_data:
        user_data[user_id] = {"pts": 0, "wins": 0, "losses": 0}
    return user_data[user_id]

def calculate_winrate(wins: int, losses: int) -> float:
    total = wins + losses
    if total == 0:
        return 0.0
    return round((wins / total) * 100, 1)

async def check_and_grant_herald_role(member: discord.Member, pts: int):
    if pts >= 100:
        role = discord.utils.find(lambda r: r.name.lower() == HERALD_ROLE_NAME.lower(), member.guild.roles)
        if role and role not in member.roles:
            try:
                await member.add_roles(role)
            except Exception as e:
                print(f"Ошибка при выдаче роли {HERALD_ROLE_NAME}: {e}")

async def send_log(guild: discord.Guild, title: str, description: str, color: discord.Color = discord.Color.blue()):
    if not LOG_CHANNEL_ID or LOG_CHANNEL_ID == 123456789012345678:
        return
    log_channel = guild.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.utcnow()
        )
        try:
            await log_channel.send(embed=embed)
        except Exception as e:
            print(f"Ошибка отправки лога: {e}")

def is_matchmaking_mod():
    async def predicate(interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        
        is_admin = interaction.user.guild_permissions.administrator
        has_mod_role = any(role.name.lower() == MOD_ROLE_NAME.lower() for role in interaction.user.roles)
        
        if not (is_admin or has_mod_role):
            await interaction.response.send_message(f"❌ У вас нет роли **{MOD_ROLE_NAME}**!", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ---------------------------------------------------------------------------
# VERDICT MODAL & CHECK VERIFIED VIEW
# ---------------------------------------------------------------------------

class VerdictModal(discord.ui.Modal, title="Вердикт проверки"):
    verdict = discord.ui.TextInput(
        label="Вердикт",
        style=discord.TextStyle.paragraph,
        placeholder="Введите вердикт...",
        required=True,
        max_length=1000
    )

    def __init__(self, target_user: discord.Member, match_id: int):
        super().__init__()
        self.target_user = target_user
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="📋 Вынесен вердикт проверки",
                description=f"**Игрок:** {self.target_user.mention}\n**Модератор:** {interaction.user.mention}\n\n**Вердикт:**\n{self.verdict.value}",
                color=discord.Color.purple()
            )
        )

        await send_log(
            guild=interaction.guild,
            title="🔍 Лог: Завершение проверки",
            description=f"**Модератор:** {interaction.user.mention}\n"
                        f"**Игрок:** {self.target_user.mention}\n"
                        f"**Матч №:** `{self.match_id}`\n"
                        f"**Вердикт:**\n{self.verdict.value}",
            color=discord.Color.purple()
        )

        await asyncio.sleep(5)
        try:
            await interaction.channel.delete(reason="Завершение проверки")
        except Exception as e:
            print(f"Ошибка удаления канала проверки: {e}")

class CheckControlView(discord.ui.View):
    def __init__(self, target_user: discord.Member, match_id: int):
        super().__init__(timeout=None)
        self.target_user = target_user
        self.match_id = match_id

    @discord.ui.button(label="Завершить проверку", style=discord.ButtonStyle.danger, custom_id="finish_check_btn")
    async def finish_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        is_admin = interaction.user.guild_permissions.administrator
        has_mod_role = any(role.name.lower() == MOD_ROLE_NAME.lower() for role in interaction.user.roles)
        if not (is_admin or has_mod_role):
            await interaction.response.send_message("❌ Только модераторы могут завершать проверку!", ephemeral=True)
            return

        await interaction.response.send_modal(VerdictModal(self.target_user, self.match_id))

# ---------------------------------------------------------------------------
# MODERATION PROOF REVIEW VIEW
# ---------------------------------------------------------------------------

class ModProofReviewView(discord.ui.View):
    def __init__(self, winner: discord.Member, players: list[discord.User], weapon: str, location: str, proof_url: str, match_channel: discord.TextChannel, match_id: int):
        super().__init__(timeout=None)
        self.winner = winner
        self.players = players
        self.weapon = weapon
        self.location = location
        self.proof_url = proof_url
        self.match_channel = match_channel
        self.match_id = match_id

    @discord.ui.button(label="Одобрить", style=discord.ButtonStyle.success, custom_id="mod_approve_proof")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        stats = get_user_stats(self.winner.id)
        stats["pts"] += 15
        stats["wins"] += 1

        await check_and_grant_herald_role(self.winner, stats["pts"])

        for p in self.players:
            if p.id != self.winner.id:
                p_stats = get_user_stats(p.id)
                p_stats["losses"] += 1

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.title = "✅ Дуэль Одобрена"
        embed.add_field(name="Результат", value=f"Победителю {self.winner.mention} начислено **+15 Pts**", inline=False)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(embed=embed, view=self)

        await interaction.response.send_message(f"✅ Результат одобрен. +15 Pts выслано {self.winner.mention}.", ephemeral=True)

        players_mentions = [p.mention for p in self.players]
        players_str = " vs ".join(players_mentions)

        await send_log(
            guild=interaction.guild,
            title="✅ Лог: Матч одобрен",
            description=f"**Модератор:** {interaction.user.mention}\n"
                        f"**Матч №:** `{self.match_id}`\n"
                        f"**Участники:** {players_str}\n"
                        f"**Победитель:** {self.winner.mention} (+15 Pts)\n"
                        f"**Доказательство:** [Ссылка на видео]({self.proof_url})",
            color=discord.Color.green()
        )

        if self.match_channel:
            try:
                await self.match_channel.send("✅ **Матч одобрен модерацией! Канал будет удалён через 7 секунд...**")
                await asyncio.sleep(7)
                await self.match_channel.delete(reason="Матч завершён и одобрен")
            except Exception as e:
                print(f"Ошибка удаления канала: {e}")

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, custom_id="mod_decline_proof")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.title = "❌ Дуэль Отклонена"
        embed.add_field(name="Результат", value="Pts не начисляются никому.", inline=False)

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(embed=embed, view=self)

        await interaction.response.send_message("❌ Доказательство отклонено.", ephemeral=True)

        players_mentions = [p.mention for p in self.players]
        players_str = " vs ".join(players_mentions)

        await send_log(
            guild=interaction.guild,
            title="❌ Лог: Матч отклонён",
            description=f"**Модератор:** {interaction.user.mention}\n"
                        f"**Матч №:** `{self.match_id}`\n"
                        f"**Участники:** {players_str}\n"
                        f"**Заявленный победитель:** {self.winner.mention}\n"
                        f"**Доказательство:** [Ссылка на видео]({self.proof_url})",
            color=discord.Color.red()
        )

        if self.match_channel:
            try:
                await self.match_channel.send("❌ **Доказательства отклонены модерацией. Канал будет удалён через 7 секунд...**")
                await asyncio.sleep(7)
                await self.match_channel.delete(reason="Матч отклонен модерацией")
            except Exception as e:
                print(f"Ошибка удаления канала: {e}")

    @discord.ui.button(label="Проверка", style=discord.ButtonStyle.primary, custom_id="mod_check_proof")
    async def start_check(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        mod_role = discord.
        
