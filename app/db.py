"""SQLAlchemy engine + session (sync, psycopg3) on Postgres 17.5, with tenant Row-Level Security."""
from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, text
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
    """Create tables + enforce tenant Row-Level Security. (Alembic replaces create_all later.)"""
    from . import models  # noqa: F401
    Base.metadata.create_all(engine)
    apply_rls()


def apply_rls() -> None:
    """Enable + FORCE Row-Level Security on the data tables so every access is scoped to
    `app.current_tenant` — Postgres itself blocks cross-tenant reads (even for the table owner).
    Scales to thousands of tenants without table/schema fan-out."""
    from .models import RLS_TABLES
    with engine.begin() as conn:
        for t in RLS_TABLES:
            conn.execute(text(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"))
            conn.execute(text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY"))
            conn.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {t}"))
            conn.execute(text(
                f"CREATE POLICY tenant_isolation ON {t} USING "
                "(tenant_id = current_setting('app.current_tenant', true)::int) "
                "WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::int)"))


@contextmanager
def tenant_scope(db, tenant_id: int):
    """Bind the session to one tenant for the duration of a transaction. Every RLS-guarded query
    then sees only that tenant's rows. Unset context = zero rows (fail-closed)."""
    db.execute(text("SELECT set_config('app.current_tenant', :t, true)"), {"t": str(tenant_id)})
    try:
        yield db
    finally:
        db.execute(text("SELECT set_config('app.current_tenant', '', true)"))
