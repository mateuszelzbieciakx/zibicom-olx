"""Modele Pydantic dla wynikow rozpoznawania zdjec gier przez Gemini."""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, field_validator

PRICE_MIN_PLN = Decimal("0")
PRICE_MAX_PLN = Decimal("2000")


class PlatformCode(StrEnum):
    """Kody platform zgodne z kolumna platform.code w bazie danych."""

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
    """Wynik rozpoznania JEDNEGO zdjecia egzemplarza przez Gemini.

    Tytul i cena maja OSOBNE flagi pewnosci (title_confident, price_confident)
    zamiast jednej wspolnej - to dwa rozne typy bledow (zle odczytany napis
    kontra zle odczytana liczba na cenowce) i osoba zatwierdzajaca oferte
    weryfikuje je inaczej.

    Attributes:
        title: Tytul gry odczytany z okladki, albo None gdy nieczytelny.
        platform: Kod platformy rozpoznany z logo/oprawy graficznej;
            "other", gdy nie da sie jej jednoznacznie ustalic.
        platform_other: Opisowa nazwa platformy, gdy platform == "other".
        price_pln: Cena z naklejonej cenowki w PLN, albo None gdy
            nieczytelna lub niewiarygodna.
        condition: Stan egzemplarza ("new"/"used"), albo None gdy nieznany.
        is_front: Czy zdjecie przedstawia glowna okladke (przod pudelka);
            None, gdy model nie jest pewien.
        title_confident: Czy tytul zostal odczytany bez watpliwosci.
        price_confident: Czy cena zostala odczytana bez watpliwosci.
        note: Krotka uwaga po polsku (np. powod niepewnosci lub bledu API).
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
        """Odrzuca jako bledny odczyt ceny spoza sensownego zakresu sklepu.

        Args:
            value: Cena zwrocona przez model rozpoznajacy (moze byc None).

        Returns:
            Cene bez zmian, gdy miesci sie w przedziale (0, 2000] PLN, w
            przeciwnym razie None.
        """
        if value is None:
            return None
        if value <= PRICE_MIN_PLN or value > PRICE_MAX_PLN:
            return None
        return value
