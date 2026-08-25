"""Szyfrowanie tokenow OLX kluczem Fernet.

Klucz szyfrujacy (`Settings.token_encryption_key`) jest sekretem i NIE
trafia do bazy - patrz `migrations/0004_olx_token.sql`. Sam wyciek dumpa
bazy nie wystarcza wiec do przejecia konta OLX, bo tokeny w kolumnach
`olx_token.*_encrypted` sa bez tego klucza bezuzyteczne.
"""

from __future__ import annotations

from functools import lru_cache

from cryptography.fernet import Fernet

from zibicom.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    """Buduje (raz na proces) obiekt Fernet z klucza szyfrujacego w konfiguracji.

    Returns:
        Obiekt Fernet gotowy do szyfrowania/odszyfrowania tokenow.
    """
    settings = get_settings()
    return Fernet(settings.token_encryption_key.get_secret_value())


def encrypt(value: str) -> bytes:
    """Szyfruje wartosc tekstowa (np. token OAuth) do zapisu w kolumnie BYTEA.

    Args:
        value: Jawna wartosc do zaszyfrowania.

    Returns:
        Zaszyfrowane bajty.
    """
    return _fernet().encrypt(value.encode("utf-8"))


def decrypt(value: bytes) -> str:
    """Odszyfrowuje wartosc wczesniej zapisana przez `encrypt`.

    Args:
        value: Zaszyfrowane bajty odczytane z bazy.

    Returns:
        Jawna wartosc tekstowa.

    Raises:
        cryptography.fernet.InvalidToken: Gdy `value` nie zostalo
            zaszyfrowane biezacym kluczem (np. zmieniony sekret).
    """
    return _fernet().decrypt(value).decode("utf-8")
