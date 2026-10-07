import os
import json
import asyncio
import traceback
from enum import Enum
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

from sqlalchemy import Column, Integer, String, JSON, Enum as SQLEnum, select, desc, func
from sqlalchemy.orm import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# ================= CONFIGURATION =================
TOKEN = os.getenv("DISCORD_TOKEN")
MODERATION_CHANNEL_ID = 1557102218770120867
MATCH_LOGS_CHANNEL_ID = 1557102218770120867
DATABASE_URL = "sqlite+aiosqlite:///matchmaking.db"

HERALD_ROLE_NAME = "herald"
MOD_ROLE_NAME = "matchmaking mod"

# ================= DATABASE SETUP =================
Base = declarative_base()

class MatchStatus(str, Enum):
    PENDING_ACCEPT = "PENDING_ACCEPT"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_MODERATION = "PENDING_MODERATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    discord_id = Column(Integer, unique=True, nullable=False, index=True)
    rating = Column(Integer, default=0, nullable=False)
    wins = Column(Integer, default=0, nullable=False)
    losses = Column(Integer, default=0, nullable=False)

class Match(Base):
    __tablename__ = "matches"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mode = Column(String, nullable=False)
    team1 = Column(JSON, nullable=False)
    team2 = Column(JSON, nullable=False)
    status = Column(SQLEnum(MatchStatus), default=MatchStatus.PENDING_ACCEPT, nullable=False)
    channel_id = Column(Integer, nullable=True)
    proof_url = Column(String, nullable=True)

class RatingLog(Base):
    __tablename__ = "rating_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    match_id = Column(Integer, nullable=True)
    user_id = Column(Integer, nullable=False)
    old_rating = Column(Integer, nullable=False)
    new_rating = Column(Integer, nullable=False)
    delta = Column(Integer, nullable=False)

engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, expire_on_commit=False)

async def calculate_pts_updates(t1_ratings: List[int], t2_ratings: List[int], winner_team: int):
    avg_t1 = sum(t1_ratings) / len(t1_ratings) if t1_ratings else 0
    avg_t2 = sum(t2_ratings) / len(t2_ratings) if t2_ratings else 0
    
    base_gain = 25
    diff = abs(avg_t1 - avg_t2)
    adjustment = int(diff / 50)
    
    if winner_team == 1:
        if avg_t1 >= avg_t2:
            delta_t1 = max(10, base_gain - adjustment)
            delta_t2 = -delta_t1
        else:
            delta_t1 = base_gain + adjustment
            delta_t2 = -delta_t1
    else:
        if avg_t2 >= avg_t1:
            delta_t2 = max(10, base_gain - adjustment)
            delta_t1 = -delta_t2
        else:
            delta_t2 = base_gain + adjustment
            delta_t1 = -delta_t2
            
    return delta_t1, delta_t2

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
    if rating >= 100 and guild:
        member = guild.get_member(user_id)
        if member:
            role = discord.utils.find(lambda r: r.name.lower() == HERALD_ROLE_NAME.lower(), guild.roles)
            if role and role not in member.roles:
                try:
                    await member.add_roles(role, reason="Достигнуто 100+ Pts")
                except Exception as e:
                    print(f"Ошибка при выдаче роли: {e}")

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

async def build_profile_embed(target_user: discord.User) -> discord.Embed:
    async with async_session() as session:
        db_user = await get_or_create_user(session, target_user.id)
        rank_stmt = select(func.count()).where(User.rating > db_user.rating)
        rank_res = await session.execute(rank_stmt)
        rank = rank_res.scalar_one() + 1

    total_games = db_user.wins + db_user.losses
    winrate = (db_user.wins / total_games * 100) if total_games > 0 else 0.0

    embed = discord.Embed(title=f"📊 Профиль {target_user.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=target_user.display_avatar.url)
    embed.add_field(name="Рейтинг Pts", value=f"🏆 **{db_user.rating} Pts** (Место: #{rank})", inline=False)
    embed.add_field(name="Победы / Поражения", value=f"📈 {db_user.wins} / 📉 {db_user.losses}", inline=True)
    embed.add_field(name="Винрейт", value=f"🎯 {winrate:.1f}%", inline=True)
    return embed

# ================= UI VIEWS =================
class RejectReasonModal(discord.ui.Modal, title="Отклонение результата матча"):
    reason = discord.ui.TextInput(
        label="Причина отклонения",
        style=discord.TextStyle.paragraph,
        placeholder="Укажите, почему результат не принят...",
        required=True,
        min_length=3,
        max_length=500,
    )

    def __init__(self, match_id: int):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with async_session() as session:
            stmt = select(Match).where(Match.id == self.match_id)
            res = await session.execute(stmt)
            match = res.scalar_one_or_none()
            if not match or match.status != MatchStatus.PENDING_MODERATION:
                await interaction.followup.send("Матч не найден или уже обработан.", ephemeral=True)
                return
            match.status = MatchStatus.CANCELLED
            await session.commit()

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(name="Статус", value=f"❌ Отклонено ({interaction.user.mention})\n**Причина:** {self.reason.value}", inline=False)
        
        disabled_view = discord.ui.View()
        for item in interaction.message.components:
            for child in item.children:
                btn = discord.ui.Button(label=child.label, style=child.style, disabled=True)
                disabled_view.add_item(btn)

        await interaction.message.edit(embed=embed, view=disabled_view)
        await interaction.followup.send(f"Матч #{self.match_id} успешно отклонён.", ephemeral=True)

class ModerationView(discord.ui.View):
    def __init__(self, match_id: int, winner_team: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.winner_team = winner_team

    @discord.ui.button(label="Подтвердить", style=discord.ButtonStyle.success, custom_id="mod_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        async with async_session() as session:
            stmt = select(Match).where(Match.id == self.match_id)
            match = (await session.execute(stmt)).scalar_one_or_none()

            if not match or match.status != MatchStatus.PENDING_MODERATION:
                await interaction.followup.send("Матч не найден или уже обработан.", ephemeral=True)
                return

            t1_users = [(await get_or_create_user(session, uid)) for uid in match.team1]
            t2_users = [(await get_or_create_user(session, uid)) for uid in match.team2]

            t1_ratings = [u.rating for u in t1_users]
            t2_ratings = [u.rating for u in t2_users]

            delta_t1, delta_t2 = await calculate_pts_updates(t1_ratings, t2_ratings, self.winner_team)

            t1_summary = []
            for u in t1_users:
                old_r = u.rating
                u.rating += delta_t1
                if self.winner_team == 1:
                    u.wins += 1
                else:
                    u.losses += 1
                session.add(RatingLog(match_id=match.id, user_id=u.discord_id, old_rating=old_r, new_rating=u.rating, delta=delta_t1))
                await check_and_assign_herald_role(interaction.guild, u.discord_id, u.rating)
                t1_summary.append(f"<@{u.discord_id}>: {old_r} ➔ **{u.rating}** ({delta_t1:+d} Pts)")

            t2_summary = []
            for u in t2_users:
                old_r = u.rating
                u.rating += delta_t2
                if self.winner_team == 2:
                    u.wins += 1
                else:
                    u.losses += 1
                session.add(RatingLog(match_id=match.id, user_id=u.discord_id, old_rating=old_r, new_rating=u.rating, delta=delta_t2))
                await check_and_assign_herald_role(interaction.guild, u.discord_id, u.rating)
                t2_summary.append(f"<@{u.discord_id}>: {old_r} ➔ **{u.rating}** ({delta_t2:+d} Pts)")

            match.status = MatchStatus.COMPLETED
            await session.commit()

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(name="Статус", value=f"✅ Подтверждено ({interaction.user.mention})\n**Изменения Pts:** T1 ({delta_t1:+d}), T2 ({delta_t2:+d})", inline=False)

        disabled_view = discord.ui.View()
        for child in self.children:
            child.disabled = True
            disabled_view.add_item(child)

        await interaction.message.edit(embed=embed, view=disabled_view)

        log_channel = interaction.guild.get_channel(MATCH_LOGS_CHANNEL_ID)
        if log_channel:
            log_embed = discord.Embed(title=f"📜 Лог матча #{self.match_id} [{match.mode}]", color=discord.Color.gold())
            log_embed.add_field(name="🏆 Победители", value=f"Команда {self.winner_team}", inline=False)
            log_embed.add_field(name="Команда 1", value="\n".join(t1_summary), inline=True)
            log_embed.add_field(name="Команда 2", value="\n".join(t2_summary), inline=True)
            log_embed.set_footer(text=f"Подтвердил модератор: {interaction.user.display_name}")
            await log_channel.send(embed=log_embed)

        await interaction.followup.send(f"Матч #{self.match_id} успешно подтверждён!", ephemeral=True)

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.danger, custom_id="mod_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectReasonModal(self.match_id))

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

        if self.accepted_users == self.required_users:
            self.stop()
            guild = interaction.guild
            category = interaction.channel.category if interaction.channel else None

            async with async_session() as session:
                stmt = select(Match).where(Match.id == self.match_id)
                match = (await session.execute(stmt)).scalar_one()
                match.status = MatchStatus.IN_PROGRESS

                overwrites = {
                    guild.default_role: discord.PermissionOverwrite(read_messages=False),
                    guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
                }

                for uid in self.required_users:
                    member = guild.get_member(uid)
                    if member:
                        overwrites[member] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                mod_role = discord.utils.find(lambda r: r.name.lower() == MOD_ROLE_NAME.lower(), guild.roles)
                if mod_role:
                    overwrites[mod_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

                channel_name = f"match-{match.id}"
                match_channel = await guild.create_text_channel(name=channel_name, category=category, overwrites=overwrites)
                match.channel_id = match_channel.id
                await session.commit()

                await match_channel.send(f"🎮 **Матч #{self.match_id} начался!**\nСостав участников готов. После завершения отправьте результат с помощью команды `/submit_result`.")

            embed = interaction.message.embeds[0]
            embed.title = f"Матч #{self.match_id} — В процессе"
            embed.color = discord.Color.gold()
            embed.set_field_at(0, name="Статус", value=f"🟢 Все игроки приняли. Создан канал {match_channel.mention}!", inline=False)
            button.disabled = True
            button.label = "Игра началась"
            button.style = discord.ButtonStyle.success
            await interaction.message.edit(embed=embed, view=self)

class MainPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Создать 1v1", style=discord.ButtonStyle.primary, emoji="⚔️", custom_id="panel_1v1")
    async def create_1v1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Для создания 1v1 матча используйте команду: `/create_match mode:1v1 opponent1:@соперник`", ephemeral=True)

    @discord.ui.button(label="Создать 2v2", style=discord.ButtonStyle.success, emoji="🛡️", custom_id="panel_2v2")
    async def create_2v2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("Для создания 2v2 матча используйте команду: `/create_match mode:2v2 opponent1:@враг1 teammate:@союзник opponent2:@враг2`", ephemeral=True)

    @discord.ui.button(label="Моя статистика", style=discord.ButtonStyle.secondary, emoji="📊", custom_id="panel_stats")
    async def show_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        embed = await build_profile_embed(interaction.user)
        await interaction.followup.send(embed=embed, ephemeral=True)

# ================= BOT BOT SETUP =================
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

# ================= SLASH COMMANDS =================
@bot.tree.command(name="panel", description="Вызвать главную панель матчмейкинга")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(title="🎮 Панель Матчмейкинга", description="Выберите нужное действие с помощью кнопок ниже:", color=discord.Color.dark_purple())
    view = MainPanelView()
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="edit_stats", description="Полная настройка статистики игрока (Только для Matchmaking Mod)")
@has_matchmaking_mod_role()
async def edit_stats(interaction: discord.Interaction, user: discord.User, pts: Optional[int] = None, wins: Optional[int] = None, losses: Optional[int] = None):
    await interaction.response.defer(ephemeral=True)
    async with async_session() as session:
        db_user = await get_or_create_user(session, user.id)
        changes = []
        if pts is not None:
            old_pts = db_user.rating
            db_user.rating = pts
            delta = pts - old_pts
            session.add(RatingLog(match_id=None, user_id=user.id, old_rating=old_pts, new_rating=pts, delta=delta))
            changes.append(f"Pts: {old_pts} ➔ **{pts}**")
        if wins is not None:
            db_user.wins = wins
            changes.append(f"Победы: **{wins}**")
        if losses is not None:
            db_user.losses = losses
            changes.append(f"Поражения: **{losses}**")
        await session.commit()

    if interaction.guild and pts is not None:
        await check_and_assign_herald_role(interaction.guild, user.id, pts)

    if not changes:
        await interaction.followup.send("Вы не указали ни одного параметра для изменения.", ephemeral=True)
        return

    log_channel = interaction.guild.get_channel(MATCH_LOGS_CHANNEL_ID)
    if log_channel:
        log_embed = discord.Embed(title="🛠️ Ручное изменение статистики", color=discord.Color.orange())
        log_embed.add_field(name="Игрок", value=user.mention, inline=True)
        log_embed.add_field(name="Модератор", value=interaction.user.mention, inline=True)
        log_embed.add_field(name="Изменения", value="\n".join(changes), inline=False)
        await log_channel.send(embed=log_embed)

    await interaction.followup.send(f"Статистика для <@{user.id}> успешно обновлена:\n" + "\n".join(changes), ephemeral=True)

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
        await interaction.response.send_message(f"Канал `{channel_name_or_mention}` не найден.", ephemeral=True)
        return

    try:
        ch_name = target_channel.name
        await target_channel.delete(reason=f"Удалено пользователем {interaction.user.display_name}")
        await interaction.response.send_message(f"✅ Канал `#{ch_name}` успешно удалён.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ У бота недостаточно прав для удаления этого канала.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Произошла ошибка при удалении: {e}", ephemeral=True)
        
