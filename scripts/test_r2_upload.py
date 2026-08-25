"""Recznie uruchamiany skrypt: wysyla jedno testowe zdjecie do R2.

Sluzy do weryfikacji konfiguracji (endpoint, bucket, dostep publiczny) -
generuje obraz w pamieci, normalizuje go i wgrywa przez ten sam kod
produkcyjny, ktorego uzywa aplikacja (zibicom.photos).

Uzycie:
    uv run python scripts/test_r2_upload.py
"""

import io

from PIL import Image

from zibicom.photos import normalize_photo, upload_photo


def _sample_jpeg_bytes() -> bytes:
    """Generuje testowy obraz JPEG w pamieci, bez zapisu na dysk.

    Returns:
        Bajty prostego, kolorowego obrazu testowego.
    """
    image = Image.new("RGB", (2000, 1500), color=(255, 120, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def main() -> None:
    """Normalizuje testowy obraz i wysyla go do R2, wypisujac publiczny URL."""
    raw = _sample_jpeg_bytes()
    normalized = normalize_photo(raw)
    url = upload_photo(normalized)
    print(url)


if __name__ == "__main__":
    main()
