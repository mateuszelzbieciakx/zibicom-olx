"""Konfiguracja aplikacji czytana z sekretów Dockera oraz z pliku .env."""

from functools import lru_cache, partial
from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

DOCKER_SECRETS_DIR = Path("/run/secrets")
LOCAL_SECRETS_DIR = Path(__file__).resolve().parents[2] / "secrets"


def _secrets_dir() -> str | None:
    """Zwraca katalog sekretów Dockera, jeśli istnieje.

    Pydantic-settings ostrzega o nieistniejącym katalogu, a na Windowsie
    /run/secrets nigdy nie istnieje - dlatego wybór jest dynamiczny.

    Returns:
        Ścieżka do katalogu sekretów albo None, gdy działamy poza Dockerem.
    """
    return str(DOCKER_SECRETS_DIR) if DOCKER_SECRETS_DIR.is_dir() else None


def _read_local_secret(name: str) -> SecretStr:
    """Czyta sekret z lokalnego pliku secrets/<name>.txt poza Dockerem.

    Służy jako wartość domyślna pola - jeśli sekret przyjdzie z
    /run/secrets (wyższy priorytet źródeł pydantic-settings), ta funkcja
    w ogóle się nie wykona. Używane przy uruchamianiu aplikacji lub
    skryptów bezpośrednio na hoście (poza kontenerem Dockera).

    Args:
        name: Nazwa pliku sekretu bez rozszerzenia, np. "r2_access_key_id".

    Returns:
        Zawartość pliku secrets/<name>.txt jako SecretStr, albo pusty
        SecretStr, gdy plik nie istnieje.
    """
    path = LOCAL_SECRETS_DIR / f"{name}.txt"
    if path.is_file():
        return SecretStr(path.read_text(encoding="utf-8").strip())
    return SecretStr("")


class Settings(BaseSettings):
    """Ustawienia aplikacji.

    Kolejność źródeł (od najważniejszego): zmienne środowiskowe, plik .env,
    pliki w /run/secrets. Dzięki temu ten sam kod działa lokalnie na Windowsie
    i w kontenerze na VPS.

    Attributes:
        app_env: Nazwa środowiska (local, staging, production).
        log_level: Poziom logowania przekazywany do loggera aplikacji.
        api_host: Interfejs, na którym nasłuchuje serwer HTTP.
        api_port: Port serwera HTTP.
        postgres_host: Host bazy danych.
        postgres_port: Port bazy danych.
        postgres_db: Nazwa bazy danych.
        postgres_user: Użytkownik bazy danych.
        postgres_password: Hasło bazy danych (sekret).
        db_pool_size: Rozmiar puli połączeń SQLAlchemy.
        db_connect_timeout: Limit sekund na nawiązanie połączenia z bazą.
        db_echo: Czy logować zapytania SQL.
        r2_endpoint: Endpoint S3 API konta Cloudflare R2.
        r2_bucket: Nazwa bucketu R2 ze zdjęciami ofert.
        r2_public_base_url: Publiczny adres, pod którym OLX pobiera zdjęcia.
        r2_access_key_id: Identyfikator klucza dostępowego R2 (sekret).
        r2_secret_access_key: Sekretny klucz dostępowy R2 (sekret).
        gemini_api_key: Klucz API Gemini (sekret).
        gemini_model: Nazwa modelu Gemini używanego do rozpoznawania zdjęć.
        olx_client_id: Identyfikator aplikacji OLX Partner API (sekret).
        olx_client_secret: Sekret aplikacji OLX Partner API (sekret).
        olx_redirect_uri: Callback URI zarejestrowany w aplikacji OLX.
            Bez działającego endpointu (OLX nie akceptuje localhost) -
            autoryzacja jest półręczna, patrz zibicom.olx.
        olx_auth_base_url: Host, pod którym OLX obsługuje logowanie OAuth.
        olx_api_base_url: Baza REST API OLX Partner (kategorie, miasta,
            ogłoszenia) oraz wymiana/odświeżenie tokenu.
        olx_city_id: Domyślne miasto OLX dla publikowanych ofert
            (0 = nieustawione, trzeba wybrać przez /api/olx/cities).
        olx_district_id: Domyślna dzielnica OLX dla publikowanych ofert
            (0 = nieustawiona - w payloadzie pomijana, patrz
            build_advert_payload). Wymagana przez OLX TYLKO dla miast z
            podziałem na dzielnice (np. Kraków) - małe miejscowości go nie
            mają; wybierz przez /api/olx/cities/{city_id}/districts.
        olx_contact_name: Nazwa kontaktowa wyświetlana w ogłoszeniu OLX
            (pole "contact.name" wymagane przez Partner API).
        token_encryption_key: Klucz Fernet do szyfrowania tokenów OLX w
            bazie (sekret). Nie może trafić do bazy - patrz
            migrations/0004_olx_token.sql.
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

    r2_endpoint: str = (
        "https://55ebb5f870e0b4d84955d5e8cd7411ce.r2.cloudflarestorage.com"
    )
    r2_bucket: str = "zibicom-photos"
    r2_public_base_url: str = "https://pub-f139767f741440dcb875c293ca7116f0.r2.dev"
    r2_access_key_id: SecretStr = Field(
        default_factory=partial(_read_local_secret, "r2_access_key_id")
    )
    r2_secret_access_key: SecretStr = Field(
        default_factory=partial(_read_local_secret, "r2_secret_access_key")
    )

    gemini_api_key: SecretStr = Field(
        default_factory=partial(_read_local_secret, "gemini_api_key")
    )
    gemini_model: str = "gemini-3.6-flash"

    olx_client_id: SecretStr = Field(
        default_factory=partial(_read_local_secret, "olx_client_id")
    )
    olx_client_secret: SecretStr = Field(
        default_factory=partial(_read_local_secret, "olx_client_secret")
    )
    olx_redirect_uri: str = (
        "https://pub-f139767f741440dcb875c293ca7116f0.r2.dev/callback"
    )
    olx_auth_base_url: str = "https://www.olx.pl"
    olx_api_base_url: str = "https://www.olx.pl/api/partner"
    olx_city_id: int = 0
    olx_district_id: int = 0
    olx_contact_name: str = "ZibiCom"

    token_encryption_key: SecretStr = Field(
        default_factory=partial(_read_local_secret, "token_encryption_key")
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_url(self) -> str:
        """Buduje asynchroniczny DSN dla SQLAlchemy (driver psycopg 3).

        Returns:
            DSN w formacie postgresql+psycopg://user:hasło@host:port/baza.
        """
        password = quote(self.postgres_password.get_secret_value(), safe="")
        user = quote(self.postgres_user, safe="")
        return (
            f"postgresql+psycopg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


@lru_cache
def get_settings() -> Settings:
    """Zwraca zbuforowaną instancję ustawień.

    Returns:
        Jedna, współdzielona instancja Settings dla całego procesu.
    """
    return Settings()
