"""Testy pilnujace kontraktu migracji 0001 (bez uruchamiania bazy)."""

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


def test_dsn_maskuje_haslo_w_reprezentacji(settings: object) -> None:
    assert "postgres_password=SecretStr" in repr(settings)
