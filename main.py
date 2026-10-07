import os
import json
import asyncio
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from sqlalchemy import select, desc, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# Импорт моделей
from db_models import Base, User, Match, MatchStatus, RatingLog, calculate_pts_updates

# --- КОНФИГУРАЦИЯ ---
TOKEN = os.getenv("DISCORD_TOKEN")
MODERATION_CHANNEL_ID = 1557102218770120867  # ID закрытого канала модераторов
DATABASE_URL = "sqlite+aiosqlite:///matchmaking.db"

# Названия ролей
HERALD_ROLE_NAME = "herald"
MOD_ROLE_NAME = "matchmaking mod"

# --- НАСТРОЙКА БАЗЫ ДАННЫХ ---
engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_user(session: AsyncSession, discord_id: int) -> User:
    result = await session.execute(select(User).where(User.discord_id == discord_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(discord_id=discord_id)
        session.add(user)
        await session.commit()
    return user


async def check_and_assign_herald_role(guild: discord.Guild, user_id: int, rating: int):
    """Проверяет рейтинг и выдает роль Herald, если rating >= 100"""
    if rating >= 100 and guild:
        member = guild.get_member(user_id)
        if member:
            role = discord.utils.find(lambda r: r.name.lower() == HERALD_ROLE_NAME.lower(), guild.roles)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Достигнуто 100+ Pts")
                except discord.Forbidden:
                    print(f"Недостаточно прав для выдачи роли {role.name} пользователю {member.display_name}")
                except Exception as e:
                    print(f"Ошибка при выдаче роли: {e}")


# --- ПРОВЕРКА РОЛИ MATCHMAKING MOD ---
def has_matchmaking_mod_role():
    async def predicate(interaction: discord.Interaction) -> bool:
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return False
        has_role = any(r.name.lower() == MOD_ROLE_NAME.lower() for r in interaction.user.roles)
        if not has_role:
            await interaction.response.send_message(
                f"❌ У вас нет прав для использования этой команды (требуется роль `{MOD_ROLE_NAME}`).", 
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)


# --- MODAL ДЛЯ ОТКЛОНЕНИЯ ---
class RejectReasonModal(discord.ui.Modal, title="Отклонение результата матча"):
    reason = discord.ui.TextInput(
        label="Причина отклонения",
        style=discord.TextStyle.paragraph,
        placeholder="Укажите, почему результат не принят...",
        required=True,
        min_length=5,
        max_length=500,
    )

    def __init__(self, match_id: int):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()
        
        async with async_session() as session:
            stmt = select(Match).where(Match.id == self.match_id)
            res = await session.execute(stmt)
            match = res.scalar_one_or_none()
            
            if not match or match.status != MatchStatus.PENDING_MODERATION:
                await interaction.followup.send("Матч не найден или уже обработан.", ephemeral=True)
                return

            match.status = MatchStatus.CANCELLED
            await session.commit()

        # Обновляем сообщение модераторов
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(name="Статус", value=f"❌ Отклонено ({interaction.user.mention})\n**Причина:** {self.reason.value}", inline=False)
        
        # Отключаем кнопки
        disabled_view = discord.ui.View()
        for item in interaction.message.components:
            for child in item.children:
                btn = discord.ui.Button(label=child.label, style=child.style, disabled=True)
                disabled_view.add_item(btn)

        await interaction.message.edit(embed=embed, view=disabled_view)
        await interaction.followup.send(f"Матч #{self.match_id} успешно отклонён.", ephemeral=True)


# --- VIEW ДЛЯ МОДЕРАЦИИ ---
class ModerationView(discord.ui.View):
    def __init__(self, match_id: int, winner_team: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.winner_team = winner_team

    @discord.ui.button(label="Подтвердить", style=discord.ButtonStyle.success, custom_id="mod_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        async with async_session() as session:
            stmt = select(Match).where(Match.id == self.match_id)
            match = (await session.execute(stmt)).scalar_one_or_none()

            if not match or match.status != MatchStatus.PENDING_MODERATION:
                await interaction.followup.send("Матч не найден или уже обработан.", ephemeral=True)
                return

            # Загружаем участников
            t1_users = [(await get_or_create_user(session, uid)) for uid in match.team1]
            t2_users = [(await get_or_create_user(session, uid)) for uid in match.team2]

            t1_ratings = [u.rating for u in t1_users]
            t2_ratings = [u.rating for u in t2_users]

            delta_t1, delta_t2 = await calculate_pts_updates(t1_ratings, t2_ratings, self.winner_team)

            # Обновление Команды 1
            for u in t1_users:
                old_r = u.rating
                u.rating += delta_t1
                if self.winner_team == 1:
                    u.wins += 1
                else:
                    u.losses += 1
                session.add(RatingLog(match_id=match.id, user_id=u.discord_id, old_rating=old_r, new_rating=u.rating, delta=delta_t1))
                await check_and_assign_herald_role(interaction.guild, u.discord_id, u.rating)

            # Обновление Команды 2
            for u in t2_users:
                old_r = u.rating
                u.rating += delta_t2
                if self.winner_team == 2:
                    u.wins += 1
                else:
                    u.losses += 1
                session.add(RatingLog(match_id=match.id, user_id=u.discord_id, old_rating=old_r, new_rating=u.rating, delta=delta_t2))
                await check_and_assign_herald_role(interaction.guild, u.discord_id, u.rating)

            match.status = MatchStatus.COMPLETED
            await session.commit()

        # Обновление UI у модератора
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(
            name="Статус", 
            value=f"✅ Подтверждено ({interaction.user.mention})\n**Изменения Pts:** T1 ({delta_t1:+d}), T2 ({delta_t2:+d})", 
            inline=False
        )

        disabled_view = discord.ui.View()
        for child in self.children:
            child.disabled = True
            disabled_view.add_item(child)

        await interaction.message.edit(embed=embed, view=disabled_view)
        await interaction.followup.send(f"Матч #{self.match_id} успешно подтверждён!", ephemeral=True)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, custom_id="mod_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectReasonModal(self.match_id))


# --- VIEW ДЛЯ ПРИНЯТИЯ МАТЧА ---
class MatchAcceptView(discord.ui.View):
    def __init__(self, match_id: int, required_users: List[int]):
        super().__init__(timeout=300)
        self.match_id = match_id
        self.required_users = set(required_users)
        self.accepted_users = set()

    @discord.ui.button(label="Принять игру", style=discord.ButtonStyle.primary, emoji="⚔️")
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id not in self.required_users:
            await interaction.response.send_message("Вы не являетесь участником этого матча!", ephemeral=True)
            return

        if interaction.user.id in self.accepted_users:
            await interaction.response.send_message("Вы уже приняли игру.", ephemeral=True)
            return

        self.accepted_users.add(interaction.user.id)
        await interaction.response.send_message("Вы подтвердили участие!", ephemeral=True)

        # Проверка: все ли приняли?
        if self.accepted_users == self.required_users:
            self.stop()
            guild = interaction.guild
            category = interaction.channel.category if interaction.channel else None

            async with async_session() as session:
                stmt = select(Match).where(Match.id == self.match_id)
                match = (await session.execute(stmt)).scalar_one()
                match.status = MatchStatus.IN_PROGRESS

                # Настройка прав для нового текстового канала match-{id}
                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }

                # Добавляем участников матча
                for uid in self.required_users:
                    member = guild.get_member(uid)
                    if member:
                        overwrites[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                # Добавляем роль модератора
                mod_role = discord.utils.find(lambda r: r.name.lower() == MOD_ROLE_NAME.lower(), guild.roles)
                if mod_role:
                    overwrites[mod_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                # Создание текстового канала с названием match-айди
                channel_name = f"match-{match.id}"
                match_channel = await guild.create_text_channel(
                    name=channel_name,
                    category=category,
                    overwrites=overwrites
                )

                match.channel_id = match_channel.id
                await session.commit()

                await match_channel.send(
                    f"🎮 **Матч #{self.match_id} начался!**\nСостав участников готов. После завершения отправьте результат с помощью команды `/submit_result`."
                )

            # Обновляем эмбед сообщения лобби
            embed = interaction.message.embeds[0]
            embed.title = f"Матч #{self.match_id} — В процессе"
            embed.color = discord.Color.gold()
            embed.set_field_at(0, name="Статус", value=f"🟢 Все игроки приняли. Создан канал {match_channel.mention}!", inline=False)
            
            button.disabled = True
            button.label = "Игра началась"
            button.style = discord.ButtonStyle.success
            await interaction.message.edit(embed=embed, view=self)


# --- BOT CLIENT ---
class MatchmakingBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await init_db()
        await self.tree.sync()
        print("Команды синхронизированы и БД инициализирована.")


bot = MatchmakingBot()


# --- COMMANDS FOR MODERATORS ---

@bot.tree.command(name="deleted_channel", description="Удалить указанный канал (Только для Matchmaking Mod)")
@has_matchmaking_mod_role()
async def deleted_channel(interaction: discord.Interaction, channel_name_or_mention: str):
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("Эта команда доступна только на сервере.", ephemeral=True)
        return

    # Очищаем ввод от символов упоминания <#id>
    clean_input = channel_name_or_mention.strip("<#> ").lower()

    target_channel = None
    for ch in guild.channels:
        if str(ch.id) == clean_input or ch.name.lower() == clean_input:
            target_channel = ch
            break

    if not target_channel:
        await interaction.response.send_message(f"Канал `{channel_name_or_mention}` не найден.", ephemeral=True)
        return

    try:
        ch_name = target_channel.name
        await target_channel.delete(reason=f"Удалено пользователем {interaction.user.display_name} через /deleted_channel")
        await interaction.response.send_message(f"✅ Канал `#{ch_name}` успешно удалён.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ У бота недостаточно прав для удаления этого канала.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Произошла ошибка при удалении: {e}", ephemeral=True)


@bot.tree.command(name="fix_pts", description="Изменить Pts игрока (Только для Matchmaking Mod)")
@has_matchmaking_mod_role()
async def fix_pts(interaction: discord.Interaction, user: discord.User, new_pts: int):
    async with async_session() as session:
        db_user = await get_or_create_user(session, user.id)
        old_pts = db_user.rating
        delta = new_pts - old_pts
        
        db_user.rating = new_pts
        session.add(RatingLog(match_id=None, user_id=user.id, old_rating=old_pts, new_rating=new_pts, delta=delta))
        await session.commit()

    if interaction.guild:
        await check_and_assign_herald_role(interaction.guild, user.id, new_pts)

    await interaction.response.send_message(
        f"🛠️ Рейтинг <@{user.id}> успешно изменен: **{old_pts} Pts** ➔ **{new_pts} Pts** (Δ {delta:+d}).", 
        ephemeral=True
    )


# --- GENERAL SLASH COMMANDS ---

@bot.tree.command(name="create_match", description="Создать рейтинговый матч")
@app_commands.choices(mode=[
    app_commands.Choice(name="1v1", value="1v1"),
    app_commands.Choice(name="2v2", value="2v2")
])
async def create_match(
    interaction: discord.Interaction, 
    mode: app_commands.Choice[str], 
    opponent1: discord.User,
    teammate: Optional[discord.User] = None,
    opponent2: Optional[discord.User] = None
):
    if mode.value == "1v1":
        team1 = [interaction.user.id]
        team2 = [opponent1.id]
    else:  # 2v2
        if not teammate or not opponent2:
            await interaction.response.send_message("Для режима 2v2 необходимо указать всех участников (teammate, opponent1, opponent2)!", ephemeral=True)
            return
        team1 = [interaction.user.id, teammate.id]
        team2 = [opponent1.id, opponent2.id]

    all_players = team1 + team2
    if len(set(all_players)) != len(all_players):
        await interaction.response.send_message("В матче не может быть дублирующихся игроков!", ephemeral=True)
        return

    async with async_session() as session:
        for pid in all_players:
            await get_or_create_user(session, pid)

        match = Match(mode=mode.value, team1=team1, team2=team2, status=MatchStatus.PENDING_ACCEPT)
        session.add(match)
        await session.commit()
        match_id = match.id

    t1_mentions = ", ".join([f"<@{uid}>" for uid in team1])
    t2_mentions = ", ".join([f"<@{uid}>" for uid in team2])

    embed = discord.Embed(
        title=f"Создано лобби матча #{match_id} [{mode.value}]",
        color=discord.Color.blue()
    )
    embed.add_field(name="Статус", value="⏳ Ожидание подтверждения участников...", inline=False)
    embed.add_field(name="Команда 1", value=t1_mentions, inline=True)
    embed.add_field(name="Команда 2", value=t2_mentions, inline=True)
    embed.set_footer(text="Все участники должны нажать кнопку ниже.")

    view = MatchAcceptView(match_id=match_id, required_users=all_players)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="submit_result", description="Отправить скриншот-подтверждение и итоговый результат")
async def submit_result(
    interaction: discord.Interaction, 
    match_id: int, 
    winner_team: int, 
    proof: discord.Attachment
):
    if winner_team not in (1, 2):
        await interaction.response.send_message("Укажите победившую команду: 1 или 2.", ephemeral=True)
        return

    if not proof.content_type or not proof.content_type.startswith("image/"):
        await interaction.response.send_message("Файл доказательства должен быть изображением!", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    async with async_session() as session:
        stmt = select(Match).where(Match.id == match_id)
        match = (await session.execute(stmt)).scalar_one_or_none()

        if not match:
            await interaction.followup.send("Матч с таким ID не найден.", ephemeral=True)
            return

        if interaction.user.id not in match.team1 and interaction.user.id not in match.team2:
            await interaction.followup.send("Вы не являетесь участником данного матча.", ephemeral=True)
            return

        if match.status != MatchStatus.IN_PROGRESS:
            await interaction.followup.send(f"Нельзя отправить результат для матча в статусе {match.status.value}.", ephemeral=True)
            return

        match.status = MatchStatus.PENDING_MODERATION
        match.proof_url = proof.url
        await session.commit()

    # Отправка модераторам
    mod_channel = interaction.guild.get_channel(MODERATION_CHANNEL_ID)
    if mod_channel:
        mod_embed = discord.Embed(
            title=f"Модерация результатов: Матч #{match_id}",
            color=discord.Color.orange()
        )
        mod_embed.add_field(name="Отправитель", value=interaction.user.mention, inline=False)
        mod_embed.add_field(name="Заявленный победитель", value=f"Команда {winner_team}", inline=True)
        mod_embed.add_field(name="Команда 1", value=", ".join([f"<@{u}>" for u in match.team1]), inline=False)
        mod_embed.add_field(name="Команда 2", value=", ".join([f"<@{u}>" for u in match.team2]), inline=False)
        mod_embed.set_image(url=proof.url)

        view = ModerationView(match_id=match_id, winner_team=winner_team)
        await mod_channel.send(embed=mod_embed, view=view)

    await interaction.followup.send("Результат отправлен на проверку модераторам!", ephemeral=True)


@bot.tree.command(name="profile", description="Просмотр профиля и рейтинга игрока")
async def profile(interaction: discord.Interaction, user: Optional[discord.User] = None):
    target = user or interaction.user

    async with async_session() as session:
        db_user = await get_or_create_user(session, target.id)
        
        # Расчет места в ладдере
        rank_stmt = select(func.count()).where(User.rating > db_user.rating)
        rank_res = await session.execute(rank_stmt)
        rank = rank_res.scalar_one() + 1

    total_games = db_user.wins + db_user.losses
    winrate = (db_user.wins / total_games * 100) if total_games > 0 else 0.0

    embed = discord.Embed(title=f"Профиль {target.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Рейтинг Pts", value=f"🏆 **{db_user.rating} Pts** (Место: #{rank})", inline=False)
    embed.add_field(name="Победы / Поражения", value=f"📈 {db_user.wins} / 📉 {db_user.losses}", inline=True)
    embed.add_field(name="Винрейт", value=f"🎯 {winrate:.1f}%", inline=True)

    await interaction.response.send_message(embed=embed)

