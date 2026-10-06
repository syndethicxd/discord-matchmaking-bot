import http.server
import socketserver
import threading
import os
import math
import datetime
from typing import List, Optional, Literal

import discord
from discord import app_commands
from discord.ext import commands

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, func, select, desc
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# ================= CONFIGURATION =================
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Укажи ID своего закрытого канала для модераторов!
MODERATOR_CHANNEL_ID = 123456789012345678  

DATABASE_URL = "sqlite+aiosqlite:///matchmaking.db"

# ================= DATABASE MODELS =================
class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    rating: Mapped[float] = mapped_column(Float, default=1000.0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)

class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10), default="1v1")  # "1v1" или "2v2"
    status: Mapped[str] = mapped_column(String(30), default="WAITING")  # WAITING, IN_PROGRESS, PENDING_MODERATION, FINISHED, CANCELLED
    team1_ids: Mapped[str] = mapped_column(String(255))
    team2_ids: Mapped[str] = mapped_column(String(255), default="")
    accepted_ids: Mapped[str] = mapped_column(String(255), default="")
    winner_team: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    proof_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    thread_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

class RatingLog(Base):
    __tablename__ = "rating_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    discord_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"))
    old_rating: Mapped[float] = mapped_column(Float)
    new_rating: Mapped[float] = mapped_column(Float)
    rating_change: Mapped[float] = mapped_column(Float)
    timestamp: Mapped[datetime.datetime] = mapped_column(DateTime, server_default=func.now())

# ================= ELO CALCULATION =================
def calculate_elo_change(team1_ratings: List[float], team2_ratings: List[float], winner: int, k_factor: float = 32.0):
    avg_r1 = sum(team1_ratings) / len(team1_ratings)
    avg_r2 = sum(team2_ratings) / len(team2_ratings)

    expected1 = 1.0 / (1.0 + math.pow(10, (avg_r2 - avg_r1) / 400.0))
    score1 = 1.0 if winner == 1 else 0.0

    delta1 = k_factor * (score1 - expected1)
    delta2 = -delta1

    new_t1 = [r + delta1 for r in team1_ratings]
    new_t2 = [r + delta2 for r in team2_ratings]

    return new_t1, new_t2, delta1, delta2

# ================= SETUP BOT & DB =================
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def get_or_create_user(session: AsyncSession, discord_id: int) -> User:
    result = await session.execute(select(User).where(User.discord_id == discord_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(discord_id=discord_id)
        session.add(user)
        await session.commit()
    return user

# ================= MODERATION VIEWS & MODALS =================
class RejectReasonModal(discord.ui.Modal, title="Причина отклонения"):
    reason = discord.ui.TextInput(label="Причина", style=discord.TextStyle.paragraph, placeholder="Укажите, почему результат отклонен...", required=True)

    def __init__(self, match_id: int):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Match).where(Match.id == self.match_id))
            match = result.scalar_one_or_none()
            if match:
                match.status = "CANCELLED"
                await session.commit()

        embed = discord.Embed(
            title=f"❌ Результат матча #{self.match_id} отклонён",
            description=f"**Модератор:** {interaction.user.mention}\n**Причина:** {self.reason.value}",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed)


class ModerationView(discord.ui.View):
    def __init__(self, match_id: int, winner_team: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.winner_team = winner_team

    @discord.ui.button(label="Подтвердить", style=discord.ButtonStyle.green, custom_id="mod_approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Match).where(Match.id == self.match_id))
            match = result.scalar_one_or_none()

            if not match or match.status != "PENDING_MODERATION":
                await interaction.response.send_message("Матч уже обработан.", ephemeral=True)
                return

            t1_ids = [int(i) for i in match.team1_ids.split(",") if i]
            t2_ids = [int(i) for i in match.team2_ids.split(",") if i]

            t1_users = [await get_or_create_user(session, uid) for uid in t1_ids]
            t2_users = [await get_or_create_user(session, uid) for uid in t2_ids]

            t1_ratings = [u.rating for u in t1_users]
            t2_ratings = [u.rating for u in t2_users]

            new_t1, new_t2, d1, d2 = calculate_elo_change(t1_ratings, t2_ratings, self.winner_team)

            for u, old_r, new_r in zip(t1_users, t1_ratings, new_t1):
                u.rating = new_r
                if self.winner_team == 1:
                    u.wins += 1
                else:
                    u.losses += 1
                session.add(RatingLog(match_id=match.id, discord_id=u.discord_id, old_rating=old_r, new_rating=new_r, rating_change=d1))

            for u, old_r, new_r in zip(t2_users, t2_ratings, new_t2):
                u.rating = new_r
                if self.winner_team == 2:
                    u.wins += 1
                else:
                    u.losses += 1
                session.add(RatingLog(match_id=match.id, discord_id=u.discord_id, old_rating=old_r, new_rating=new_r, rating_change=d2))

            match.status = "FINISHED"
            await session.commit()

            # Анонс в канале
            desc = f"Победитель: **Команда {self.winner_team}**\n\n"
            desc += f"**Команда 1:** {' '.join([f'<@{u.discord_id}> ({d1:+.1f})' for u in t1_users])}\n"
            desc += f"**Команда 2:** {' '.join([f'<@{u.discord_id}> ({d2:+.1f})' for u in t2_users])}"

            embed = discord.Embed(title=f"✅ Результаты матча #{self.match_id} подтверждены", description=desc, color=discord.Color.green())
            await interaction.response.send_message(embed=embed)
            
            # Уведомление в ветке матча (если есть)
            if match.thread_id:
                thread = interaction.guild.get_thread(match.thread_id)
                if thread:
                    await thread.send(embed=embed)

            self.stop()

    @discord.ui.button(label="Отклонить", style=discord.ButtonStyle.red, custom_id="mod_reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectReasonModal(self.match_id))


# ================= LOBBY ACCEPT VIEW =================
class AcceptMatchView(discord.ui.View):
    def __init__(self, match_id: int, max_players: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.max_players = max_players

    @discord.ui.button(label="Принять игру", style=discord.ButtonStyle.blurple, emoji="⚔️", custom_id="accept_match_btn")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as session:
            result = await session.execute(select(Match).where(Match.id == self.match_id))
            match = result.scalar_one_or_none()

            if not match or match.status != "WAITING":
                await interaction.response.send_message("Матч больше недоступен для присоединения.", ephemeral=True)
                return

            accepted = [int(i) for i in match.accepted_ids.split(",") if i]

            if interaction.user.id in accepted:
                await interaction.response.send_message("Вы уже приняли участие в этом матче!", ephemeral=True)
                return

            accepted.append(interaction.user.id)
            match.accepted_ids = ",".join(map(str, accepted))

            # Формируем команды при заполнении
            if len(accepted) < self.max_players:
                # В процессе сбора
                all_t1 = [int(i) for i in match.team1_ids.split(",") if i]
                if interaction.user.id not in all_t1:
                    t2 = [int(i) for i in match.team2_ids.split(",") if i]
                    t2.append(interaction.user.id)
                    match.team2_ids = ",".join(map(str, t2))
                
                await session.commit()

                players_mentions = ", ".join([f"<@{pid}>" for pid in accepted])
                embed = discord.Embed(
                    title=f"🎮 Лобби #{self.match_id} ({match.mode})",
                    description=f"Принято игроков: **{len(accepted)}/{self.max_players}**\n\n**Участники:** {players_mentions}",
                    color=discord.Color.gold()
                )
                await interaction.response.edit_message(embed=embed, view=self)
            else:
                # Лобби заполнено — НАЧАЛО МАТЧА
                match.status = "IN_PROGRESS"
                await session.commit()

                t1_mentions = ", ".join([f"<@{pid}>" for pid in match.team1_ids.split(",") if i])
                t2_mentions = ", ".join([f"<@{pid}>" for pid in match.team2_ids.split(",") if i])

                embed = discord.Embed(
                    title=f"🚀 Матч #{self.match_id} начался!",
                    description=f"**Команда 1:** {t1_mentions}\n**Команда 2:** {t2_mentions}\n\n*Для отправки результатов используйте команду:* `/submit_result`",
                    color=discord.Color.green()
                )
                await interaction.response.edit_message(embed=embed, view=None)

                # Создаем приватную ветку (Thread)
                try:
                    thread = await interaction.channel.create_thread(
                        name=f"Матч #{self.match_id}",
                        type=discord.ChannelType.private_thread,
                        invitable=False
                    )
                    match.thread_id = thread.id
                    await session.commit()

                    for pid in accepted:
                        member = interaction.guild.get_member(pid)
                        if member:
                            await thread.add_user(member)

                    await thread.send(f"Добро пожаловать в комнату матча #{self.match_id}!\nДоговоритесь об игре и отправьте результат с помощью `/submit_result`.")
                except Exception as e:
                    print(f"Ошибка при создании ветки: {e}")


# ================= SLASH COMMANDS =================
@bot.tree.command(name="create_match", description="Создать лобби для рейтинговой игры")
@app_commands.describe(mode="Выберите режим игры")
async def create_match(interaction: discord.Interaction, mode: Literal["1v1", "2v2"]):
    max_players = 2 if mode == "1v1" else 4

    async with AsyncSessionLocal() as session:
        await get_or_create_user(session, interaction.user.id)
        
        new_match = Match(
            mode=mode,
            status="WAITING",
            team1_ids=str(interaction.user.id),
            accepted_ids=str(interaction.user.id)
        )
        session.add(new_match)
        await session.commit()
        match_id = new_match.id

    embed = discord.Embed(
        title=f"🎮 Поиск игроков: Матч #{match_id} ({mode})",
        description=f"Создатель: {interaction.user.mention}\nПринято: **1/{max_players}**\n\nНажмите кнопку ниже, чтобы принять игру!",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=AcceptMatchView(match_id, max_players))


@bot.tree.command(name="submit_result", description="Отправить результат сыгранного матча")
@app_commands.describe(
    match_id="ID вашего матча",
    winner="Победившая команда (1 или 2)",
    proof="Скриншот экрана с итоговым счётом"
)
async def submit_result(interaction: discord.Interaction, match_id: int, winner: Literal[1, 2], proof: discord.Attachment):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Match).where(Match.id == match_id))
        match = result.scalar_one_or_none()

        if not match or match.status != "IN_PROGRESS":
            await interaction.response.send_message("Матч не найден или не находится в процессе игры.", ephemeral=True)
            return

        accepted = [int(i) for i in match.accepted_ids.split(",") if i]
        if interaction.user.id not in accepted:
            await interaction.response.send_message("Вы не являетесь участником этого матча!", ephemeral=True)
            return

        match.status = "PENDING_MODERATION"
        match.winner_team = winner
        match.proof_url = proof.url
        await session.commit()

        await interaction.response.send_message(f"Результат матча #{match_id} отправлен на проверку модераторам!", ephemeral=True)

        mod_channel = interaction.guild.get_channel(MODERATOR_CHANNEL_ID)
        if mod_channel:
            mod_embed = discord.Embed(
                title=f"🔍 Модерация результата: Матч #{match_id}",
                description=f"**Заявитель:** {interaction.user.mention}\n**Заявленный победитель:** Команда {winner}",
                color=discord.Color.orange()
            )
            mod_embed.add_field(name="Команда 1", value=", ".join([f"<@{pid}>" for pid in match.team1_ids.split(",") if pid]))
            mod_embed.add_field(name="Команда 2", value=", ".join([f"<@{pid}>" for pid in match.team2_ids.split(",") if pid]))
            mod_embed.set_image(url=proof.url)

            await mod_channel.send(embed=mod_embed, view=ModerationView(match_id, winner))


@bot.tree.command(name="profile", description="Просмотреть статистику игрока")
async def profile(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user

    async with AsyncSessionLocal() as session:
        user = await get_or_create_user(session, target.id)

        total_games = user.wins + user.losses
        winrate = (user.wins / total_games * 100) if total_games > 0 else 0.0

        embed = discord.Embed(title=f"📊 Профиль {target.display_name}", color=discord.Color.purple())
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Рейтинг Elo", value=f"**{user.rating:.1f}**", inline=True)
        embed.add_field(name="Победы / Поражения", value=f"{user.wins} / {user.losses}", inline=True)
        embed.add_field(name="Винрейт", value=f"{winrate:.1f}%", inline=True)

        await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Топ-10 игроков по рейтингу")
async def leaderboard(interaction: discord.Interaction):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).order_by(desc(User.rating)).limit(10))
        top_users = result.scalars().all()

        if not top_users:
            await interaction.response.send_message("Таблица лидеров пока пуста.", ephemeral=True)
            return

        desc_text = ""
        for idx, user in enumerate(top_users, start=1):
            medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"#{idx}"
            desc_text += f"{medal} <@{user.discord_id}> — **{user.rating:.1f} Elo** ({user.wins}W / {user.losses}L)\n"

        embed = discord.Embed(title="🏆 Таблица Лидеров", description=desc_text, color=discord.Color.gold())
        await interaction.response.send_message(embed=embed)


# ================= BOT EVENTS & WEB SERVER =================
@bot.event
async def on_ready():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await bot.tree.sync()
    print(f"Бот успешно запущен как {bot.user}")


def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    bot.run(TOKEN)
                
