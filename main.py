import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime
import discord
from discord import app_commands
from discord.ext import commands

# ==========================================
# ВЕБ-СЕРВЕР ДЛЯ БЕСПЛАТНОГО RENDER WEB SERVICE
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(b"Bot is active")

    def log_message(self, format, *args):
        return  # Не засоряем консоль логами веб-сервера

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

# Запускаем фоновый поток для Render
threading.Thread(target=run_web_server, daemon=True).start()

# ==========================================
# НАСТРОЙКИ И ПЕРЕМЕННЫЕ
# ==========================================
MOD_ROLE_NAME = "matchmaking mod"
HERALD_ROLE_NAME = "herald"  # Роль, выдаваемая при 100 Pts

MOD_PROOF_CHANNEL_ID = 1557114424576319598  # ID канала для проверки модераторами
LOG_CHANNEL_ID = 1557311338378694667        # ID канала логов

match_counter = 1
user_data = {}  # {user_id: {"pts": 0, "wins": 0, "losses": 0}}

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
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

# ==========================================
# МОДЕРАЦИЯ ПРУФОВ И РЕЗУЛЬТАТОВ
# ==========================================
class ModProofReviewView(discord.ui.View):
    def __init__(self, winner: discord.Member, players: list[discord.Member], match_channel: discord.TextChannel, match_id: int, proof_url: str):
        super().__init__(timeout=None)
        self.winner = winner
        self.players = players
        self.match_channel = match_channel
        self.match_id = match_id
        self.proof_url = proof_url

    @discord.ui.button(label="Одобрить", style=discord.ButtonStyle.success, custom_id="mod_approve_proof")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        stats = get_user_stats(self.winner.id)
        stats["pts"] += 15
        stats["wins"] += 1

        # Выдача роли herald при 100+ Pts
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

        players_mentions = ", ".join([p.mention for p in self.players])
        await send_log(
            guild=interaction.guild,
            title="✅ Лог: Матч одобрен",
            description=f"**Модератор:** {interaction.user.mention}\n"
                        f"**Матч №:** `{self.match_id}`\n"
                        f"**Участники:** {players_mentions}\n"
                        f"**Победитель:** {self.winner.mention} (+15 Pts)\n"
                        f"**Доказательство:** [Ссылка на скриншот]({self.proof_url})",
            color=discord.Color.green()
        )

        if self.match_channel:
            try:
                await self.match_channel.send("✅ **Матч одобрен! Канал будет удалён через 7 секунд...**")
                await asyncio.sleep(7)
                await self.match_channel.delete()
            except Exception as e:
                print(f"Ошибка удаления канала: {e}")

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, custom_id="mod_reject_proof")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.title = "❌ Результат Отклонён"

        for item in self.children:
            item.disabled = True
        await interaction.message.edit(embed=embed, view=self)

        await interaction.response.send_message("❌ Доказательство отклонено.", ephemeral=True)

        if self.match_channel:
            await self.match_channel.send("❌ **Модерация отклонила предоставленный скриншот/пруф.**")

# ==========================================
# МЕНЮ ВНУТРИ КАНАЛА МАТЧА (ОТПРАВКА ПРУФОВ)
# ==========================================
class SubmitProofModal(discord.ui.Modal, title="Отправка пруфа победы"):
    proof_url = discord.ui.TextInput(
        label="Ссылка на скриншот/видео (Imgur/Discord/etc.)",
        placeholder="https://...",
        required=True
    )

    def __init__(self, match_id: int, players: list[discord.Member]):
        super().__init__()
        self.match_id = match_id
        self.players = players

    async def on_submit(self, interaction: discord.Interaction):
        mod_channel = interaction.guild.get_channel(MOD_PROOF_CHANNEL_ID)
        if not mod_channel:
            await interaction.response.send_message("❌ Канал проверки модерацией не найден!", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔎 Проверка матча №{self.match_id}",
            color=discord.Color.gold(),
            timestamp=datetime.utcnow()
        )
        embed.add_field(name="Заявитель победы", value=interaction.user.mention, inline=True)
        embed.add_field(name="Участники", value=", ".join([p.mention for p in self.players]), inline=True)
        embed.add_field(name="Пруф", value=self.proof_url.value, inline=False)
        embed.set_image(url=self.proof_url.value)

        view = ModProofReviewView(
            winner=interaction.user,
            players=self.players,
            match_channel=interaction.channel,
            match_id=self.match_id,
            proof_url=self.proof_url.value
        )

        await mod_channel.send(embed=embed, view=view)
        await interaction.response.send_message("✅ Ваш пруф отправлен модераторам на проверку!", ephemeral=True)

class MatchChannelView(discord.ui.View):
    def __init__(self, match_id: int, players: list[discord.Member]):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.players = players

    @discord.ui.button(label="Отправить пруф победы", style=discord.ButtonStyle.primary, custom_id="submit_proof_btn")
    async def submit_proof(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.players:
            await interaction.response.send_message("❌ Вы не являетесь участником этого матча!", ephemeral=True)
            return
        await interaction.response.send_modal(SubmitProofModal(self.match_id, self.players))

# ==========================================
# ОСНОВНАЯ ПАНЕЛЬ МАТЧМЕЙКИНГА (ПОИСК ДУЭЛИ)
# ==========================================
class MatchmakingPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.waiting_player = None
        self.selected_weapon = "Любое"
        self.selected_location = "Любая"

    @discord.ui.select(
        placeholder="Выберите оружие...",
        options=[
            discord.SelectOption(label="Любое", value="Любое", default=True),
            discord.SelectOption(label="Меч / Shield", value="Меч / Shield"),
            discord.SelectOption(label="Лук / Bow", value="Лук / Bow"),
            discord.SelectOption(label="Топор / Axe", value="Топор / Axe")
        ],
        custom_id="select_weapon"
    )
    async def select_weapon(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_weapon = select.values[0]
        await interaction.response.send_message(f"Оружие выбрано: **{self.selected_weapon}**", ephemeral=True)

    @discord.ui.select(
        placeholder="Выберите локацию...",
        options=[
            discord.SelectOption(label="Любая", value="Любая", default=True),
            discord.SelectOption(label="Арена", value="Арена"),
            discord.SelectOption(label="Замок", value="Замок"),
            discord.SelectOption(label="Лес", value="Лес")
        ],
        custom_id="select_location"
    )
    async def select_location(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_location = select.values[0]
        await interaction.response.send_message(f"Локация выбрана: **{self.selected_location}**", ephemeral=True)

    @discord.ui.button(label="Вступить в поиск / Поиск дуэли", style=discord.ButtonStyle.success, custom_id="join_queue_btn")
    async def join_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        global match_counter

        if self.waiting_player is None:
            self.waiting_player = interaction.user
            await interaction.response.send_message("⏳ Вы встали в очередь! Ожидаем второго игрока...", ephemeral=True)
        elif self.waiting_player.id == interaction.user.id:
            await interaction.response.send_message("⚠️ Вы уже находитесь в очереди!", ephemeral=True)
        else:
            p1 = self.waiting_player
            p2 = interaction.user
            self.waiting_player = None

            guild = interaction.guild

            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                p1: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                p2: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }

            # Создаем только текстовый канал матча
            match_channel = await guild.create_text_channel(
                name=f"match-{match_counter}",
                overwrites=overwrites
            )

            p1_stats = get_user_stats(p1.id)
            p2_stats = get_user_stats(p2.id)

            embed = discord.Embed(
                title=f"⚔️ МАТЧ №{match_counter} НАЧАЛСЯ!",
                color=discord.Color.red(),
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="Игрок 1", value=f"{p1.mention} ({p1_stats['pts']} Pts)", inline=True)
            embed.add_field(name="Игрок 2", value=f"{p2.mention} ({p2_stats['pts']} Pts)", inline=True)
            embed.add_field(name="Оружие", value=self.selected_weapon, inline=False)
            embed.add_field(name="Локация", value=self.selected_location, inline=False)
            embed.set_footer(text="После завершения боя нажмите кнопку ниже и отправьте скриншот.")

            view = MatchChannelView(match_counter, [p1, p2])
            await match_channel.send(content=f"{p1.mention} {p2.mention}", embed=embed, view=view)

            await interaction.response.send_message(f"⚔️ Матч найден! Ваш канал: {match_channel.mention}", ephemeral=True)

            match_counter += 1

# ==========================================
# ИНИЦИАЛИЗАЦИЯ И КОМАНДЫ БОТА
# ==========================================
class MatchmakingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()

bot = MatchmakingBot()

@bot.tree.command(name="setup_panel", description="Отправить главную панель Matchmaking")
@is_matchmaking_mod()
async def setup_panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚔️ ПОИСК МАТЧА / MATCHMAKING",
        description="Выберите параметры дуэли и нажмите **'Вступить в поиск'**.\nБот создаст закрытый канал для вас и вашего оппонента.",
        color=discord.Color.blurple()
    )
    await interaction.channel.send(embed=embed, view=MatchmakingPanelView())
    await interaction.response.send_message("✅ Панель успешно создана!", ephemeral=True)

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
    embed.add_field(name="🏆 Pts", value=f"**{stats['pts']}**", inline=True)
    embed.add_field(name="⚔️ Игр", value=f"**{total_games}**", inline=True)
    embed.add_field(name="🗽 WR", value=f"**{winrate}%**", inline=True)
    embed.add_field(name="📈 Побед", value=f"**{stats['wins']}**", inline=True)
    embed.add_field(name="📉 Поражений", value=f"**{stats['losses']}**", inline=True)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Таблица лидеров по Pts")
async def leaderboard(interaction: discord.Interaction):
    if not user_data:
        await interaction.response.send_message("🏆 Таблица пуста.", ephemeral=True)
        return

    sorted_users = sorted(user_data.items(), key=lambda item: item[1]["pts"], reverse=True)[:10]
    embed = discord.Embed(title="🏆 ТАБЛИЦА ЛИДЕРОВ [TOP-10]", color=discord.Color.gold())

    lines = []
    for rank, (u_id, stats) in enumerate(sorted_users, start=1):
        member = interaction.guild.get_member(u_id)
        name = member.display_name if member else f"ID: {u_id}"
        lines.append(f"**#{rank} {name}** — `{stats['pts']} Pts` | Игр: `{stats['wins'] + stats['losses']}` | WR: `{calculate_winrate(stats['wins'], stats['losses'])}%`")

    embed.description = "\n".join(lines)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="fix_pts", description="Изменить количество Pts у игрока")
@is_matchmaking_mod()
async def fix_pts(interaction: discord.Interaction, user: discord.User, pts: int):
    stats = get_user_stats(user.id)
    old_pts = stats["pts"]
    stats["pts"] = pts

    if isinstance(user, discord.Member):
        await check_and_grant_herald_role(user, pts)

    await interaction.response.send_message(f"✅ Для {user.mention} установлено **{pts} Pts**.", ephemeral=True)

    await send_log(
        guild=interaction.guild,
        title="⚙️ Лог: Изменение Pts",
        description=f"**Модератор:** {interaction.user.mention}\n**Игрок:** {user.mention}\n**Было:** `{old_pts}` Pts | **Стало:** `{pts}` Pts",
        color=discord.Color.blue()
    )

# ==========================================
# ЗАПУСК БОТА
# ==========================================
if __name__ == "__main__":
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("❌ Ошибка: Переменная DISCORD_TOKEN не задана!")
        
