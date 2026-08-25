"""Silnik i sesje SQLAlchemy 2.0 (asynchroniczne, driver psycopg 3)."""

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from zibicom.config import get_settings


class Base(DeclarativeBase):
    """Wspolna klasa bazowa dla modeli ORM."""


@lru_cache
def get_engine() -> AsyncEngine:
    """Tworzy (raz na proces) asynchroniczny silnik bazy danych.

    Returns:
        Skonfigurowany AsyncEngine z pula polaczen.
    """
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        pool_pre_ping=True,
        # Bez limitu /health wisi w nieskonczonosc, gdy baza nie odpowiada
        # (zapora odrzuca pakiety zamiast zwracac connection refused).
        connect_args={"connect_timeout": settings.db_connect_timeout},
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    """Zwraca fabryke sesji zwiazana z silnikiem aplikacji.

    Returns:
        Fabryka sesji AsyncSession.
    """
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """Zaleznosc FastAPI dostarczajaca sesje bazy danych.

    Yields:
        Sesja zamykana automatycznie po obsludze zadania.
    """
    async with get_sessionmaker()() as session:
        yield session


async def dispose_engine() -> None:
    """Zamyka pule polaczen (wywolywane przy zamykaniu aplikacji)."""
    await get_engine().dispose()
