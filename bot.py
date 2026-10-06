import os
import json
import asyncio
import http.server
import socketserver
import threading
from enum import Enum as PyEnum
from datetime import datetime
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text, func, select, desc
from sqlalchemy.ext.asyncio import AsyncAttrs, create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# --- КОНФИГУРАЦИЯ ---
# Токен подтягивается из Environment Variables на Render
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
MODERATION_CHANNEL_ID = 1557102218770120867  # ID канала модераторов

# ⚠️ ВСТАВЬТЕ СЮДА ID СВОЕГО СЕРВЕРА DISCORD (чтобы команды зарегистрировались мгновенно)
GUILD_ID = 1557102217058590744

DATABASE_URL = "sqlite+aiosqlite:///matchmaking.db"

# --- МОДЕЛИ БАЗЫ ДАННЫХ ---
class MatchStatus(str, PyEnum):
    PENDING_ACCEPT = "PENDING_ACCEPT"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_MODERATION = "PENDING_MODERATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

class Base(AsyncAttrs, DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    rating: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    rating_logs: Mapped[List["RatingLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")

class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus), default=MatchStatus.PENDING_ACCEPT, nullable=False)
    
    team1_raw: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    team2_raw: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    
    proof_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    thread_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    @property
    def team1(self) -> List[int]:
        return json.loads(self.team1_raw)

    @team1.setter
    def team1(self, value: List[int]):
        self.team1_raw = json.dumps(value)

    @property
    def team2(self) -> List[int]:
        return json.loads(self.team2_raw)

    @team2.setter
    def team2(self, value: List[int]):
        self.team2_raw = json.dumps(value)

class RatingLog(Base):
    __tablename__ = "rating_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(Integer, ForeignKey("matches.id"), nullable=False)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"), nullable=False)
    old_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    new_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="rating_logs")

async def calculate_elo_updates(
    team1_ratings: list[int], 
    team2_ratings: list[int], 
    winner_team: int, 
    k_factor: int = 32
) -> tuple[int, int]:
    avg_r1 = sum(team1_ratings) / len(team1_ratings)
    avg_r2 = sum(team2_ratings) / len(team2_ratings)

    expected_t1 = 1.0 / (1.0 + 10.0 ** ((avg_r2 - avg_r1) / 400.0))
    actual_t1 = 1.0 if winner_team == 1 else 0.0

    delta_t1 = round(k_factor * (actual_t1 - expected_t1))
    delta_t2 = -delta_t1

    return delta_t1, delta_t2

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

# --- MODALS & VIEWS ---
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
            match = (await session.execute(stmt)).scalar_one_or_none()
            
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
        await interaction.response.defer()

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

            delta_t1, delta_t2 = await calculate_elo_updates(t1_ratings, t2_ratings, self.winner_team)

            for u in t1_users:
                old_r = u.rating
                u.rating += delta_t1
                if self.winner_team == 1:
                    u.wins += 1
                else:
                    u.losses += 1
                session.add(RatingLog(match_id=match.id, user_id=u.discord_id, old_rating=old_r, new_rating=u.rating, delta=delta_t1))

            for u in t2_users:
                old_r = u.rating
                u.rating += delta_t2
                if self.winner_team == 2:
                    u.wins += 1
                else:
                    u.losses += 1
                session.add(RatingLog(match_id=match.id, user_id=u.discord_id, old_rating=old_r, new_rating=u.rating, delta=delta_t2))

            match.status = MatchStatus.COMPLETED
            await session.commit()

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(
            name="Статус", 
            value=f"✅ Подтверждено ({interaction.user.mention})\n**Изменения рейтинга:** T1 ({delta_t1:+d}), T2 ({delta_t2:+d})", 
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
            async with async_session() as session:
                stmt = select(Match).where(Match.id == self.match_id)
                match = (await session.execute(stmt)).scalar_one()
                match.status = MatchStatus.IN_PROGRESS

                channel = interaction.channel
                thread = await channel.create_thread(
                    name=f"Матч #{self.match_id} ({match.mode})",
                    type=discord.ChannelType.private_thread,
                    invitable=False
                )
                
                match.thread_id = thread.id
                await session.commit()

                for uid in self.required_users:
                    user_obj = channel.guild.get_member(uid)
                    if user_obj:
                        await thread.add_user(user_obj)

                await thread.send(f"🎮 **Матч #{self.match_id} начался!**\nСостав участников готов. После завершения отправьте результат с помощью команды `/submit_result`.")

            embed = interaction.message.embeds[0]
            embed.title = f"Матч #{self.match_id} — В процессе"
            embed.color = discord.Color.gold()
            embed.set_field_at(0, name="Статус", value="🟢 Все игроки приняли. Матч перенесён в ветку!", inline=False)
            
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
        guild = discord.Object(id=GUILD_ID)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        print("Команды синхронизированы на сервер и БД инициализирована.")

bot = MatchmakingBot()

# --- SLASH COMMANDS ---
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
    else:
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
            await interaction.followup.send(f"Нельзя отправить результат для матча в этом статусе.", ephemeral=True)
            return

        match.status = MatchStatus.PENDING_MODERATION
        match.proof_url = proof.url
        await session.commit()

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
        
        rank_stmt = select(func.count()).where(User.rating > db_user.rating)
        rank_res = await session.execute(rank_stmt)
        rank = rank_res.scalar_one() + 1

    total_games = db_user.wins + db_user.losses
    winrate = (db_user.wins / total_games * 100) if total_games > 0 else 0.0

    embed = discord.Embed(title=f"Профиль {target.display_name}", color=discord.Color.blue())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Рейтинг Elo", value=f"🏆 **{db_user.rating}** (Место: #{rank})", inline=False)
    embed.add_field(name="Победы / Поражения", value=f"📈 {db_user.wins} / 📉 {db_user.losses}", inline=True)
    embed.add_field(name="Винрейт", value=f"🎯 {winrate:.1f}%", inline=True)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Топ-10 игроков сервера")
async def leaderboard(interaction: discord.Interaction):
    async with async_session() as session:
        stmt = select(User).order_by(desc(User.rating)).limit(10)
        top_users = (await session.execute(stmt)).scalars().all()

    embed = discord.Embed(title="🏆 Лидерборд — Топ 10 Игроков", color=discord.Color.gold())
    
    description_lines = []
    for idx, u in enumerate(top_users, start=1):
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"`#{idx}`"
        description_lines.append(f"{medal} <@{u.discord_id}> — **{u.rating} Elo** (Побед: {u.wins} | Ссыграно: {u.wins + u.losses})")

    embed.description = "\n".join(description_lines) if description_lines else "Таблица лидеров пуста."
    await interaction.response.send_message(embed=embed)

# --- ВЕБ-СЕРВЕР ДЛЯ РАБОТЫ НА RENDER ---
def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(TOKEN)
