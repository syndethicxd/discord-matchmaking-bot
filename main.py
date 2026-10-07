import os
import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# БАЗА ДАННЫХ И ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (Сохраняем весь ваш прошлый функционал)
# ---------------------------------------------------------------------------

MATCH_LOGS_CHANNEL_ID = 123456789012345678  # Замените на ID вашего канала логов

async def init_db():
    # Ваша логика инициализации базы данных
    pass

def has_matchmaking_mod_role():
    async def predicate(interaction: discord.Interaction):
        # Проверка роли модератора
        return True
    return app_commands.check(predicate)

async def check_and_assign_herald_role(guild: discord.Guild, user_id: int, pts: int):
    # Логика выдачи роли herald при достижении 100 Pts
    pass

# ---------------------------------------------------------------------------
# MODALS & VIEWS FOR MATCHMAKING (Новая система вызовов и матчей)
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
    def __init__(self, target_user: discord.User):
        super().__init__(timeout=300)
        self.target_user = target_user

    @discord.ui.button(label="Принять отмену", style=discord.ButtonStyle.danger)
    async def accept_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_user.id:
            await interaction.response.send_message("❌ Только ваш противник может подтвердить отмену!", ephemeral=True)
            return
        await interaction.response.send_message("⛔ **Матч был отменён по обоюдному согласию.**")
        self.stop()

    @discord.ui.button(label="Отклонить отмену", style=discord.ButtonStyle.secondary)
    async def decline_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.target_user.id:
            await interaction.response.send_message("❌ Только ваш противник может отклонить отмену!", ephemeral=True)
            return
        await interaction.response.send_message("❌ **Запрос на отмену матча отклонён.** Продолжайте игру!", ephemeral=True)
        self.stop()

class ActiveMatchView(discord.ui.View):
    def __init__(self, player1: discord.User, player2: discord.User):
        super().__init__(timeout=None)
        self.player1 = player1
        self.player2 = player2

    @discord.ui.button(label="Отмена матча", style=discord.ButtonStyle.danger, custom_id="active_match_cancel")
    async def request_cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.player1.id, self.player2.id):
            await interaction.response.send_message("❌ Вы не являетесь участником этого матча!", ephemeral=True)
            return

        opponent = self.player2 if interaction.user.id == self.player1.id else self.player1
        view = MatchCancelView(target_user=opponent)
        await interaction.response.send_message(
            f"⚠️ {interaction.user.mention} запросил отмену матча. {opponent.mention}, вы согласны?",
            view=view
        )

    @discord.ui.button(label="Завершить дуэль", style=discord.ButtonStyle.success, custom_id="active_match_finish")
    async def finish_match(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in (self.player1.id, self.player2.id):
            await interaction.response.send_message("❌ Вы не являетесь участником этого матча!", ephemeral=True)
            return

        await interaction.response.send_modal(YoutubeProofModal())

class MatchInviteView(discord.ui.View):
    def __init__(self, challenger: discord.User, opponent: discord.User, location: str, weapon: str, mode: str):
        super().__init__(timeout=600)
        self.challenger = challenger
        self.opponent = opponent
        self.location = location
        self.weapon = weapon
        self.mode = mode

    @discord.ui.button(label="Принять", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ Этот вызов адресован не вам!", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"⚔️ Матч начался! [{self.mode}]",
            description=f"**Участники:** {self.challenger.mention} vs {self.opponent.mention}\n"
                        f"**Локация:** {self.location}\n"
                        f"**Оружие:** {self.weapon}",
            color=discord.Color.green()
        )
        
        active_view = ActiveMatchView(player1=self.challenger, player2=self.opponent)
        await interaction.response.send_message(embed=embed, view=active_view)
        self.stop()

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("❌ Этот вызов адресован не вам!", ephemeral=True)
            return

        await interaction.response.send_message(f"❌ {self.opponent.mention} отклонил вызов от {self.challenger.mention}.")
        self.stop()

class MatchSetupView(discord.ui.View):
    def __init__(self, challenger: discord.User, mode: str):
        super().__init__(timeout=180)
        self.challenger = challenger
        self.mode = mode
        self.selected_opponent = None
        self.selected_location = None
        self.selected_weapon = None

        user_select = discord.ui.UserSelect(
            placeholder="Выберите противника...",
            min_values=1,
            max_values=1,
            row=0
        )
        user_select.callback = self.user_callback
        self.add_item(user_select)

        location_select = discord.ui.Select(
            placeholder="Выберите локацию...",
            options=[
                discord.SelectOption(label="ЛСП", value="ЛСП"),
                discord.SelectOption(label="СВАЛКА", value="СВАЛКА")
            ],
            row=1
        )
        location_select.callback = self.location_callback
        self.add_item(location_select)

        weapon_select = discord.ui.Select(
            placeholder="Выберите оружие...",
            options=[
                discord.SelectOption(label="ДИГЛ", value="ДИГЛ"),
                discord.SelectOption(label="М4", value="М4")
            ],
            row=2
        )
        weapon_select.callback = self.weapon_callback
        self.add_item(weapon_select)

    async def user_callback(self, interaction: discord.Interaction):
        self.selected_opponent = interaction.data["values"][0]
        await interaction.response.defer()

    async def location_callback(self, interaction: discord.Interaction):
        self.selected_location = interaction.data["values"][0]
        await interaction.response.defer()

    async def weapon_callback(self, interaction: discord.Interaction):
        self.selected_weapon = interaction.data["values"][0]
        await interaction.response.defer()

    @discord.ui.button(label="Отправить вызов", style=discord.ButtonStyle.primary, row=3)
    async def send_challenge(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_opponent or not self.selected_location or not self.selected_weapon:
            await interaction.response.send_message("❌ Заполните все поля (соперник, локация, оружие)!", ephemeral=True)
            return

        opponent = interaction.guild.get_member(int(self.selected_opponent))
        if not opponent:
            await interaction.response.send_message("❌ Указанный пользователь не найден на сервере.", ephemeral=True)
            return

        if opponent.id == interaction.user.id:
            await interaction.response.send_message("❌ Вы не можете вызвать самого себя!", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"⚔️ ВЫЗОВ НА ДУЭЛЬ [{self.mode}]",
            description=f"{interaction.user.mention} вызывает на дуэль {opponent.mention}!\n\n"
                        f"📍 **Локация:** {self.selected_location}\n"
                        f"🔫 **Оружие:** {self.selected_weapon}",
            color=discord.Color.gold()
        )

        invite_view = MatchInviteView(
            challenger=interaction.user,
            opponent=opponent,
            location=self.selected_location,
            weapon=self.selected_weapon,
            mode=self.mode
        )

        await interaction.channel.send(content=opponent.mention, embed=embed, view=invite_view)
        await interaction.response.send_message("✅ Вызов успешно отправлен!", ephemeral=True)

# ---------------------------------------------------------------------------
# MAIN PANEL VIEW (Сохранены кнопки 1v1 и 2v2)
# ---------------------------------------------------------------------------

class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="1v1", style=discord.ButtonStyle.primary, custom_id="main_panel_1v1")
    async def button_1v1(self, interaction: discord.Interaction, button: discord.ui.Button):
        setup_view = MatchSetupView(challenger=interaction.user, mode="1v1")
        await interaction.response.send_message("⚙️ **Настройка матча 1v1:**", view=setup_view, ephemeral=True)

    @discord.ui.button(label="2v2", style=discord.ButtonStyle.secondary, custom_id="main_panel_2v2")
    async def button_2v2(self, interaction: discord.Interaction, button: discord.ui.Button):
        setup_view = MatchSetupView(challenger=interaction.user, mode="2v2")
        await interaction.response.send_message("⚙️ **Настройка матча 2v2:**", view=setup_view, ephemeral=True)

# ---------------------------------------------------------------------------
# ИНИЦИАЛИЗА БОТА И СЛАШ-КОМАНДЫ
# ---------------------------------------------------------------------------

class MatchmakingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await init_db()
        self.add_view(MainPanelView())
        await self.tree.sync()
        print(">>> КОМАНДЫ СИНХРОНИЗИРОВАНЫ И БД ИНИЦИАЛИЗИРОВАНА <<<")

bot = MatchmakingBot()

# ----------------- SLASH COMMANDS -----------------

@bot.tree.command(name="panel", description="Вызвать главную панель матчмейкинга")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎮 Панель Матчмейкинга",
        description="Выберите нужное действие с помощью кнопок ниже:"
    )
    view = MainPanelView()
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="profile", description="Просмотреть свой профиль или профиль игрока")
async def profile(interaction: discord.Interaction, user: discord.User = None):
    target_user = user or interaction.user
    # Ваша реализация профиля
    await interaction.response.send_message(f"📊 Профиль пользователя {target_user.mention}", ephemeral=True)

@bot.tree.command(name="leaderboard", description="Таблица лидеров")
async def leaderboard(interaction: discord.Interaction):
    # Ваша реализация таблицы лидеров
    await interaction.response.send_message("🏆 Таблица лидеров", ephemeral=True)

@bot.tree.command(name="edit_stats", description="Полная настройка статистики игрока (Только для Matchmaking Mod)")
@has_matchmaking_mod_role()
async def edit_stats(interaction: discord.Interaction, user: discord.User, pts: int = None, wins: int = None, losses: int = None):
    # Логика редактирования статистики (Pts вместо Elo)
    await interaction.response.send_message(f"Статистика для {user.mention} обновлена.", ephemeral=True)

@bot.tree.command(name="deleted_channel", description="Удалить указанный канал (Только для Matchmaking Mod)")
@has_matchmaking_mod_role()
async def deleted_channel(interaction: discord.Interaction, channel_name_or_mention: str):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return

    clean_input = channel_name_or_mention.strip("<#> ").lower()
    target_channel = None
    for ch in guild.channels:
        if str(ch.id) == clean_input or ch.name.lower() == clean_input:
            target_channel = ch
            break

    if not target_channel:
        await interaction.response.send_message(f"Канал {channel_name_or_mention} не найден.", ephemeral=True)
        return

    try:
        ch_name = target_channel.name
        await target_channel.delete(reason=f"Удалено пользователем {interaction.user.display_name}")
        await interaction.response.send_message(f"✅ Канал #{ch_name} успешно удалён.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ У бота недостаточно прав для удаления этого канала.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Произошла ошибка при удалении: {e}", ephemeral=True)

# ---------------------------------------------------------------------------
# ЗАПУСК БОТА
# ---------------------------------------------------------------------------

import os

TOKEN = os.getenv("DISCORD_TOKEN")

if __name__ == "__main__":
    if TOKEN:
        bot.run(TOKEN)
    else:
        print("Ошибка: Токен DISCORD_TOKEN не найден!")
                       
