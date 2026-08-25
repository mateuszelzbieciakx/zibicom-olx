"""Testy klienta Gemini - Gemini jest w pelni zamockowany, zero sieci."""

from unittest.mock import MagicMock

import pytest

from zibicom import vision
from zibicom.models import PhotoExtraction, PlatformCode


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

    result = vision.recognize_photo(b"jpeg-stub-bytes")

    assert result is expected
    _, kwargs = fake_models.generate_content.call_args
    assert kwargs["config"].temperature == 0
    assert kwargs["config"].response_schema is PhotoExtraction


def test_recognize_photo_bledu_api_nie_rzuca_wyjatkiem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_models = MagicMock()
    fake_models.generate_content.side_effect = RuntimeError("timeout")
    monkeypatch.setattr(vision, "_client", lambda: MagicMock(models=fake_models))

    result = vision.recognize_photo(b"jpeg-stub-bytes")

    assert result.title_confident is False
    assert result.price_confident is False
    assert result.note is not None


def test_recognize_photo_odpowiedzi_niezgodnej_ze_schematem_nie_rzuca(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_models = MagicMock()
    fake_models.generate_content.return_value = MagicMock(parsed=None, text="cos")
    monkeypatch.setattr(vision, "_client", lambda: MagicMock(models=fake_models))

    result = vision.recognize_photo(b"jpeg-stub-bytes")

    assert result.title_confident is False
    assert result.price_confident is False
    assert result.note is not None
