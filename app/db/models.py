from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    query: Mapped[str] = mapped_column(Text, nullable=False)
    advertiser_id: Mapped[str] = mapped_column(String(50), nullable=True)
    campaign_id: Mapped[str] = mapped_column(String(50), nullable=True)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    tools_used: Mapped[str] = mapped_column(Text, nullable=True)     # comma-separated
    status: Mapped[str] = mapped_column(String(30), nullable=False)  # RESOLVED_BY_AGENT | ESCALATED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
