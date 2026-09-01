"""Normalizacja zdjęć produktowych oraz upload/skasowanie w Cloudflare R2.

OLX nie przyjmuje binarnego uploadu zdjęć - w payloadzie ogłoszenia podaje
się listę URL-i, a OLX sam POBIERA pliki spod wskazanego adresu. Zdjęcia
muszą więc być publicznie osiągalne z internetu ZANIM wyślemy ogłoszenie.
Aplikacja chodzi lokalnie na Macu za NAT-em, więc serwowanie z dysku
odpada - pliki trafiają do publicznego bucketu R2.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from uuid import uuid4

import boto3
import pillow_heif
from botocore.client import BaseClient
from PIL import Image, ImageOps

from zibicom.config import get_settings

pillow_heif.register_heif_opener()

MAX_DIMENSION_PX = 1600
JPEG_QUALITY = 80


def normalize_photo(raw: bytes) -> bytes:
    """Normalizuje surowe zdjęcie (HEIC/JPEG/PNG) do publikowalnego JPEG-a.

    Usuwamy metadane EXIF - to nie kosmetyka. iPhone zapisuje w EXIF
    współrzędne GPS miejsca zrobienia zdjęcia, a znormalizowany plik trafia
    pod publiczny URL w R2. Bez usunięcia EXIF-a każdy, kto zobaczy
    ogłoszenie na OLX, poznałby dokładną lokalizację sklepu. Orientacja z
    EXIF (pion/poziom telefonu) jest natomiast najpierw ZASTOSOWANA do
    pikseli (`ImageOps.exif_transpose`) - inaczej, po odrzuceniu metadanych,
    zdjęcia z iPhone'a wyszłyby obrócone.

    Args:
        raw: Surowa zawartość pliku zdjęcia (HEIC, JPEG lub PNG).

    Returns:
        Bajty znormalizowanego zdjęcia w formacie JPEG (RGB, maks. 1600 px
        dłuższego boku, jakość 80, bez EXIF).

    Raises:
        ValueError: Gdy `raw` nie da się zdekodować jako obraz.
    """
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise ValueError(
            "Nie udało się odczytać pliku jako obrazu (HEIC/JPEG/PNG)."
        ) from exc

    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")
    image.thumbnail((MAX_DIMENSION_PX, MAX_DIMENSION_PX), Image.Resampling.LANCZOS)

    output = io.BytesIO()
    # Brak argumentu exif= => Pillow nie zapisuje żadnych metadanych EXIF
    # do wyjściowego JPEG-a, niezależnie od tego, co niesie obraz wejściowy.
    image.save(output, format="JPEG", quality=JPEG_QUALITY)
    return output.getvalue()


def _r2_client() -> BaseClient:
    """Buduje klienta boto3 S3 skonfigurowanego pod Cloudflare R2.

    Returns:
        Klient boto3 "s3" wskazujący na endpoint R2 z konfiguracji.
    """
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key_id.get_secret_value(),
        aws_secret_access_key=settings.r2_secret_access_key.get_secret_value(),
        region_name="auto",
    )


def upload_photo(jpeg_bytes: bytes) -> str:
    """Wysyła znormalizowane zdjęcie JPEG do R2 i zwraca jego publiczny URL.

    Args:
        jpeg_bytes: Bajty zdjęcia JPEG (wynik `normalize_photo`).

    Returns:
        Pełny, publiczny URL zdjęcia w R2, gotowy do wpisania w payload OLX.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    key = f"{now:%Y}/{now:%m}/{uuid4().hex}.jpg"

    _r2_client().put_object(
        Bucket=settings.r2_bucket,
        Key=key,
        Body=jpeg_bytes,
        ContentType="image/jpeg",
    )

    return f"{settings.r2_public_base_url}/{key}"


def download_photo(public_url: str) -> bytes:
    """Pobiera zawartość zdjęcia z R2 na podstawie jego publicznego URL-a.

    Używane w kroku rozpoznawania AI (`zibicom.intake.extract_batch`) -
    zdjęcia partii istnieją tylko jako obiekty w R2, więc trzeba je stamtąd
    ściągnąć przed przekazaniem do Gemini.

    Args:
        public_url: Publiczny URL zdjęcia zwrócony wcześniej przez
            `upload_photo`.

    Returns:
        Bajty zdjęcia.

    Raises:
        ValueError: Gdy URL nie należy do skonfigurowanego publicznego
            bucketu R2 (np. literówka albo dane z innego środowiska).
    """
    settings = get_settings()
    prefix = f"{settings.r2_public_base_url}/"
    if not public_url.startswith(prefix):
        raise ValueError(
            f"URL nie pochodzi ze skonfigurowanego bucketu R2: {public_url!r}"
        )

    key = public_url.removeprefix(prefix)
    response = _r2_client().get_object(Bucket=settings.r2_bucket, Key=key)
    return response["Body"].read()


def delete_photo(public_url: str) -> None:
    """Kasuje zdjęcie z R2 na podstawie jego publicznego URL-a.

    Używane, gdy oferta znika po sprzedaży i zdjęcie przestaje być
    potrzebne.

    Args:
        public_url: Publiczny URL zdjęcia zwrócony wcześniej przez
            `upload_photo`.

    Raises:
        ValueError: Gdy URL nie należy do skonfigurowanego publicznego
            bucketu R2 (np. literówka albo dane z innego środowiska).
    """
    settings = get_settings()
    prefix = f"{settings.r2_public_base_url}/"
    if not public_url.startswith(prefix):
        raise ValueError(
            f"URL nie pochodzi ze skonfigurowanego bucketu R2: {public_url!r}"
        )

    key = public_url.removeprefix(prefix)
    _r2_client().delete_object(Bucket=settings.r2_bucket, Key=key)
