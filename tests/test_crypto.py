"""Testy szyfrowania tokenow OLX (Fernet) - zero sieci, zero bazy."""

from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet, InvalidToken

from zibicom import crypto
from zibicom.config import Settings


@pytest.fixture(autouse=True)
def _clear_fernet_cache() -> Iterator[None]:
    """Zeruje cache `_fernet()` przed i po kazdym tescie tego pliku.

    `_fernet()` jest opakowany w `lru_cache` (jedna instancja na caly
    proces) - bez czyszczenia jeden test moglby dostac obiekt zbudowany
    (z innym kluczem) przez poprzedni test.
    """
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def _settings_with_key(key: str) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        token_encryption_key=key,
    )


def test_encrypt_decrypt_roundtrip(monkeypatch: pytest.MonkeyPatch) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(key))

    encrypted = crypto.encrypt("tajny-refresh-token")

    assert isinstance(encrypted, bytes)
    assert b"tajny-refresh-token" not in encrypted
    assert crypto.decrypt(encrypted) == "tajny-refresh-token"


def test_fernet_jest_budowany_tylko_raz_na_klucz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(key))

    first = crypto._fernet()
    second = crypto._fernet()

    assert first is second


def test_decrypt_z_innym_kluczem_rzuca_invalid_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_a = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(key_a))
    encrypted = crypto.encrypt("sekret")

    crypto._fernet.cache_clear()
    key_b = Fernet.generate_key().decode("ascii")
    monkeypatch.setattr(crypto, "get_settings", lambda: _settings_with_key(key_b))

    with pytest.raises(InvalidToken):
        crypto.decrypt(encrypted)
