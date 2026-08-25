"""Wspolne fixtures dla testow."""

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import psycopg
import pytest
from httpx import ASGITransport, AsyncClient
from psycopg import sql as pg_sql
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from zibicom.config import Settings, _read_local_secret, get_settings
from zibicom.main import app

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

# Nazwa bazy testowej: TEST_POSTGRES_DB, domyslnie "zibicom_test". MUSI
# konczyc sie na "_test" - patrz `_require_test_database_name`.
_TEST_DB_ENV_VAR = "TEST_POSTGRES_DB"
_DEFAULT_TEST_DB = "zibicom_test"
_TEST_DB_SUFFIX = "_test"

# Baza utrzymywana przez kazdy klaster Postgres - jedyna, do ktorej mozna sie
# polaczyc, ZANIM baza testowa w ogole istnieje (CREATE DATABASE nie da sie
# wykonac w polaczeniu do bazy, ktora sama ma dopiero powstac).
_MAINTENANCE_DB = "postgres"


def _require_test_database_name() -> str:
    """Nazwa bazy testowej z TEST_POSTGRES_DB (domyslnie "zibicom_test").

    TWARDA BLOKADA, nie konwencja: nazwa MUSI konczyc sie na "_test".
    `db_session` czysci dane MIEDZY testami (DELETE FROM intake_batch/
    listing/game/olx_token/olx_operation) - pomylka w konfiguracji (np.
    ustawienie TEST_POSTGRES_DB=zibicom przez przypadek) polaczylaby testy z
    ta sama baza, na ktorej pracuje aplikacja, i skasowala prawdziwa
    autoryzacje OLX oraz inwentarz sklepu.

    Returns:
        Nazwa bazy testowej.

    Raises:
        RuntimeError: Gdy skonfigurowana nazwa nie konczy sie na "_test".
    """
    name = os.environ.get(_TEST_DB_ENV_VAR, _DEFAULT_TEST_DB)
    if not name.endswith(_TEST_DB_SUFFIX):
        raise RuntimeError(
            f"BLOKADA BEZPIECZENSTWA: {_TEST_DB_ENV_VAR}={name!r} nie konczy "
            f"sie na {_TEST_DB_SUFFIX!r}. Testy czyszcza dane MIEDZY "
            "uruchomieniami (DELETE FROM ...) - nazwa bazy testowej musi "
            "wiec jednoznacznie odrozniac ja od bazy aplikacji, inaczej "
            "pomylka w konfiguracji moglaby nieodwracalnie skasowac "
            "prawdziwe dane. Zmien TEST_POSTGRES_DB (albo usun te zmienna, "
            f"zeby uzyc domyslnej {_DEFAULT_TEST_DB!r})."
        )
    return name


def _connect(db_settings: Settings, *, dbname: str) -> psycopg.Connection:
    """Otwiera synchroniczne polaczenie psycopg (autocommit) do `dbname`.

    Synchroniczne i osobne od aplikacyjnego silnika SQLAlchemy async -
    uzywane WYLACZNIE do jednorazowego przygotowania bazy testowej
    (`_ensure_test_database`) przed startem wlasciwych (asynchronicznych)
    testow, wiec nie ma powodu komplikowac go zakresem petli zdarzen
    pytest-asyncio.

    Args:
        db_settings: Zrodlo hosta/portu/uzytkownika/hasla.
        dbname: Nazwa bazy, do ktorej sie laczyc - NIE zawsze
            `db_settings.postgres_db` (patrz `_ensure_test_database`, gdzie
            pierwsze polaczenie idzie do `_MAINTENANCE_DB`, bo baza testowa
            moze jeszcze nie istniec).

    Returns:
        Otwarte polaczenie psycopg w trybie autocommit (CREATE DATABASE nie
        moze biec wewnatrz transakcji).
    """
    return psycopg.connect(
        host=db_settings.postgres_host,
        port=db_settings.postgres_port,
        user=db_settings.postgres_user,
        password=db_settings.postgres_password.get_secret_value(),
        dbname=dbname,
        autocommit=True,
    )


def _ensure_test_database(db_settings: Settings) -> None:
    """Tworzy baze testowa (jesli nie istnieje) i aplikuje na niej migracje.

    Migracje NIE sa idempotentne (CREATE TABLE/CREATE TYPE bez IF NOT
    EXISTS - napisane do jednorazowego uruchomienia przez
    docker-entrypoint-initdb.d), wiec sa aplikowane WYLACZNIE przy tworzeniu
    bazy od zera, nigdy ponownie na juz istniejacej - kolejne uruchomienia
    pytest ponownie uzywaja tej samej, raz zmigrowanej bazy testowej (patrz
    README: trzeba ja recznie usunac po dodaniu nowej migracji).

    Args:
        db_settings: Ustawienia wskazujace juz na baze testowa
            (`db_settings.postgres_db` zweryfikowane wczesniej przez
            `_require_test_database_name`).
    """
    with _connect(db_settings, dbname=_MAINTENANCE_DB) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (db_settings.postgres_db,),
        ).fetchone()
        if exists:
            return
        conn.execute(
            pg_sql.SQL("CREATE DATABASE {} OWNER {}").format(
                pg_sql.Identifier(db_settings.postgres_db),
                pg_sql.Identifier(db_settings.postgres_user),
            )
        )

    with _connect(db_settings, dbname=db_settings.postgres_db) as conn:
        for migration_path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            conn.execute(migration_path.read_text(encoding="utf-8"))


async def _assert_connected_to_test_database(session: AsyncSession) -> None:
    """Ostatnia linia obrony PRZED DELETE: sprawdza faktyczna nazwe bazy.

    `db_settings`/`_require_test_database_name` juz wymuszaja to przy
    konfiguracji - to dodatkowe sprawdzenie, wykonane na samym polaczeniu
    tuz przed czyszczeniem danych, zeby rowniez pomylka w kodzie fixture (a
    nie tylko w konfiguracji srodowiskowej) nie mogla skasowac
    produkcyjnych danych.

    Args:
        session: Sesja, ktorej polaczenie sprawdzamy.

    Raises:
        RuntimeError: Gdy biezaca baza nie konczy sie na "_test".
    """
    current_db = (await session.execute(text("SELECT current_database()"))).scalar()
    if not current_db or not current_db.endswith(_TEST_DB_SUFFIX):
        raise RuntimeError(
            "BLOKADA BEZPIECZENSTWA: probowano wyczyscic (DELETE) baze "
            f"{current_db!r}, ktora NIE jest baza testowa (nazwa nie konczy "
            f"sie na {_TEST_DB_SUFFIX!r}). Przerywam PRZED wykonaniem "
            "jakiegokolwiek DELETE - to wygladalo na baze aplikacji."
        )


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
    """Ustawienia polaczenia z baza TESTOWA - zawsze oddzielna od bazy aplikacji.

    Nazwa bazy pochodzi z `_require_test_database_name` (TEST_POSTGRES_DB,
    domyslnie "zibicom_test"), NIGDY z `postgres_db` uzywanego przez
    aplikacje - dzieki temu DELETE-y wykonywane przez `db_session` po kazdym
    tescie nie maja szans dotknac prawdziwych danych. Haslo pochodzi z tego
    samego pliku sekretu, ktorego uzyl kontener bazy przy pierwszej
    inicjalizacji (POSTGRES_PASSWORD_FILE) - .env moze byc nieaktualne wobec
    faktycznie dzialajacej bazy.

    Tworzy baze testowa (i aplikuje na niej migracje), jesli jeszcze nie
    istnieje w tym klastrze Postgres - patrz `_ensure_test_database`. Dzieje
    sie to raz na sesje pytest (fixture ma scope="session").

    Returns:
        Ustawienia wskazujace na baze testowa z docker-compose (localhost:5432).
    """
    test_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        postgres_password=_read_local_secret("postgres_password"),
        postgres_db=_require_test_database_name(),
    )
    _ensure_test_database(test_settings)
    return test_settings


@pytest.fixture
async def db_session(db_settings: Settings) -> AsyncIterator[AsyncSession]:
    """Sesja polaczona z rzeczywista baza TESTOWA (docker compose db).

    Testy intake celowo NIE mockuja bazy - to jej realne zachowanie (enumy,
    CHECK-i, kaskady FK) chcemy zweryfikowac. Po tescie kasuje wszystkie
    partie poczekalni (CASCADE do intake_item/intake_photo), oferty
    (CASCADE do listing_photo), gry, oraz stan integracji OLX
    (olx_token/olx_operation), zeby kolejne testy startowaly z czystym
    stanem; tabele slownikowe (platform) zostaja nietkniete. `db_settings`
    gwarantuje, ze to zawsze baza testowa (nazwa konczaca sie na "_test"),
    a `_assert_connected_to_test_database` sprawdza to jeszcze raz tuz przed
    DELETE - patrz tam po uzasadnienie podwojnego zabezpieczenia.

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
        await _assert_connected_to_test_database(cleanup_session)
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
