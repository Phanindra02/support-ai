from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

# A SQLite file in the project folder. In Stage 2 you'll swap this for a Postgres URL.
DATABASE_URL = "sqlite:///./support.db"

# check_same_thread=False lets FastAPI's worker threads share the SQLite connection.
# Add echo=True while learning to print every SQL statement SQLAlchemy runs.
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})

# Each request will open a session from this factory and close it when done.
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    """Every table model inherits from this."""


def get_db():
    """Give each request its own session, and close it when the request is done."""
    with SessionLocal() as db:
        yield db
