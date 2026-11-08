"""Database engine and session factory."""

from typing import Generator
from sqlmodel import SQLModel, Session, create_engine
from backend.app.config import DATABASE_URL

# SQLite requires check_same_thread=False for multi-threaded FastAPI workers
def get_database_url() -> str:
    url = DATABASE_URL
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://") and not url.startswith("postgresql+"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url

resolved_db_url = get_database_url()
connect_args = {}
if resolved_db_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(resolved_db_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    """Create all tables if they don't already exist and migrate columns."""
    import backend.app.models  # Ensure all models are registered
    from sqlalchemy import text
    SQLModel.metadata.create_all(engine)

    # Lightweight migration for existing SQLite dev databases ONLY
    if engine.dialect.name == "sqlite":
        with engine.connect() as conn:
            try:
                res = conn.execute(text("PRAGMA table_info(mandates)")).fetchall()
                existing_cols = {row[1] for row in res}
                if "escalate_first_merchant" not in existing_cols and len(existing_cols) > 0:
                    conn.execute(text("ALTER TABLE mandates ADD COLUMN escalate_first_merchant BOOLEAN DEFAULT 0"))
                    conn.commit()

                res_p = conn.execute(text("PRAGMA table_info(proposals)")).fetchall()
                existing_p_cols = {row[1] for row in res_p}
                if "llm_fallback" not in existing_p_cols and len(existing_p_cols) > 0:
                    conn.execute(text("ALTER TABLE proposals ADD COLUMN llm_fallback BOOLEAN DEFAULT 0"))
                    conn.commit()
                if "model_used" not in existing_p_cols and len(existing_p_cols) > 0:
                    conn.execute(text("ALTER TABLE proposals ADD COLUMN model_used VARCHAR DEFAULT NULL"))
                    conn.commit()
                if "llm_attempts" not in existing_p_cols and len(existing_p_cols) > 0:
                    conn.execute(text("ALTER TABLE proposals ADD COLUMN llm_attempts INTEGER DEFAULT 0"))
                    conn.commit()

                res_c = conn.execute(text("PRAGMA table_info(challenge_attempts)")).fetchall()
                existing_c_cols = {row[1] for row in res_c}
                if "breached" not in existing_c_cols and len(existing_c_cols) > 0:
                    conn.execute(text("ALTER TABLE challenge_attempts ADD COLUMN breached BOOLEAN DEFAULT 0"))
                    conn.commit()
            except Exception:
                pass


def get_session() -> Generator[Session, None, None]:
    """Yield a database session per request."""
    with Session(engine) as session:
        yield session
