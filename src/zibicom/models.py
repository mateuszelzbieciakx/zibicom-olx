"""Modele Pydantic dla wyników rozpoznawania zdjęć gier przez Gemini."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, field_validator

PRICE_MIN_PLN = Decimal("0")
PRICE_MAX_PLN = Decimal("2000")


class PlatformCode(StrEnum):
    """Kody platform zgodne z kolumną platform.code w bazie danych."""

    PS1 = "ps1"
    PS2 = "ps2"
    PS3 = "ps3"
    PS4_PS5 = "ps4_ps5"
    PS5 = "ps5"
    PSP = "psp"
    PSVITA = "psvita"
    XBOX = "xbox"
    XBOX360 = "xbox360"
    XBOXONE_SX = "xboxone_sx"
    XBOXSX = "xboxsx"
    SWITCH1_2 = "switch1_2"
    SWITCH2 = "switch2"
    OTHER = "other"


class PhotoExtraction(BaseModel):
    """Wynik rozpoznania JEDNEGO zdjęcia egzemplarza przez Gemini.

    Tytuł i cena mają OSOBNE flagi pewności (title_confident, price_confident)
    zamiast jednej wspólnej - to dwa różne typy błędów (źle odczytany napis
    kontra źle odczytana liczba na cenówce) i osoba zatwierdzająca ofertę
    weryfikuje je inaczej.

    Attributes:
        title: Tytuł gry odczytany z okładki, albo None gdy nieczytelny.
        platform: Kod platformy rozpoznany z logo/oprawy graficznej;
            "other", gdy nie da się jej jednoznacznie ustalić.
        platform_other: Opisowa nazwa platformy, gdy platform == "other".
        price_pln: Cena z naklejonej cenówki w PLN, albo None gdy
            nieczytelna lub niewiarygodna.
        condition: Stan egzemplarza ("new"/"used"), albo None gdy nieznany.
        is_front: Czy zdjęcie przedstawia główną okładkę (przód pudełka);
            None, gdy model nie jest pewien.
        title_confident: Czy tytuł został odczytany bez wątpliwości.
        price_confident: Czy cena została odczytana bez wątpliwości.
        note: Krótka uwaga po polsku (np. powód niepewności lub błędu API).
    """

    title: str | None = None
    platform: PlatformCode
    platform_other: str | None = None
    price_pln: Decimal | None = None
    condition: Literal["new", "used"] | None = None
    is_front: bool | None = None
    title_confident: bool
    price_confident: bool
    note: str | None = None

    @field_validator("price_pln")
    @classmethod
    def _odrzuc_niewiarygodna_cene(cls, value: Decimal | None) -> Decimal | None:
        """Odrzuca jako błędny odczyt ceny spoza sensownego zakresu sklepu.

        Args:
            value: Cena zwrócona przez model rozpoznający (może być None).

        Returns:
            Cenę bez zmian, gdy mieści się w przedziale (0, 2000] PLN, w
            przeciwnym razie None.
        """
        if value is None:
            return None
        if value <= PRICE_MIN_PLN or value > PRICE_MAX_PLN:
            return None
        return value
