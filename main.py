import os
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

MOD_PROOF_CHANNEL_ID = 1557114424576319598  # Замени на ID своего канала
LOG_CHANNEL_ID = 1557311338378694667        # Замени на ID своего канала

# ---------------------------------------------------------------------------
# HTTP SERVER FOR RENDER
# ---------------------------------------------------------------------------

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return

def run_health_check_server():
    try:
        port = int(os.getenv("PORT", 8080))
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        print(f">>> HTTP Сервер запущен на порту {port}")
        server.serve_forever()
    except Exception as e:
        print(f"HTTP Server Exception: {e}")

threading.Thread(target=run_health_check_server, daemon=True).start()

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
    """Выдача роли herald при достижении 100+ Pts"""
    if pts >= 100:
        role = discord.utils.find(lambda r: r.name.lower() == HERALD_ROLE_NAME.lower(), member.guild.roles)
        if role and role not in member.roles:
            try:
                await member.add_roles(role)
            except Exception as e:
                print(f"Ошибка при выдаче роли {HERALD_ROLE_NAME}: {e}")

async def send_log(guild: discord.Guild, title: str, description: str, color: discord.Color = discord.Color.blue()):
    """Отправка эмбеда с логом в указанный канал логов."""
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
        placeholder="Введите вердикт (например: Оправдан / Забанен)...",
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

        players_mentions = []
        for p in self.players:
            players_mentions.append(p.mention)
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

        players_mentions = []
        for p in self.players:
            players_mentions.append(p.mention)
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
        mod_role = discord.utils.find(lambda r: r.name.lower() == MOD_ROLE_NAME.lower(), guild.roles)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            self.winner: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }
        if mod_role:
            overwrites[mod_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        check_channel_name = f"check-{self.match_id}"
        check_channel = await guild.create_text_channel(
            name=check_channel_name,
            overwrites=overwrites,
            topic=f"Проверка игрока {self.winner.display_name} по матчу #{self.match_id}"
        )

        embed = discord.Embed(
            title="🔍 ВЫЗОВ НА ПРОВЕРКУ",
            description="Уважаемый игрок, у вас есть 24 часа на предоставление доказательств о том что вы чист.",
            color=discord.Color.orange()
        )

        view = CheckControlView(target_user=self.winner, match_id=self.match_id)
        await check_channel.send(content=self.winner.mention, embed=embed, view=view)

        await interaction.response.send_message(f"🚨 Игрок {self.winner.mention} вызван на проверку! Канал: {check_channel.mention}", ephemeral=True)

        await send_log(
            guild=interaction.guild,
            title="🚨 Лог: Вызов на проверку",
            description=f"**Модератор:** {interaction.user.mention}\n"
                        f"**Матч №:** `{self.match_id}` был отправлен на проверку\n"
                        f"**Игрок на проверке:** {self.winner.mention}\n"
                        f"**Канал проверки:** {check_channel.mention}",
            color=discord.Color.gold()
        )

# ---------------------------------------------------------------------------
# MODALS & VIEWS FOR MATCH
# ---------------------------------------------------------------------------

class YoutubeProofModal(discord.ui.Modal, title="Завершение дуэли"):
    youtube_url = discord.ui.TextInput(
        label="Ссылка на YouTube (доказательство)",
        placeholder="https://www.youtube.com/watch?v=...",
        required=True,
        min_length=10
    )

    def __init__(self, winner: discord.Member, players: list[discord.User], weapon: str, location: str, match_channel: discord.TextChannel, match_id: int):
        super().__init__()
        self.winner = winner
        self.players = players
        self.weapon = weapon
        self.location = location
        self.match_channel = match_channel
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        mod_channel = guild.get_channel(MOD_PROOF_CHANNEL_ID)

        if not mod_channel:
            await interaction.response.send_message("❌ Ошибка: Канал доказательств не найден! Обратитесь к администратору.", ephemeral=True)
            return

        players_mentions = []
        for p in self.players:
            players_mentions.append(p.mention)
        players_str = " vs ".join(players_mentions)

        embed = discord.Embed(
            title=f"📥 Новая заявка на проверку дуэли #{self.match_id}",
            color=discord.Color.gold()
        )
        embed.add_field(name="👥 Игроки", value=players_str, inline=False)
        embed.add_field(name="🏆 Победитель (заявлен)", value=self.winner.mention, inline=True)
        embed.add_field(name="🔫 Оружие", value=self.weapon, inline=True)
        embed.add_field(name="📍 Карта", value=self.location, inline=True)
        embed.add_field(name="🎥 Доказательство", value=self.youtube_url.value, inline=False)

        review_view = ModProofReviewView(
            winner=self.winner,
            players=self.players,
            weapon=self.weapon,
            location=self.location,
            proof_url=self.youtube_url.value,
            match_channel=self.match_channel,
            match_id=self.match_id
        )

        await mod_channel.send(embed=embed, view=review_view)
        await interaction.response.send_message("✅ Доказательство успешно отправлено модераторам! Ожидайте решения.", ephemeral=True)

class MatchCancelView(discord.ui.View):
    def __init__(self, allowed_users: list[discord.User]):
        super().__init__(timeout=300)
        self.allowed_users = allowed_users

    @discord.ui.button(label="Принять отмену", style=discord.ButtonStyle.success)
    async def accept_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_ids = [u.id for u in self.allowed_users]
        if interaction.user.id not in allowed_ids:
            await interaction.response.send_message("❌ Вы не можете подтвердить отмену!", ephemeral=True)
            return

        await interaction.response.send_message("⛔ **Матч отменён. Канал будет удалён через 7 секунд...**")
        self.stop()
        
        await asyncio.sleep(7)
        try:
            await interaction.channel.delete(reason="Матч отменен игроками")
        except Exception as e:
            print(f"Ошибка удаления: {e}")

    @discord.ui.button(label="Отклонить отмену", style=discord.ButtonStyle.secondary)
    async def decline_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        allowed_ids = [u.id for u in self.allowed_users]
        if interaction.user.id not in allowed_ids:
            await interaction.response.send_message("❌ Вы не можете отклонить отмену!", ephemeral=True)
            return

        await interaction.response.send_message("❌ **Запрос на отмену отклонён.** Продолжайте игру!", ephemeral=True)
        self.stop()

class ActiveMatchView(discord.ui.View):
    def __init__(self, players: list[discord.User], weapon: str, location: str, match_id: int):
        super().__init__(timeout=None)
        self.players = players
        self.weapon = weapon
        self.location = location
        self.match_id = match_id

    @discord.ui.button(label="Отмена матча", style=discord.ButtonStyle.danger, custom_id="active_match_cancel")
    async def request_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        player_ids = [p.id for p in self.players]
        if interaction.user.id not in player_ids:
            await interaction.response.send_message("❌ Вы не являетесь участником матча!", ephemeral=True)
            return

        opponents = []
        for p in self.players:
            if p.id != interaction.user.id:
                opponents.append(p)

        opponents_mentions = " ".join([p.mention for p in opponents])
        
        view = MatchCancelView(allowed_users=opponents)
        await interaction.response.send_message(
            f"⚠️ {interaction.user.mention} запросил отмену матча. {opponents_mentions}, вы согласны?",
            view=view
        )

    @discord.ui.button(label="Завершить дуэль", style=discord.ButtonStyle.success, custom_id="active_match_finish")
    async def finish_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        player_ids = [p.id for p in self.players]
        if interaction.user.id not in player_ids:
            await interaction.response.send_message("❌ Вы не являетесь участником матча!", ephemeral=True)
            return

        await interaction.response.send_modal(
            YoutubeProofModal(
                winner=interaction.user,
                players=self.players,
                weapon=self.weapon,
                location=self.location,
                match_channel=interaction.channel,
                match_id=self.match_id
            )
        )

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
        global match_counter
        invited_ids = [p.id for p in self.players if p.id != self.challenger.id]
        if interaction.user.id not in invited_ids:
            await interaction.response.send_message("❌ Вызов адресован не вам!", ephemeral=True)
            return

        guild = interaction.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_channels=True)
        }

        for player in self.players:
            overwrites[player] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

        current_match_id = match_counter
        channel_name = f"match-{current_match_id}"
        match_counter += 1
