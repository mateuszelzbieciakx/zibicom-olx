"""Wspolne fixtures dla testow."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from zibicom.config import Settings, _read_local_secret, get_settings
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
def olx_token_migration_sql() -> str:
    """Tresc migracji 0004 (tabela olx_token).

    Returns:
        Zawartosc pliku migrations/0004_olx_token.sql.
    """
    return (MIGRATIONS_DIR / "0004_olx_token.sql").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def db_settings() -> Settings:
    """Ustawienia polaczenia z baza testowa (kontener z docker-compose).

    Haslo pochodzi z tego samego pliku sekretu, ktorego uzyl kontener bazy
    przy pierwszej inicjalizacji (POSTGRES_PASSWORD_FILE) - .env moze byc
    nieaktualne wobec faktycznie dzialajacej bazy.

    Returns:
        Ustawienia wskazujace na baze z docker-compose (localhost:5432).
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        postgres_password=_read_local_secret("postgres_password"),
    )


@pytest.fixture
async def db_session(db_settings: Settings) -> AsyncIterator[AsyncSession]:
    """Sesja polaczona z rzeczywista baza Postgres (docker compose db).

    Testy intake celowo NIE mockuja bazy - to jej realne zachowanie (enumy,
    CHECK-i, kaskady FK) chcemy zweryfikowac. Po tescie kasuje wszystkie
    partie poczekalni (CASCADE do intake_item/intake_photo), oferty
    (CASCADE do listing_photo), gry, oraz stan integracji OLX
    (olx_token/olx_operation), zeby kolejne testy startowaly z czystym
    stanem; tabele slownikowe (platform) zostaja nietkniete.

    Args:
        db_settings: Ustawienia polaczenia z baza testowa.

    Yields:
        Sesja do uzycia w tescie.
    """
    engine = create_async_engine(db_settings.database_url)
    sessionmaker = async_sessionmaker(
        bind=engine, expire_on_commit=False, autoflush=False
    )
    async with sessionmaker() as session:
        yield session

    async with sessionmaker() as cleanup_session:
        await cleanup_session.execute(text("DELETE FROM intake_batch"))
        await cleanup_session.execute(text("DELETE FROM listing"))
        await cleanup_session.execute(text("DELETE FROM game"))
        await cleanup_session.execute(text("DELETE FROM olx_token"))
        await cleanup_session.execute(text("DELETE FROM olx_operation"))
        await cleanup_session.commit()
    await engine.dispose()


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Klient HTTP rozmawiajacy z aplikacja przez transport ASGI.

    Yields:
        Asynchroniczny klient httpx.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
