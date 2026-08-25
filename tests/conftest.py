"""Wspolne fixtures dla testow."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from zibicom.config import Settings, get_settings
from zibicom.main import app

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def settings() -> Iterator[Settings]:
    """Ustawienia testowe, odciete od .env i sekretow developera.

    Yields:
        Instancja Settings z wartosciami domyslnymi.
    """
    get_settings.cache_clear()
    yield Settings(_env_file=None)  # type: ignore[call-arg]
    get_settings.cache_clear()


@pytest.fixture
def initial_migration_sql() -> str:
    """Tresc migracji 0001.

    Returns:
        Zawartosc pliku migrations/0001_initial_schema.sql.
    """
    return (MIGRATIONS_DIR / "0001_initial_schema.sql").read_text(encoding="utf-8")


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Klient HTTP rozmawiajacy z aplikacja przez transport ASGI.

    Yields:
        Asynchroniczny klient httpx.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
