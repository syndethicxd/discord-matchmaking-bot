import json
from datetime import datetime
from enum import Enum as PyEnum
from typing import List, Optional

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, Text, func
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# 1. Объявление статусов матча
class MatchStatus(str, PyEnum):
    PENDING_ACCEPT = "PENDING_ACCEPT"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_MODERATION = "PENDING_MODERATION"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"

# 2. Базовый класс для SQLAlchemy
class Base(AsyncAttrs, DeclarativeBase):
    pass

# 3. Модель пользователя
class User(Base):
    __tablename__ = "users"

    discord_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    rating: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    wins: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    losses: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    rating_logs: Mapped[List["RatingLog"]] = relationship(back_populates="user", cascade="all, delete-orphan")

# 4. Модель матча
class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[MatchStatus] = mapped_column(Enum(MatchStatus), default=MatchStatus.PENDING_ACCEPT, nullable=False)
    
    team1_raw: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    team2_raw: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    
    proof_url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    channel_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)  # Храним ID созданного текстового канала
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

# 5. Логи изменения рейтинга
class RatingLog(Base):
    __tablename__ = "rating_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("matches.id"), nullable=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.discord_id"), nullable=False)
    old_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    new_rating: Mapped[int] = mapped_column(Integer, nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="rating_logs")

# 6. Расчет Pts
async def calculate_pts_updates(
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
  
