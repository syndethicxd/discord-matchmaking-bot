import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# HTTP SERVER FOR RENDER (Поддержка работы на Render Web Service)
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
        server.serve_forever()
    except Exception as e:
        print(f"HTTP Server Exception: {e}")

# ---------------------------------------------------------------------------
# DATA & IN-MEMORY DATABASE
# ---------------------------------------------------------------------------

match_counter = 1  # Глобальный счетчик для нумерации дуэлей (match-1, match-2...)

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

def is_matchmaking_mod():
    async def predicate(interaction: discord.Interaction):
        has_role = any(role.name == "Matchmaking Mod" for role in interaction.user.roles)
        if not has_role:
            await interaction.response.send_message("❌ У вас нет роли **Matchmaking Mod** для использования этой команды!", ephemeral=True)
            return False
        return True
    return app_commands.check(predicate)

# ---------------------------------------------------------------------------
# MODALS & VIEWS
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
        global match_counter
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

        channel_name = f"match-{match_counter}"
        match_counter += 1

        match_channel = await guild.create_text_channel(name=channel_name, overwrites=overwrites)

        embed = discord.Embed(
            title=f"⚔️ Матч #{match_counter - 1} начался! [{self.mode}]",
            description=f"**Участники:** {' vs '.join([p.mention for p in self.players])}\n"
                        f"📍 **Локация:** {self.location}\n"
                        f"🔫 **Оружие:** {self.weapon}",
            color=discord.Color.green()
        )

        active_view = ActiveMatchView(players=self.players)
        await match_channel.send(content=" ".join([p.mention for p in self.players]), embed=embed, view=active_view)

        await interaction.response.send_message(f"✅ Матч создан! Перейдите в канал {match_channel.mention}")
        self.stop()

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in [p.id for p in self.players if p.id != self.challenger.id]:
            await interaction.response.send_message("❌ Этот вызов адресован не вам!", ephemeral=True)
            return

        await interaction.response.send_message(f"❌ {interaction.user.mention} отклонил вызов на матч.")
        self.stop()

# ---------------------------------------------------------------------------
# MATCH SETUP VIEWS
# ---------------------------------------------------------------------------

class Setup1v1View(discord.ui.View):
    def __init__(self, challenger: discord.User):
        super().__init__(timeout=180)
        self.challenger = challenger
        self.opponent_id = None
        self.location = None
        self.weapon = None

        user_select = discord.ui.UserSelect(placeholder="Выберите противника...", min_values=1, max_values=1, row=0)
        user_select.callback = self.user_cb
        self.add_item(user_select)

        loc_select = discord.ui.Select(
            placeholder="Выберите локацию...",
            options=[discord.SelectOption(label="ЛСП", value="ЛСП"), discord.SelectOption(label="СВАЛКА", value="СВАЛКА")],
            row=1
        )
        loc_select.callback = self.loc_cb
        self.add_item(loc_select)

        wpn_select = discord.ui.Select(
            placeholder="Выберите оружие...",
            options=[discord.SelectOption(label="ДИГЛ", value="ДИГЛ"), discord.SelectOption(label="М4", value="М4")],
            row=2
        )
        wpn_select.callback = self.wpn_cb
        self.add_item(wpn_select)

    async def user_cb(self, interaction: discord.Interaction):
        self.opponent_id = interaction.data["values"][0]
        await interaction.response.defer()

    async def loc_cb(self, interaction: discord.Interaction):
        self.location = interaction.data["values"][0]
        await interaction.response.defer()

    async def wpn_cb(self, interaction: discord.Interaction):
        self.weapon = interaction.data["values"][0]
        await interaction.response.defer()

    @discord.ui.button(label="Отправить вызов 1v1", style=discord.ButtonStyle.primary, row=3)
    async def send_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.opponent_id or not self.location or not self.weapon:
            await interaction.response.send_message("❌ Выберите противника, локацию и оружие!", ephemeral=True)
            return

        opponent = interaction.guild.get_member(int(self.opponent_id))
        if not opponent or opponent.id == interaction.user.id:
            await interaction.response.send_message("❌ Укажите корректного противника!", ephemeral=True)
            return

        players = [interaction.user, opponent]
        embed = discord.Embed(
            title="⚔️ ВЫЗОВ НА ДУЭЛЬ [1v1]",
            description=f"{interaction.user.mention} вызывает на дуэль {opponent.mention}!\n\n"
                        f"📍 **Локация:** {self.location}\n"
                        f"🔫 **Оружие:** {self.weapon}",
            color=discord.Color.gold()
        )

        invite_view = MatchInviteView(challenger=interaction.user, players=players, location=self.location, weapon=self.weapon, mode="1v1")
        await interaction.channel.send(content=opponent.mention, embed=embed, view=invite_view)
        await interaction.response.send_message("✅ Вызов успешно отправлен!", ephemeral=True)

class Setup2v2View(discord.ui.View):
    def __init__(self, challenger: discord.User):
        super().__init__(timeout=180)
        self.challenger = challenger
        self.teammate_id = None
        self.opponents_ids = []
        self.location = None
        self.weapon = None

        tm_select = discord.ui.UserSelect(placeholder="Выберите вашего ТИММЕЙТА...", min_values=1, max_values=1, row=0)
        tm_select.callback = self.tm_cb
        self.add_item(tm_select)

        opp_select = discord.ui.UserSelect(placeholder="Выберите 2-х ПРОТИВНИКОВ...", min_values=2, max_values=2, row=1)
        opp_select.callback = self.opp_cb
        self.add_item(opp_select)

        loc_select = discord.ui.Select(
            placeholder="Выберите локацию...",
            options=[discord.SelectOption(label="ЛСП", value="ЛСП"), discord.SelectOption(label="СВАЛКА", value="СВАЛКА")],
            row=2
        )
        loc_select.callback = self.loc_cb
        self.add_item(loc_select)

        wpn_select = discord.ui.Select(
            placeholder="Выберите оружие...",
            options=[discord.SelectOption(label="ДИГЛ", value="ДИГЛ"), discord.SelectOption(label="М4", value="М4")],
            row=3
        )
        wpn_select.callback = self.wpn_cb
        self.add_item(wpn_select)

    async def tm_cb(self, interaction: discord.Interaction):
        self.teammate_id = interaction.data["values"][0]
        await interaction.response.defer()

    async def opp_cb(self, interaction: discord.Interaction):
        self.opponents_ids = interaction.data["values"]
        await interaction.response.defer()

    async def loc_cb(self, interaction: discord.Interaction):
        self.location = interaction.data["values"][0]
        await interaction.response.defer()

    async def wpn_cb(self, interaction: discord.Interaction):
        self.weapon = interaction.data["values"][0]
        await interaction.response.defer()

    @discord.ui.button(label="Отправить вызов 2v2", style=discord.ButtonStyle.primary, row=4)
    async def send_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.teammate_id or len(self.opponents_ids) != 2 or not self.location or not self.weapon:
            await interaction.response.send_message("❌ Заполните все поля (1 тиммейт, 2 противника, локация и оружие)!", ephemeral=True)
            return

        teammate = interaction.guild.get_member(int(self.teammate_id))
        opp1 = interaction.guild.get_member(int(self.opponents_ids[0]))
        opp2 = interaction.guild.get_member(int(self.opponents_ids[1]))

        if not teammate or not opp1 or not opp2:
            await interaction.response.send_message("❌ Не удалось найти всех указанных игроков на сервере.", ephemeral=True)
            return

        players = [interaction.user, teammate, opp1, opp2]
        opponents_mentions = f"{opp1.mention} и {opp2.mention}"

        embed = discord.Embed(
            title="⚔️ ВЫЗОВ НА ДУЭЛЬ [2v2]",
            description=f"Команда {interaction.user.mention} и {teammate.mention}\n"
                        f"Вызывает команду {opponents_mentions}!\n\n"
                        f"📍 **Локация:** {self.location}\n"
                        f"🔫 **Оружие:** {self.weapon}",
            color=discord.Color.gold()
        )

        invite_view = MatchInviteView(challenger=interaction.user, players=players, location=self.location, weapon=self.weapon, mode="2v2")
        await interaction.channel.send(content=f"{opp1.mention} {opp2.mention}", embed=embed, view=invite_view)
        await interaction.response.send_message("✅ Вызов на 2v2 успешно отправлен!", ephemeral=True)

# ---------------------------------------------------------------------------
# MAIN PANEL VIEW
# ---------------------------------------------------------------------------

class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⚔️ 1v1", style=discord.ButtonStyle.primary, custom_id="main_panel_1v1")
    async def button_1v1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚙️ **Настройка матча 1v1:**", view=Setup1v1View(interaction.user), ephemeral=True)

    @discord.ui.button(label="⚔️ 2v2", style=discord.ButtonStyle.success, custom_id="main_panel_2v2")
    async def button_2v2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("⚙️ **Настройка матча 2v2:**", view=Setup2v2View(interaction.user), ephemeral=True)

    @discord.ui.button(label="📊 Моя статистика", style=discord.ButtonStyle.secondary, custom_id="main_panel_my_stats")
    async def button_my_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        stats = get_user_stats(interaction.user.id)
        total_games = stats["wins"] + stats["losses"]
        winrate = calculate_winrate(stats["wins"], stats["losses"])

        embed = discord.Embed(
            title=f"📊 Профиль {interaction.user.display_name}",
            color=discord.Color.blue()
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)
        embed.add_field(name="🏆 PTS", value=f"**{stats['pts']}**", inline=True)
        embed.add_field(name="⚔️ Сыграно игр", value=f"**{total_games}**", inline=True)
        embed.add_field(name="📈 Винрейт", value=f"**{winrate}%**", inline=True)
        embed.add_field(name="✅ Победы", value=f"**{stats['wins']}**", inline=True)
        embed.add_field(name="❌ Поражения", value=f"**{stats['losses']}**", inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------------------------------------------------------------------------
# BOT INITIALIZATION
# ---------------------------------------------------------------------------

class MatchmakingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        self.add_view(MainPanelView())
        await self.tree.sync()
        print(">>> КОМАНДЫ И ПАНЕЛИ УСПЕШНО СИНХРОНИЗИРОВАНЫ <<<")

bot = MatchmakingBot()

# ---------------------------------------------------------------------------
# SLASH COMMANDS
# ---------------------------------------------------------------------------

@bot.tree.command(name="panel", description="Вызвать главную панель матчмейкинга")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎮 Панель Матчмейкинга",
        description="Выберите нужное действие с помощью кнопок ниже:",
        color=discord.Color.dark_purple()
    )
    await interaction.response.send_message(embed=embed, view=MainPanelView())

@bot.tree.command(name="profile", description="Просмотреть профиль игрока")
async def profile(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    stats = get_user_stats(target.id)
    total_games = stats["wins"] + stats["losses"]
    winrate = calculate_winrate(stats["wins"], stats["losses"])

    embed = discord.Embed(
        title=f"📊 Профиль {target.display_name}",
        color=discord.Color.blue()
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="🏆 PTS", value=f"**{stats['pts']}**", inline=True)
    embed.add_field(name="⚔️ Всего игр", value=f"**{total_games}**", inline=True)
    embed.add_field(name="📈 Винрейт", value=f"**{winrate}%**", inline=True)
    embed.add_field(name="✅ Побед", value=f"**{stats['wins']}**", inline=True)
    embed.add_field(name="❌ Поражений", value=f"**{stats['losses']}**", inline=True)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Таблица лидеров сервера")
async def leaderboard(interaction: discord.Interaction):
    if not user_data:
        await interaction.response.send_message("🏆 **Таблица лидеров пока пуста.**", ephemeral=True)
        return

    sorted_users = sorted(user_data.items(), key=lambda item: item[1]["pts"], reverse=True)[:10]

    embed = discord.Embed(
        title="🏆 ТАБЛИЦА ЛИДЕРОВ [TOP-10]",
        color=discord.Color.gold()
    )

    description_text = ""
    for rank, (u_id, stats) in enumerate(sorted_users, start=1):
        member = interaction.guild.get_member(u_id)
        name = member.display_name if member else f"User ID: {u_id}"
        total_games = stats["wins"] + stats["losses"]
        winrate = calculate_winrate(stats["wins"], stats["losses"])
        description_text += f"**#{rank} {name}** — `{stats['pts']} PTS` | Игр: `{total_games}` | WR: `{winrate}%`\n"

    embed.description = description_text
    await interaction.response.send_message(embed=embed)
