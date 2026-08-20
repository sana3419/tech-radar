from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .settings import get_settings

_engine = None
_SessionLocal = None


def get_engine():
    global _engine, _SessionLocal
    if _engine is None:
        _engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True,
                                pool_recycle=1800)

        @event.listens_for(_engine, "connect")
        def _disable_prepared_statements(dbapi_conn, _rec):
            """psycopg3 auto-prepares repeated queries; a migration that changes a column type then
            makes long-lived daemons fail with 'cached plan must not change result type' until they
            are restarted. This project's query volume doesn't need prepared plans."""
            try:
                dbapi_conn.prepare_threshold = None
            except AttributeError:      # non-psycopg driver
                pass

        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, class_=Session)
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    get_engine()
    s: Session = _SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def ping() -> bool:
    with get_engine().connect() as c:
        return c.execute(text("select 1")).scalar() == 1
