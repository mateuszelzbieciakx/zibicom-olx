"""Grupowanie rozpoznanych zdjec partii w egzemplarze do zatwierdzenia.

Funkcje w tym module sa czyste (bez bazy danych i bez sieci) - cala logika
grupowania i scalania jest w pelni testowalna na samych obiektach
PhotoExtraction.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from zibicom.models import PhotoExtraction, PlatformCode

_PUNCTUATION_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")


class GroupedListing(BaseModel):
    """Jeden przyszly egzemplarz zlozony z jednego lub wielu zdjec.

    Attributes:
        title: Scalony tytul (pierwszy niepusty w grupie), albo None.
        platform: Platforma wybrana wiekszoscia glosow zdjec w grupie.
        platform_other: Opis platformy, gdy platform == "other".
        price_pln: Pierwsza odczytana cena w grupie, albo None.
        condition: 'new', jesli ktorekolwiek zdjecie wskazuje 'new',
            w przeciwnym razie 'used'.
        warning: Zbiorcze ostrzezenie tekstowe do podswietlenia w widoku
            zatwierdzania (np. brak ceny, niepewny tytul); None, gdy grupa
            nie budzi zadnych watpliwosci.
        photos: Zrodlowe wyniki rozpoznania zdjec wchodzacych w sklad grupy,
            w kolejnosci zrobienia.
    """

    title: str | None
    platform: PlatformCode
    platform_other: str | None
    price_pln: Decimal | None
    condition: Literal["new", "used"]
    warning: str | None
    photos: list[PhotoExtraction]


def normalize_title(title: str) -> str:
    """Normalizuje tytul do porownan: bez diakrytykow, interpunkcji, lowercase.

    Args:
        title: Surowy tytul odczytany z okladki.

    Returns:
        Znormalizowana postac tytulu, uzywana wylacznie do porownan
        rownosci (nie do wyswietlania).
    """
    without_diacritics = unicodedata.normalize("NFKD", title)
    ascii_only = without_diacritics.encode("ascii", "ignore").decode("ascii")
    without_punctuation = _PUNCTUATION_RE.sub("", ascii_only)
    collapsed = _WHITESPACE_RE.sub(" ", without_punctuation).strip()
    return collapsed.lower()


def group_photos(extractions: list[PhotoExtraction]) -> list[GroupedListing]:
    """Grupuje kolejne zdjecia jednej partii w egzemplarze.

    Granica miedzy egzemplarzami to zdjecie PRZODU pudelka (is_front=True),
    NIE zmiana tytulu. To krytyczne: sklep trzyma po kilka kopii jednego
    wydania, a dwie kopie sfotografowane po kolei maja identyczny tytul -
    grupowanie po tytule scalaloby je w jedna pozycje i zgubilo druga
    oferte razem z jej cena.

    Gdy is_front jest None (model niepewny, ktora to strona), obowiazuje
    regula zapasowa: inny znormalizowany tytul niz w biezacej grupie
    oznacza nowy egzemplarz. Zdjecie bez tytulu nigdy samo z siebie nie
    zaczyna nowej grupy - dolacza do biezacej.

    Args:
        extractions: Wyniki rozpoznania kolejnych zdjec partii, w
            kolejnosci zrobienia (przod, tyl, [wnetrze], ...).

    Returns:
        Lista zgrupowanych egzemplarzy, w kolejnosci wystapienia pierwszego
        zdjecia kazdej grupy. Pusta lista dla pustej partii.
    """
    groups: list[list[PhotoExtraction]] = []
    last_title_norm: str | None = None

    for photo in extractions:
        current_title_norm = normalize_title(photo.title) if photo.title else None

        starts_new_group = photo.is_front is True or (
            photo.is_front is None
            and current_title_norm is not None
            and last_title_norm is not None
            and current_title_norm != last_title_norm
        )

        if not groups or starts_new_group:
            groups.append([photo])
        else:
            groups[-1].append(photo)

        if current_title_norm is not None:
            last_title_norm = current_title_norm

    return [_merge_group(group) for group in groups]


def _merge_group(photos: list[PhotoExtraction]) -> GroupedListing:
    """Scala zdjecia jednej grupy w opis pojedynczego egzemplarza.

    Args:
        photos: Zdjecia nalezace do jednego egzemplarza (co najmniej jedno).

    Returns:
        Scalony opis egzemplarza wraz ze zbiorczym ostrzezeniem.
    """
    title = next((p.title for p in photos if p.title), None)
    price_pln = next((p.price_pln for p in photos if p.price_pln is not None), None)
    condition: Literal["new", "used"] = (
        "new" if any(p.condition == "new" for p in photos) else "used"
    )

    platform_counts = Counter(p.platform for p in photos)
    platform = platform_counts.most_common(1)[0][0]
    platform_other = next(
        (
            p.platform_other
            for p in photos
            if p.platform == platform and p.platform_other
        ),
        None,
    )

    issues: list[str] = []
    if title is None:
        issues.append("brak tytulu")
    if price_pln is None:
        issues.append("brak ceny")
    if any(not p.title_confident for p in photos):
        issues.append("niepewny tytul")
    if any(not p.price_confident for p in photos):
        issues.append("niepewna cena")
    warning = "; ".join(issues) if issues else None

    return GroupedListing(
        title=title,
        platform=platform,
        platform_other=platform_other,
        price_pln=price_pln,
        condition=condition,
        warning=warning,
        photos=photos,
    )
