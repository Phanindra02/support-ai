from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

# The engine keeps a pool of open connections and reuses them across requests.
# Add echo=True while learning to print every SQL statement SQLAlchemy runs.
engine = create_engine(
    settings.database_url,
    # Check that a pooled connection still works before using it, so a restarted
    # database server doesn't fail the next request.
    pool_pre_ping=True,
    # Ask Postgres for times in UTC. Otherwise it uses the server's time zone (New York on
    # the developer's Mac), and the API would return times like 14:00-04:00.
    connect_args={"options": "-c timezone=utc"},
)

# Each request will open a session from this factory and close it when done.
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    """Every table model inherits from this."""


def get_db():
    """Give each request its own session, and close it when the request is done."""
    with SessionLocal() as db:
        yield db
