"""Database engine, session factory and declarative base."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Integer, create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


class BudgetLock(Base):
    """One fixed row that budget writers take a write lock on.

    It carries no data. It exists only so the SQLite serialisation below has
    a row it can always write, independent of whether any business table
    happens to hold records yet.
    """

    __tablename__ = "budget_lock"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)


BUDGET_LOCK_ID = 1


def _engine_kwargs(url: str) -> dict:
    if url.startswith("sqlite"):
        # Every /r click is a write and these routes run in Starlette's
        # threadpool, so a click arriving during an optimizer write contends
        # for the file. The timeout makes it wait rather than serving the
        # visitor an error on a click that was already paid for.
        return {
            "connect_args": {"check_same_thread": False, "timeout": 30},
            "future": True,
        }
    # pool_recycle sits below the five-minute idle cut managed Postgres
    # applies, so the pool never hands out a connection the server has closed.
    return {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 10,
        "pool_recycle": 280,
        "future": True,
    }


_settings = get_settings()
engine = create_engine(_settings.database_url, **_engine_kwargs(_settings.database_url))

if _settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover
        """WAL and a busy timeout, which are per-connection in SQLite."""
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def lock_budget_mutations(session: Session) -> None:
    """Serialise budget-changing mutations within one database transaction.

    Two writers that read the committed total and then write new budgets can
    both pass the cap against the same snapshot and land over it together.
    The lock orders them: each transaction's read happens after the previous
    one's write.

    This is same-database serialisation only — one process per database. It
    cannot coordinate two application servers against separate engines, and
    nothing here claims to.
    """
    connection = session.connection()
    transaction = session.get_transaction()
    if session.info.get("budget_lock_transaction") is transaction:
        session.flush()
        return
    dialect = connection.dialect.name
    if dialect == "sqlite":
        # A write on the dedicated lock row acquires the database's single
        # write transaction immediately, instead of at first flush, so the
        # committed-budget read that follows is ordered against every other
        # writer. The row is created on demand, so an empty database — no
        # offers, no campaigns — is serialised exactly like a full one.
        connection.execute(
            text(
                "INSERT INTO budget_lock (id) VALUES (:id) "
                "ON CONFLICT (id) DO UPDATE SET id = excluded.id"
            ),
            {"id": BUDGET_LOCK_ID},
        )
    elif dialect == "postgresql":
        if connection.get_isolation_level() != "READ COMMITTED":
            raise RuntimeError(
                "budget mutations require READ COMMITTED isolation"
            )
        connection.execute(text("SELECT pg_advisory_xact_lock(1735289201)"))
    else:
        raise RuntimeError(
            "budget mutation serialisation requires SQLite or PostgreSQL"
        )
    session.flush()
    session.expire_all()
    session.info["budget_lock_transaction"] = transaction


def init_db() -> None:
    """Create all tables. Safe to call repeatedly."""
    from . import models  # noqa: F401  (register mappers)

    Base.metadata.create_all(bind=engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope for background jobs and scripts."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
