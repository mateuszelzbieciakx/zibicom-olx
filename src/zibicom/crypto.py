"""Szyfrowanie tokenów OLX kluczem Fernet.

Klucz szyfrujący (`Settings.token_encryption_key`) jest sekretem i NIE
trafia do bazy - patrz `migrations/0004_olx_token.sql`. Sam wyciek dumpa
bazy nie wystarcza więc do przejęcia konta OLX, bo tokeny w kolumnach
`olx_token.*_encrypted` są bez tego klucza bezużyteczne.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from zibicom.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    """Buduje (raz na proces) obiekt Fernet z klucza szyfrującego w konfiguracji.

    Returns:
        Obiekt Fernet gotowy do szyfrowania/odszyfrowania tokenów.
    """
    settings = get_settings()
    return Fernet(settings.token_encryption_key.get_secret_value())


def encrypt(value: str) -> bytes:
    """Szyfruje wartość tekstową (np. token OAuth) do zapisu w kolumnie BYTEA.

    Args:
        value: Jawna wartość do zaszyfrowania.

    Returns:
        Zaszyfrowane bajty.
    """
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt(value: bytes) -> str:
    """Odszyfrowuje wartość wcześniej zapisaną przez `encrypt`.

    Args:
        value: Zaszyfrowane bajty odczytane z bazy.

    Returns:
        Jawna wartość tekstowa.

    Raises:
        cryptography.fernet.InvalidToken: Gdy `value` nie zostało
            zaszyfrowane bieżącym kluczem (np. zmieniony sekret).
    """
    return _fernet().decrypt(value).decode("utf-8")
