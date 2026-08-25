"""Testy pilnujace kontraktu migracji 0001 i 0004 (bez uruchamiania bazy)."""

from zibicom.config import Settings

EXPECTED_TABLES = (
    "platform",
    "game",
    "listing",
    "listing_photo",
    "sale_event",
    "olx_operation",
)


def test_migracja_tworzy_wszystkie_tabele(initial_migration_sql: str) -> None:
    for table in EXPECTED_TABLES:
        assert f"CREATE TABLE {table} (" in initial_migration_sql


def test_kazda_tabela_ma_trigger_updated_at(initial_migration_sql: str) -> None:
    for table in EXPECTED_TABLES:
        assert f"CREATE TRIGGER {table}_set_updated_at" in initial_migration_sql


def test_indeks_fifo_obejmuje_pare_gra_stan(initial_migration_sql: str) -> None:
    assert (
        "ON listing (game_id, condition, posted_at)\n    WHERE status = 'active'"
        in initial_migration_sql
    )


def test_ean_tylko_w_tabeli_game(initial_migration_sql: str) -> None:
    after_create = initial_migration_sql.split("CREATE TABLE listing (")[1]
    listing_ddl = after_create.split(");")[0]
    assert "ean" not in listing_ddl


def test_olx_token_jest_singletonem(olx_token_migration_sql: str) -> None:
    assert "id                      SMALLINT PRIMARY KEY DEFAULT 1" in (
        olx_token_migration_sql
    )
    assert "CHECK (id = 1)" in olx_token_migration_sql


def test_olx_token_refresh_token_jest_wymagany(olx_token_migration_sql: str) -> None:
    assert "refresh_token_encrypted BYTEA NOT NULL" in olx_token_migration_sql


def test_olx_token_ma_trigger_updated_at(olx_token_migration_sql: str) -> None:
    assert "CREATE TRIGGER olx_token_set_updated_at" in olx_token_migration_sql


def test_dsn_maskuje_haslo_w_reprezentacji(settings: object) -> None:
    assert "postgres_password=SecretStr" in repr(settings)


def test_database_url_zawiera_rozpakowane_haslo(settings: object) -> None:
    """`database_url` musi zawierac prawdziwe haslo jako `str`, nie SecretStr.

    `urllib.parse.quote()` uzyte w `database_url` przyjmuje wylacznie
    str/bytes - gdyby ktos usunal `.get_secret_value()`, ten test zawiodlaby
    z jasnym TypeError zamiast po cichu budowac zly DSN.
    """
    haslo = "tajne-haslo-!@#"
    settings_z_haslem = Settings(
        _env_file=None,  # type: ignore[call-arg]
        postgres_password=haslo,
    )

    assert isinstance(settings_z_haslem.database_url, str)
    assert "tajne-haslo" in settings_z_haslem.database_url
