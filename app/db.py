"""SQLAlchemy engine + session (sync, psycopg3). Postgres 17.5."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import settings

engine = create_engine(settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


class Base(DeclarativeBase):
    pass


def get_session():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create tables on boot. (Alembic migrations replace this once the schema stabilises.)"""
    from . import models  # noqa: F401  (register mappers)
    Base.metadata.create_all(engine)
