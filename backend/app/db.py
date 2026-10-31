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
    """Create all tables if they don't already exist."""
    import backend.app.models  # Ensure all models are registered
    SQLModel.metadata.create_all(engine)


def get_session() -> Generator[Session, None, None]:
    """Yield a database session per request."""
    with Session(engine) as session:
        yield session
