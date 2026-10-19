from typing import Annotated

from fastapi import Depends
from sqlalchemy import Engine, MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


def make_engine(url: str) -> Engine:
    """Make an engine with the options every connection needs. The tests use it too."""
    # Add echo=True while learning to print every SQL statement SQLAlchemy runs.
    return create_engine(
        url,
        # Check that a pooled connection still works before using it, so a restarted
        # database server doesn't fail the next request.
        pool_pre_ping=True,
        # Ask Postgres for times in UTC. Otherwise it uses the server's time zone (New York on
        # the developer's Mac), and the API would return times like 14:00-04:00.
        connect_args={"options": "-c timezone=utc"},
    )


# The engine keeps a pool of open connections and reuses them across requests.
engine = make_engine(settings.database_url)

# Each request will open a session from this factory and close it when done.
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    """Every table model inherits from this."""

    # Give constraints and indexes predictable names, like fk_tickets_customer_id_customers.
    # A later migration has to name a constraint to drop or change it, and without these
    # rules Postgres would make up the names itself.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


def get_db():
    """Give each request its own session, and close it when the request is done."""
    with SessionLocal() as db:
        yield db


# Shortcut: any endpoint with a `db: SessionDep` parameter gets its own database session.
SessionDep = Annotated[Session, Depends(get_db)]
