"""Session plumbing. Kept tiny on purpose — models.py owns the schema."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from .models import get_session, init_engine


def connect(db_url: str, *, echo: bool = False) -> None:
    init_engine(db_url, echo=echo)


@contextmanager
def session_scope(db_url: str | None = None, *, echo: bool = False) -> Iterator[Session]:
    if db_url:
        init_engine(db_url, echo=echo)
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = ["connect", "get_session", "init_engine", "session_scope"]
