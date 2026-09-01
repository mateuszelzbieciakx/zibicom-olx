"""Grupowanie rozpoznanych zdjęć partii w egzemplarze do zatwierdzenia.

Funkcje w tym module są czyste (bez bazy danych i bez sieci) - cała logika
grupowania i scalania jest w pełni testowalna na samych obiektach
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
    """Jeden przyszły egzemplarz złożony z jednego lub wielu zdjęć.

    Attributes:
        title: Scalony tytuł (pierwszy niepusty w grupie), albo None.
        platform: Platforma wybrana większością głosów zdjęć w grupie.
        platform_other: Opis platformy, gdy platform == "other".
        price_pln: Pierwsza odczytana cena w grupie, albo None.
        condition: 'new', jeśli którekolwiek zdjęcie wskazuje 'new',
            w przeciwnym razie 'used'.
        warning: Zbiorcze ostrzeżenie tekstowe do podświetlenia w widoku
            zatwierdzania (np. brak ceny, niepewny tytuł); None, gdy grupa
            nie budzi żadnych wątpliwości.
        photos: Źródłowe wyniki rozpoznania zdjęć wchodzących w skład grupy,
            w kolejności zrobienia.
    """

    title: str | None
    platform: PlatformCode
    platform_other: str | None
    price_pln: Decimal | None
    condition: Literal["new", "used"]
    warning: str | None
    photos: list[PhotoExtraction]


def normalize_title(title: str) -> str:
    """Normalizuje tytuł do porównań: bez diakrytyków, interpunkcji, lowercase.

    Args:
        title: Surowy tytuł odczytany z okładki.

    Returns:
        Znormalizowana postać tytułu, używana wyłącznie do porównań
        równości (nie do wyświetlania).
    """
    without_diacritics = unicodedata.normalize("NFKD", title)
    ascii_only = without_diacritics.encode("ascii", "ignore").decode("ascii")
    without_punctuation = _PUNCTUATION_RE.sub("", ascii_only)
    collapsed = _WHITESPACE_RE.sub(" ", without_punctuation).strip()
    return collapsed.lower()


class IncrementalGrouper:
    """Grupuje zdjęcia jednej partii w egzemplarze przyrostowo, zdjęcie po zdjęciu.

    Równoważnik `group_photos` dla ekstrakcji przyrostowej (partie
    zapisywane do intake_item w miarę przetwarzania, zamiast dopiero po
    rozpoznaniu całej partii) - identyczna reguła granicy grupy
    (is_front, z regułą zapasową po tytule - patrz `group_photos`), ale
    zwraca każdy domknięty egzemplarz NATYCHMIAST po pojawieniu się
    zdjęcia, które zaczyna kolejną grupę, zamiast czekać na koniec całej
    partii.

    Stan (bieżąca, jeszcze niedomknięta grupa i ostatni znormalizowany
    tytuł) jest prywatny. Przy wznowieniu przerwanej ekstrakcji odtwarza
    się go, podając (`add_photo`) od nowa WSZYSTKIE zdjęcia partii w
    kolejności - także te, których domknięte grupy zostały już zapisane w
    poprzednim przebiegu (wywołujący po prostu ignoruje zwrócone dla nich
    `GroupedListing` i nie zapisuje ich drugi raz) - inaczej reguła
    zapasowa po tytule (dla is_front=None) działałaby na niepełnej
    historii i mogła wyznaczyć inną granicę niż w pierwszym przebiegu.
    """

    def __init__(self) -> None:
        """Inicjuje pusty stan - bez otwartej grupy i bez znanego tytułu."""
        self._current: list[PhotoExtraction] = []
        self._last_title_norm: str | None = None

    def add_photo(self, photo: PhotoExtraction) -> GroupedListing | None:
        """Dołącza kolejne zdjęcie i zwraca domknięty egzemplarz, jeśli ten je zamknął.

        Args:
            photo: Wynik rozpoznania kolejnego zdjęcia partii, w kolejności
                zrobienia.

        Returns:
            Scalony POPRZEDNI egzemplarz, jeśli to zdjęcie zaczyna nową
            grupę (samo trafia do nowej, bieżącej grupy) - None, gdy
            dołączyło do wciąż otwartej grupy (jeszcze nie ma czego
            zapisywać).
        """
        current_title_norm = normalize_title(photo.title) if photo.title else None

        starts_new_group = photo.is_front is True or (
            photo.is_front is None
            and current_title_norm is not None
            and self._last_title_norm is not None
            and current_title_norm != self._last_title_norm
        )

        closed = None
        if starts_new_group and self._current:
            closed = _merge_group(self._current)
            self._current = []

        self._current.append(photo)
        if current_title_norm is not None:
            self._last_title_norm = current_title_norm

        return closed

    def close(self) -> GroupedListing | None:
        """Domyka ostatnią, wciąż otwartą grupę (koniec partii).

        Returns:
            Scalony ostatni egzemplarz, albo None, gdy nie ma otwartej
            grupy (jeszcze żadne zdjęcie nie zostało podane).
        """
        if not self._current:
            return None
        closed = _merge_group(self._current)
        self._current = []
        return closed


def group_photos(extractions: list[PhotoExtraction]) -> list[GroupedListing]:
    """Grupuje kolejne zdjęcia jednej partii w egzemplarze.

    Granica między egzemplarzami to zdjęcie PRZODU pudełka (is_front=True),
    NIE zmiana tytułu. To krytyczne: sklep trzyma po kilka kopii jednego
    wydania, a dwie kopie sfotografowane po kolei mają identyczny tytuł -
    grupowanie po tytule scalałoby je w jedną pozycję i zgubiło drugą
    ofertę razem z jej ceną.

    Gdy is_front jest None (model niepewny, która to strona), obowiązuje
    reguła zapasowa: inny znormalizowany tytuł niż w bieżącej grupie
    oznacza nowy egzemplarz. Zdjęcie bez tytułu nigdy samo z siebie nie
    zaczyna nowej grupy - dołącza do bieżącej.

    Cienka warstwa nad `IncrementalGrouper` (jeden przebieg zamiast
    zwracania każdej grupy osobno) - patrz tamten docstring po pełny opis
    algorytmu.

    Args:
        extractions: Wyniki rozpoznania kolejnych zdjęć partii, w
            kolejności zrobienia (przód, tył, [wnętrze], ...).

    Returns:
        Lista zgrupowanych egzemplarzy, w kolejności wystąpienia pierwszego
        zdjęcia każdej grupy. Pusta lista dla pustej partii.
    """
    grouper = IncrementalGrouper()
    groups = [
        closed
        for photo in extractions
        if (closed := grouper.add_photo(photo)) is not None
    ]
    last = grouper.close()
    if last is not None:
        groups.append(last)
    return groups


def _merge_group(photos: list[PhotoExtraction]) -> GroupedListing:
    """Scala zdjęcia jednej grupy w opis pojedynczego egzemplarza.

    Args:
        photos: Zdjęcia należące do jednego egzemplarza (co najmniej jedno).

    Returns:
        Scalony opis egzemplarza wraz ze zbiorczym ostrzeżeniem.
    """
    title_source = next((p for p in photos if p.title), None)
    title = title_source.title if title_source else None

    price_source = next((p for p in photos if p.price_pln is not None), None)
    price_pln = price_source.price_pln if price_source else None

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

    # Pewność bierzemy WYŁĄCZNIE ze zdjęcia, które faktycznie dostarczyło
    # wartość - zdjęcie tyłu pudełka nie widzi cenówki ani tytułu, więc jego
    # (z definicji niepewne) flagi nie mogą obniżać pewności wartości
    # odczytanej z przodu.
    issues: list[str] = []
    if title_source is None:
        issues.append("brak tytułu")
    elif not title_source.title_confident:
        issues.append("niepewny tytuł")
    if price_source is None:
        issues.append("brak ceny")
    elif not price_source.price_confident:
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
