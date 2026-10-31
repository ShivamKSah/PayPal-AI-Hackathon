"""Database engine and session factory."""

from typing import Generator
from sqlmodel import SQLModel, Session, create_engine
from backend.app.config import DATABASE_URL

# SQLite requires check_same_thread=False for multi-threaded FastAPI workers
connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, echo=False, connect_args=connect_args)


def init_db() -> None:
    """Create all tables if they don't already exist and migrate columns."""
    import backend.app.models  # Ensure all models are registered
    from sqlalchemy import text
    SQLModel.metadata.create_all(engine)

    # Lightweight migration for SQLite dev databases
    if DATABASE_URL.startswith("sqlite"):
        with engine.connect() as conn:
            try:
                res = conn.execute(text("PRAGMA table_info(mandates)")).fetchall()
                existing_cols = {row[1] for row in res}
                if "escalate_first_merchant" not in existing_cols and len(existing_cols) > 0:
                    conn.execute(text("ALTER TABLE mandates ADD COLUMN escalate_first_merchant BOOLEAN DEFAULT 0"))
                    conn.commit()
            except Exception:
                pass


def get_session() -> Generator[Session, None, None]:
    """Yield a database session per request."""
    with Session(engine) as session:
        yield session
