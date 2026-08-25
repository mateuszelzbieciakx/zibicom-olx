"""Konfiguracja aplikacji czytana z sekretow Dockera oraz z pliku .env."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

DOCKER_SECRETS_DIR = Path("/run/secrets")


def _secrets_dir() -> str | None:
    """Zwraca katalog sekretow Dockera, jesli istnieje.

    Pydantic-settings ostrzega o nieistniejacym katalogu, a na Windowsie
    /run/secrets nigdy nie istnieje - dlatego wybor jest dynamiczny.

    Returns:
        Sciezka do katalogu sekretow albo None, gdy dzialamy poza Dockerem.
    """
    return str(DOCKER_SECRETS_DIR) if DOCKER_SECRETS_DIR.is_dir() else None


class Settings(BaseSettings):
    """Ustawienia aplikacji.

    Kolejnosc zrodel (od najwazniejszego): zmienne srodowiskowe, plik .env,
    pliki w /run/secrets. Dzieki temu ten sam kod dziala lokalnie na Windowsie
    i w kontenerze na VPS.

    Attributes:
        app_env: Nazwa srodowiska (local, staging, production).
        log_level: Poziom logowania przekazywany do loggera aplikacji.
        api_host: Interfejs, na ktorym nasluchuje serwer HTTP.
        api_port: Port serwera HTTP.
        postgres_host: Host bazy danych.
        postgres_port: Port bazy danych.
        postgres_db: Nazwa bazy danych.
        postgres_user: Uzytkownik bazy danych.
        postgres_password: Haslo bazy danych (sekret).
        db_pool_size: Rozmiar puli polaczen SQLAlchemy.
        db_connect_timeout: Limit sekund na nawiazanie polaczenia z baza.
        db_echo: Czy logowac zapytania SQL.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        secrets_dir=_secrets_dir(),
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "local"
    log_level: str = "INFO"

    api_host: str = "127.0.0.1"
    api_port: int = 8000

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "zibicom"
    postgres_user: str = "zibicom"
    postgres_password: SecretStr = Field(default=SecretStr(""))

    db_pool_size: int = 5
    db_connect_timeout: int = 5
    db_echo: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Buduje asynchroniczny DSN dla SQLAlchemy (driver psycopg 3).

        Returns:
            DSN w formacie postgresql+psycopg://user:haslo@host:port/baza.
        """
        password = quote(self.postgres_password.get_secret_value(), safe="")
        user = quote(self.postgres_user, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Zwraca zbuforowana instancje ustawien.

    Returns:
        Jedna, wspoldzielona instancja Settings dla calego procesu.
    """
    return Settings()
