"""Testy normalizacji zdjec i wrappera R2 (bez ruchu sieciowego)."""

import io
import re
from unittest.mock import MagicMock

import pytest
from PIL import Image
from pydantic import SecretStr

from zibicom import photos
from zibicom.config import Settings

FAKE_SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    r2_endpoint="https://fake-account.r2.cloudflarestorage.com",
    r2_bucket="test-bucket",
    r2_public_base_url="https://cdn.example.test",
    r2_access_key_id="fake-access-key-id",
    r2_secret_access_key="fake-secret-access-key",
)


def _jpeg_bytes(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), color=(10, 120, 200))
    buffer = io.BytesIO()
    exif = image.getexif()
    exif[0x0112] = 1  # Orientation: normalna, wystarczy zeby EXIF istnial.
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def test_normalizacja_zmniejsza_obraz_wiekszy_niz_1600px() -> None:
    result = photos.normalize_photo(_jpeg_bytes(3000, 2000))

    with Image.open(io.BytesIO(result)) as image:
        assert max(image.size) == 1600
        assert image.size == (1600, 1067)


def test_normalizacja_nie_powieksza_mniejszego_obrazu() -> None:
    result = photos.normalize_photo(_jpeg_bytes(400, 300))

    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (400, 300)


def test_normalizacja_usuwa_metadane_exif() -> None:
    result = photos.normalize_photo(_jpeg_bytes(800, 600))

    with Image.open(io.BytesIO(result)) as image:
        assert "exif" not in image.info
        assert dict(image.getexif()) == {}


def test_normalizacja_niepoprawnych_bajtow_rzuca_value_error() -> None:
    with pytest.raises(ValueError, match="obraz"):
        photos.normalize_photo(b"to na pewno nie jest zdjecie")


def test_r2_client_uzywa_konfiguracji_r2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(photos, "get_settings", lambda: FAKE_SETTINGS)

    client = photos._r2_client()

    assert client.meta.endpoint_url == FAKE_SETTINGS.r2_endpoint
    assert client.meta.region_name == "auto"


def test_r2_client_przekazuje_klucze_jako_zwykly_str(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """boto3 oczekuje `str` - SecretStr trzeba rozpakowac przed przekazaniem.

    Test celowo mockuje `boto3.client` (a nie `photos._r2_client`, jak w
    innych testach tego pliku) - inaczej nie da sie sprawdzic, JAKI typ
    argumentu faktycznie trafia do konstruktora klienta.
    """
    captured: dict[str, object] = {}

    def fake_boto3_client(service_name: str, **kwargs: object) -> MagicMock:
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(photos, "get_settings", lambda: FAKE_SETTINGS)
    monkeypatch.setattr(photos.boto3, "client", fake_boto3_client)

    photos._r2_client()

    for key in ("aws_access_key_id", "aws_secret_access_key"):
        assert isinstance(captured[key], str)
        assert not isinstance(captured[key], SecretStr)
    assert (
        captured["aws_access_key_id"]
        == FAKE_SETTINGS.r2_access_key_id.get_secret_value()
    )
    assert (
        captured["aws_secret_access_key"]
        == FAKE_SETTINGS.r2_secret_access_key.get_secret_value()
    )


def test_upload_photo_wysyla_do_r2_i_zwraca_publiczny_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(photos, "get_settings", lambda: FAKE_SETTINGS)
    fake_client = MagicMock()
    monkeypatch.setattr(photos, "_r2_client", lambda: fake_client)

    url = photos.upload_photo(b"jpeg-stub-bytes")

    assert url.startswith(f"{FAKE_SETTINGS.r2_public_base_url}/")
    key = url.removeprefix(f"{FAKE_SETTINGS.r2_public_base_url}/")
    assert re.fullmatch(r"\d{4}/\d{2}/[0-9a-f]{32}\.jpg", key)

    fake_client.put_object.assert_called_once_with(
        Bucket=FAKE_SETTINGS.r2_bucket,
        Key=key,
        Body=b"jpeg-stub-bytes",
        ContentType="image/jpeg",
    )


def test_download_photo_pobiera_obiekt_po_kluczu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(photos, "get_settings", lambda: FAKE_SETTINGS)
    fake_body = MagicMock()
    fake_body.read.return_value = b"jpeg-bytes"
    fake_client = MagicMock()
    fake_client.get_object.return_value = {"Body": fake_body}
    monkeypatch.setattr(photos, "_r2_client", lambda: fake_client)

    result = photos.download_photo(
        f"{FAKE_SETTINGS.r2_public_base_url}/2026/08/abc123.jpg"
    )

    assert result == b"jpeg-bytes"
    fake_client.get_object.assert_called_once_with(
        Bucket=FAKE_SETTINGS.r2_bucket,
        Key="2026/08/abc123.jpg",
    )


def test_download_photo_odrzuca_url_spoza_bucketu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(photos, "get_settings", lambda: FAKE_SETTINGS)
    fake_client = MagicMock()
    monkeypatch.setattr(photos, "_r2_client", lambda: fake_client)

    with pytest.raises(ValueError, match="R2"):
        photos.download_photo("https://obcy-serwer.test/2026/08/abc123.jpg")

    fake_client.get_object.assert_not_called()


def test_delete_photo_kasuje_obiekt_po_kluczu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(photos, "get_settings", lambda: FAKE_SETTINGS)
    fake_client = MagicMock()
    monkeypatch.setattr(photos, "_r2_client", lambda: fake_client)

    photos.delete_photo(f"{FAKE_SETTINGS.r2_public_base_url}/2026/08/abc123.jpg")

    fake_client.delete_object.assert_called_once_with(
        Bucket=FAKE_SETTINGS.r2_bucket,
        Key="2026/08/abc123.jpg",
    )


def test_delete_photo_odrzuca_url_spoza_bucketu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(photos, "get_settings", lambda: FAKE_SETTINGS)
    fake_client = MagicMock()
    monkeypatch.setattr(photos, "_r2_client", lambda: fake_client)

    with pytest.raises(ValueError, match="R2"):
        photos.delete_photo("https://obcy-serwer.test/2026/08/abc123.jpg")

    fake_client.delete_object.assert_not_called()
