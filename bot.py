
```python
import os
import math
import asyncio
import datetime
from typing import List, Optional

import discord
from discord import app_commands
from discord.ext import commands

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, func, select, update, desc
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

# ================= CONFIGURATION =================
# Вставьте сюда Token вашего бота из Discord Developer Portal
TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# ID закрытого канала модерации, куда будут приходить скриншоты для проверки
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
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="WAITING")
    team1_ids: Mapped[str] = mapped_column(String(255))
    team2_ids: Mapped[str] = mapped_column(String(255), default="")
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
def calculate_team_elo(
    team1_ratings: List[float], 
    team2_ratings: List[float], 
    winner: int, 
    k_factor: float = 32.0
):
    avg_r1 = sum(team1_ratings) / len(team1_ratings)
    avg_r2 = sum(team2_ratings) / len(team2_ratings)

    e1 = 1.0 / (1.0 + math.pow(10, (avg_r2 - avg_r1) / 400.0))
    s1 = 1.0 if winner == 1 else 0.0

    delta1 = k_factor * (s1 - e1)
    delta2 = -delta1

    new_team1 = [r + delta1 for r in team1_ratings]
    new_team2 = [r + delta2 for r in team2_ratings]

    return new_team1, new_team2


# ================= BOT INITIALIZATION =================
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


# ================= UI COMPONENTS =================
class LobbyView(discord.ui.View):
    def __init__(self, match_id: int, required_players: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.required_players = required_players
        self.accepted_players: List[int] = []

    @discord.ui.button(label="Принять игру ⚔️️", style=discord.ButtonStyle.green, custom_id="join_lobby")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        if user_id in self.accepted_players:
            await interaction.response.send_message("Вы уже приняли эту игру!", ephemeral=True)
            return

        self.accepted_players.append(user_id)
        
        async with AsyncSessionLocal() as session:
            await get_or_create_user(session, user_id)

        if len(self.accepted_players) < self.required_players:
            embed = interaction.message.embeds[0]
            players_str = "\n".join([f"<@{pid}>" for pid in self.accepted_players])
            embed.set_field_at(0, name=f"Приняли ({len(self.accepted_players)}/{self.required_players}):", value=players_str, inline=False)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            button.disabled = True
            button.label = "Матч начался"
            button.style = discord.ButtonStyle.grey

            half = len(self.accepted_players) // 2
            team1 = self.accepted_players[:half]
            team2 = self.accepted_players[half:]

            async with AsyncSessionLocal() as session:
                stmt = (
                    update(Match)
                    .where(Match.id == self.match_id)
                    .values(
                        status="IN_PROGRESS",
                        team1_ids=",".join(map(str, team1)),
                        team2_ids=",".join(map(str, team2))
                    )
                )
                await session.execute(stmt)
                await session.commit()

            embed = interaction.message.embeds[0]
            embed.title = f"⚔️ Матч #{self.match_id} НАЧАЛСЯ!"
            embed.color = discord.Color.green()
            
            t1_str = ", ".join([f"<@{pid}>" for pid in team1])
            t2_str = ", ".join([f"<@{pid}>" for pid in team2])
            embed.clear_fields()
            embed.add_field(name="Команда 1", value=t1_str, inline=True)
            embed.add_field(name="Команда 2", value=t2_str, inline=True)

            await interaction.response.edit_message(embed=embed, view=self)

            thread = await interaction.channel.create_thread(
                name=f"Матч #{self.match_id}",
                type=discord.ChannelType.private_thread,
                invitable=False
            )
            for pid in self.accepted_players:
                member = interaction.guild.get_member(pid)
                if member:
                    await thread.add_user(member)
            
            async with AsyncSessionLocal() as session:
                await session.execute(update(Match).where(Match.id == self.match_id).values(thread_id=thread.id))
                await session.commit()

            await thread.send(f"Матч **#{self.match_id}** начался! Сыграйте партию и отправьте результат с помощью команды `/submit_result`.")


class RejectReasonModal(discord.ui.Modal, title="Причина отклонения"):
    reason = discord.ui.TextInput(
        label="Причина",
        style=discord.TextStyle.paragraph,
        placeholder="Укажите, почему скриншот или результат не принят...",
        required=True
    )

    def __init__(self, match_id: int):
        super().__init__()
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        async with AsyncSessionLocal() as session:
            await session.execute(
                update(Match).where(Match.id == self.match_id).values(status="CANCELLED")
            )
            await session.commit()

        embed = interaction.message.embeds[0]
        embed.color = discord.Color.red()
        embed.add_field(name="Статус модерации", value=f"❌ **Отклонено**: {self.reason.value}", inline=False)

        view = discord.ui.View.from_message(interaction.message)
        for child in view.children:
            child.disabled = True

        await interaction.response.edit_message(embed=embed, view=view)
        await interaction.followup.send(f"Матч #{self.match_id} отменён.", ephemeral=True)


class ModerationView(discord.ui.View):
    def __init__(self, match_id: int, winner_team: int):
        super().__init__(timeout=None)
        self.match_id = match_id
        self.winner_team = winner_team

    @discord.ui.button(label="Подтвердить ✅", style=discord.ButtonStyle.success, custom_id="approve_match")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with AsyncSessionLocal() as session:
            match_res = await session.execute(select(Match).where(Match.id == self.match_id))
            match = match_res.scalar_one_or_none()

            if not match or match.status == "COMPLETED":
                await interaction.response.send_message("Этот матч уже обработан!", ephemeral=True)
                return

            t1_ids = list(map(int, match.team1_ids.split(",")))
            t2_ids = list(map(int, match.team2_ids.split(",")))

            t1_users = [(await get_or_create_user(session, pid)) for pid in t1_ids]
            t2_users = [(await get_or_create_user(session, pid)) for pid in t2_ids]

            t1_ratings = [u.rating for u in t1_users]
            t2_ratings = [u.rating for u in t2_users]

            new_t1_ratings, new_t2_ratings = calculate_team_elo(t1_ratings, t2_ratings, self.winner_team)

            changes_summary = []
            for u, old_r, new_r in zip(t1_users, t1_ratings, new_t1_ratings):
                delta = new_r - old_r
                u.rating = new_r
                if self.winner_team == 1:
                    u.wins += 1
                else:
                    u.losses += 1
                
                session.add(RatingLog(
                    match_id=match.id, discord_id=u.discord_id, 
                    old_rating=old_r, new_rating=new_r, rating_change=delta
                ))
                changes_summary.append(f"<@{u.discord_id}>: {old_r:.1f} ➔ **{new_r:.1f}** ({delta:+.1f})")

            for u, old_r, new_r in zip(t2_users, t2_ratings, new_t2_ratings):
                delta = new_r - old_r
                u.rating = new_r
                if self.winner_team == 2:
                    u.wins += 1
                else:
                    u.losses += 1

                session.add(RatingLog(
                    match_id=match.id, discord_id=u.discord_id, 
                    old_rating=old_r, new_rating=new_r, rating_change=delta
                ))
                changes_summary.append(f"<@{u.discord_id}>: {old_r:.1f} ➔ **{new_r:.1f}** ({delta:+.1f})")

            match.status = "COMPLETED"
            await session.commit()

        for child in self.children:
            child.disabled = True
        
        embed = interaction.message.embeds[0]
        embed.color = discord.Color.green()
        embed.add_field(name="Результаты Elo", value="\n".join(changes_summary), inline=False)
        embed.set_footer(text=f"Подтверждено модератором {interaction.user.display_name}")
        await interaction.response.edit_message(embed=embed, view=self)

        if match.thread_id:
            thread = interaction.guild.get_thread(match.thread_id)
            if thread:
                announcement = discord.Embed(
                    title=f"🏆 Матч #{match.id} завершен!",
                    description="\n".join(changes_summary),
                    color=discord.Color.gold()
                )
                await thread.send(embed=announcement)

    @discord.ui.button(label="Отклонить ❌", style=discord.ButtonStyle.danger, custom_id="reject_match")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RejectReasonModal(self.match_id))


# ================= COMMANDS =================
@bot.tree.command(name="create_match", description="Создать лобби для рейтинговой игры")
@app_commands.choices(mode=[
    app_commands.Choice(name="1v1 (2 игрока)", value="1v1"),
    app_commands.Choice(name="2v2 (4 игрока)", value="2v2")
])
async def create_match(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    required_players = 2 if mode.value == "1v1" else 4

    async with AsyncSessionLocal() as session:
        new_match = Match(mode=mode.value, status="WAITING", team1_ids="")
        session.add(new_match)
        await session.commit()
        match_id = new_match.id

    embed = discord.Embed(
        title=f"🎮 Поиск игры: Матч #{match_id} ({mode.value})",
        description=f"Нажмите кнопку ниже, чтобы принять участие.\nТребуется игроков: **{required_players}**",
        color=discord.Color.blue()
    )
    embed.add_field(name=f"Приняли (0/{required_players}):", value="Пока никто...", inline=False)

    view = LobbyView(match_id, required_players)
    await interaction.response.send_message(embed=embed, view=view)


@bot.tree.command(name="submit_result", description="Отправить результат сыгранного матча")
@app_commands.choices(winner=[
    app_commands.Choice(name="Команда 1", value=1),
    app_commands.Choice(name="Команда 2", value=2)
])
async def submit_result(
    interaction: discord.Interaction, 
    match_id: int, 
    winner: app_commands.Choice[int], 
    screenshot: discord.Attachment
):
    if not screenshot.content_type or not screenshot.content_type.startswith("image/"):
        await interaction.response.send_message("Ошибка: Вложение должно быть скриншотом!", ephemeral=True)
        return

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Match).where(Match.id == match_id))
        match = result.scalar_one_or_none()

        if not match or match.status != "IN_PROGRESS":
            await interaction.response.send_message("Матч не найден или не находится в процессе игры.", ephemeral=True)
            return

        match.status = "PENDING_MODERATION"
        match.winner_team = winner.value
        match.proof_url = screenshot.url
        await session.commit()

    await interaction.response.send_message(f"Результат матча #{match_id} отправлен модераторам!", ephemeral=True)

    mod_channel = interaction.guild.get_channel(MODERATOR_CHANNEL_ID)
    if mod_channel:
        mod_embed = discord.Embed(
            title=f"🔍 Проверка результата: Матч #{match_id}",
            description=f"**Заявитель:** {interaction.user.mention}\n**Заявленный победитель:** Команда {winner.value}",
            color=discord.Color.orange()
        )
        mod_embed.add_field(name="Команда 1", value=", ".join([f"<@{pid}>" for pid in match.team1_ids.split(",")]))
        mod_embed.add_field(name="Команда 2", value=", ".join([f"<@{pid}>" for pid in match.team2_ids.split(",")]))
        mod_embed.set_image(url=screenshot.url)

        await mod_channel.send(embed=mod_embed, view=ModerationView(match_id, winner.value))


@bot.tree.command(name="profile", description="Посмотреть статистику игрока")
async def profile(interaction: discord.Interaction, member: Optional[discord.Member] = None):
    target = member or interaction.user
    
    async with AsyncSessionLocal() as session:
        user = await get_or_create_user(session, target.id)

    total_games = user.wins + user.losses
    winrate = (user.wins / total_games * 100) if total_games > 0 else 0.0

    embed = discord.Embed(title=f"📊 Профиль {target.display_name}", color=discord.Color.purple())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Рейтинг Elo", value=f"**{user.rating:.1f}**", inline=True)
    embed.add_field(name="Поб / Пор", value=f"{user.wins} / {user.losses}", inline=True)
    embed.add_field(name="Винрейт", value=f"{winrate:.1f}%", inline=True)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="leaderboard", description="Топ-10 игроков сервера")
async def leaderboard(interaction: discord.Interaction):
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).order_by(desc(User.rating)).limit(10))
        top_users = result.scalars().all()

    if not top_users:
        await interaction.response.send_message("Таблица лидеров пуста.", ephemeral=True)
        return

    description = ""
    for idx, user in enumerate(top_users, start=1):
        medal = "🥇" if idx == 1 else "🥈" if idx == 2 else "🥉" if idx == 3 else f"`#{idx}`"
        description += f"{medal} <@{user.discord_id}> — **{user.rating:.1f} Elo** ({user.wins}W / {user.losses}L)\n"

    embed = discord.Embed(title="🏆 Таблица Лидеров", description=description, color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


@bot.event
async def on_ready():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await bot.tree.sync()
    print(f"Бот успешно запущен как {bot.user}")


    import http.server
import socketserver
import threading

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", port), handler) as httpd:
        httpd.serve_forever()

if __name__ == "__main__":
    # Запускаем фоновый веб-сервер для Render
    threading.Thread(target=run_web_server, daemon=True).start()
    
    # Запускаем бота Discord
    bot.run(TOKEN)

```
