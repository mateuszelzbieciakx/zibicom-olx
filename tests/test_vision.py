"""Testy klienta Gemini - Gemini jest w pelni zamockowany, zero sieci."""

import io
from collections.abc import Iterator
from unittest.mock import MagicMock

import pytest
from google.genai import errors
from PIL import Image
from pydantic import SecretStr

from zibicom import vision
from zibicom.config import Settings
from zibicom.models import PhotoExtraction, PlatformCode


def _jpeg_bytes() -> bytes:
    """Minimalne, ale prawdziwe (dekodowalne przez PIL) bajty JPEG."""
    image = Image.new("RGB", (4, 4), color=(10, 120, 200))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _clear_client_cache() -> Iterator[None]:
    """Zeruje cache `_client()` przed i po kazdym tescie tego pliku.

    `_client()` jest opakowany w `lru_cache` (jedna instancja na caly
    proces) - bez czyszczenia jeden test moglby dostac klienta zbudowanego
    (i zamockowanego) przez poprzedni test.
    """
    vision._client.cache_clear()
    yield
    vision._client.cache_clear()


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Testy retry nie maja naprawde czekac `RETRY_DELAY_SECONDS`."""
    monkeypatch.setattr(vision.time, "sleep", lambda _seconds: None)


def _client_error(code: int) -> errors.ClientError:
    return errors.ClientError(
        code, {"error": {"code": code, "message": "boom", "status": "ERROR"}}
    )


def test_client_przekazuje_klucz_api_jako_zwykly_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`genai.Client` oczekuje `str` - SecretStr trzeba rozpakowac przed przekazaniem.

    Test celowo mockuje `genai.Client` (a nie `vision._client`, jak w
    pozostalych testach tego pliku) - inaczej nie da sie sprawdzic, JAKI typ
    argumentu faktycznie trafia do konstruktora klienta.
    """
    captured: dict[str, object] = {}

    def fake_client_cls(*, api_key: object) -> MagicMock:
        captured["api_key"] = api_key
        return MagicMock()

    monkeypatch.setattr(vision.genai, "Client", fake_client_cls)
    fake_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        gemini_api_key="fake-gemini-key",
    )
    monkeypatch.setattr(vision, "get_settings", lambda: fake_settings)

    vision._client()

    assert captured["api_key"] == "fake-gemini-key"
    assert isinstance(captured["api_key"], str)
    assert not isinstance(captured["api_key"], SecretStr)


def test_client_jest_budowany_tylko_raz_na_proces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`recognize_photo` wywolane wielokrotnie ma reuzywac jednego klienta.

    Przed poprawka kazde wywolanie budowalo nowy `genai.Client` - GC zbieral
    porzucone instancje i zamykal ich wspoldzielony transport httpx, przez
    co analiza kolejnych zdjec partii w tym samym procesie wywalala sie na
    "Cannot send a request, as the client has been closed" (dzialalo tylko
    pojedyncze zapytanie w swiezym procesie).
    """
    created_clients: list[MagicMock] = []

    def fake_client_cls(*, api_key: object) -> MagicMock:
        client = MagicMock()
        client.models.generate_content.return_value = MagicMock(
            parsed=PhotoExtraction(
                platform=PlatformCode.OTHER,
                title_confident=False,
                price_confident=False,
            )
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(vision.genai, "Client", fake_client_cls)
    fake_settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        gemini_api_key="fake-gemini-key",
    )
    monkeypatch.setattr(vision, "get_settings", lambda: fake_settings)

    vision.recognize_photo(_jpeg_bytes())
    vision.recognize_photo(_jpeg_bytes())

    assert len(created_clients) == 1


def test_recognize_photo_zwraca_sparsowany_wynik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = PhotoExtraction(
        title="Bloodborne",
        platform=PlatformCode.PS4_PS5,
        title_confident=True,
        price_confident=False,
        price_pln=None,
    )
    fake_models = MagicMock()
    fake_models.generate_content.return_value = MagicMock(parsed=expected)
    monkeypatch.setattr(vision, "_client", lambda: MagicMock(models=fake_models))

    result = vision.recognize_photo(_jpeg_bytes())

    assert result is expected
    _, kwargs = fake_models.generate_content.call_args
    assert kwargs["config"].temperature == 0
    assert kwargs["config"].response_schema is PhotoExtraction
    # Sprawdzona implementacja: obraz jako obiekt PIL.Image, nie types.Part,
    # w kolejnosci [prompt, obraz].
    assert kwargs["contents"][0] == vision.PROMPT
    assert isinstance(kwargs["contents"][1], Image.Image)


def test_recognize_photo_ponawia_bledy_przejsciowe_i_w_koncu_sie_udaje(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = PhotoExtraction(
        platform=PlatformCode.OTHER, title_confident=False, price_confident=False
    )
    fake_models = MagicMock()
    fake_models.generate_content.side_effect = [
        RuntimeError("blad przejsciowy 1"),
        RuntimeError("blad przejsciowy 2"),
        MagicMock(parsed=expected),
    ]
    monkeypatch.setattr(vision, "_client", lambda: MagicMock(models=fake_models))

    result = vision.recognize_photo(_jpeg_bytes())

    assert result is expected
    assert fake_models.generate_content.call_count == vision.RETRY_ATTEMPTS


def test_recognize_photo_wyczerpanych_prob_zwraca_bezpieczny_wynik(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_models = MagicMock()
    fake_models.generate_content.side_effect = RuntimeError("timeout")
    monkeypatch.setattr(vision, "_client", lambda: MagicMock(models=fake_models))

    result = vision.recognize_photo(_jpeg_bytes())

    assert result.title_confident is False
    assert result.price_confident is False
    assert result.note is not None
    assert fake_models.generate_content.call_count == vision.RETRY_ATTEMPTS


def test_recognize_photo_limit_429_przerywa_bez_ponawiania(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Limit dzienny (429) NIE jest ponawiany i propaguje sie jako wyjatek.

    Czekanie nic tu nie da (limit odnawia sie raz na dobe) - w
    przeciwienstwie do innych bledow, ten MA przerwac analize calej partii
    zamiast zwrocic bezpieczny wynik i pozwolic wywolujacemu isc dalej.
    """
    fake_models = MagicMock()
    fake_models.generate_content.side_effect = _client_error(429)
    monkeypatch.setattr(vision, "_client", lambda: MagicMock(models=fake_models))

    with pytest.raises(vision.GeminiQuotaExceededError, match="limit"):
        vision.recognize_photo(_jpeg_bytes())

    assert fake_models.generate_content.call_count == 1


def test_recognize_photo_inny_blad_klienta_nie_jest_ponawiany(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Blad 4xx inny niz 429 (np. zle zadanie) jest deterministyczny - bez retry."""
    fake_models = MagicMock()
    fake_models.generate_content.side_effect = _client_error(400)
    monkeypatch.setattr(vision, "_client", lambda: MagicMock(models=fake_models))

    result = vision.recognize_photo(_jpeg_bytes())

    assert result.title_confident is False
    assert result.price_confident is False
    assert fake_models.generate_content.call_count == 1


def test_recognize_photo_odpowiedzi_niezgodnej_ze_schematem_nie_rzuca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_models = MagicMock()
    fake_models.generate_content.return_value = MagicMock(parsed=None, text="cos")
    monkeypatch.setattr(vision, "_client", lambda: MagicMock(models=fake_models))

    result = vision.recognize_photo(_jpeg_bytes())

    assert result.title_confident is False
    assert result.price_confident is False
    assert result.note is not None
