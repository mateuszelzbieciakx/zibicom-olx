"""Normalizacja zdjec produktowych oraz upload/skasowanie w Cloudflare R2.

OLX nie przyjmuje binarnego uploadu zdjec - w payloadzie ogloszenia podaje
sie liste URL-i, a OLX sam POBIERA pliki spod wskazanego adresu. Zdjecia
musza wiec byc publicznie osiagalne z internetu ZANIM wyslemy ogloszenie.
Aplikacja chodzi lokalnie na Macu za NAT-em, wiec serwowanie z dysku
odpada - pliki trafiaja do publicznego bucketu R2.
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
    """Normalizuje surowe zdjecie (HEIC/JPEG/PNG) do publikowalnego JPEG-a.

    Usuwamy metadane EXIF - to nie kosmetyka. iPhone zapisuje w EXIF
    wspolrzedne GPS miejsca zrobienia zdjecia, a znormalizowany plik trafia
    pod publiczny URL w R2. Bez usuniecia EXIF-a kazdy, kto zobaczy
    ogloszenie na OLX, poznalby dokladna lokalizacje sklepu. Orientacja z
    EXIF (pion/poziom telefonu) jest natomiast najpierw ZASTOSOWANA do
    pikseli (`ImageOps.exif_transpose`) - inaczej, po odrzuceniu metadanych,
    zdjecia z iPhone'a wyszlyby obrocone.

    Args:
        raw: Surowa zawartosc pliku zdjecia (HEIC, JPEG lub PNG).

    Returns:
        Bajty znormalizowanego zdjecia w formacie JPEG (RGB, maks. 1600 px
        dluzszego boku, jakosc 80, bez EXIF).

    Raises:
        ValueError: Gdy `raw` nie da sie zdekodowac jako obraz.
    """
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise ValueError(
            "Nie udalo sie odczytac pliku jako obrazu (HEIC/JPEG/PNG)."
        ) from exc

    image = ImageOps.exif_transpose(image)
    image = image.convert("RGB")
    image.thumbnail((MAX_DIMENSION_PX, MAX_DIMENSION_PX), Image.Resampling.LANCZOS)

    output = io.BytesIO()
    # Brak argumentu exif= => Pillow nie zapisuje zadnych metadanych EXIF
    # do wyjsciowego JPEG-a, niezaleznie od tego, co niesie obraz wejsciowy.
    image.save(output, format="JPEG", quality=JPEG_QUALITY)
    return output.getvalue()


def _r2_client() -> BaseClient:
    """Buduje klienta boto3 S3 skonfigurowanego pod Cloudflare R2.

    Returns:
        Klient boto3 "s3" wskazujacy na endpoint R2 z konfiguracji.
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
    """Wysyla znormalizowane zdjecie JPEG do R2 i zwraca jego publiczny URL.

    Args:
        jpeg_bytes: Bajty zdjecia JPEG (wynik `normalize_photo`).

    Returns:
        Pelny, publiczny URL zdjecia w R2, gotowy do wpisania w payload OLX.
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


def delete_photo(public_url: str) -> None:
    """Kasuje zdjecie z R2 na podstawie jego publicznego URL-a.

    Uzywane, gdy oferta znika po sprzedazy i zdjecie przestaje byc
    potrzebne.

    Args:
        public_url: Publiczny URL zdjecia zwrocony wczesniej przez
            `upload_photo`.

    Raises:
        ValueError: Gdy URL nie nalezy do skonfigurowanego publicznego
            bucketu R2 (np. literowka albo dane z innego srodowiska).
    """
    settings = get_settings()
    prefix = f"{settings.r2_public_base_url}/"
    if not public_url.startswith(prefix):
        raise ValueError(
            f"URL nie pochodzi ze skonfigurowanego bucketu R2: {public_url!r}"
        )

    key = public_url.removeprefix(prefix)
    _r2_client().delete_object(Bucket=settings.r2_bucket, Key=key)
