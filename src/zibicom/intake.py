"""Warstwa poczekalni (staging) dla partii zdjęć przetwarzanych przez AI.

Rozpoznanie obrazem jest niepewne (model myli ceny i czasem tytuły), więc
wyniki NIE trafiają bezpośrednio do tabel produkcyjnych `game`/`listing` -
żyją w `intake_batch`/`intake_item`/`intake_photo`, dopóki człowiek ich nie
zatwierdzi. Publikacja zatwierdzonych pozycji do OLX to kolejny krok, poza
zakresem tego modułu.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, field_validator
from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from zibicom import grouping, olx, photos, vision
from zibicom.config import get_settings
from zibicom.models import PhotoExtraction, PlatformCode

logger = logging.getLogger(__name__)


class IntakeError(Exception):
    """Błąd domenowy warstwy intake - komunikat po polsku, gotowy dla klienta."""


class IntakeNotFoundError(IntakeError):
    """Żądany zasób (partia albo pozycja) nie istnieje."""


class IntakeValidationError(IntakeError):
    """Żądanie narusza regułę biznesową poczekalni."""


class PlatformView(BaseModel):
    """Wpis słownika platform do listy wyboru.

    Attributes:
        id: Identyfikator platformy (używany jako platform_id w intake_item).
        code: Kod platformy zgodny z PlatformCode.
        name: Nazwa wyświetlana.
        manufacturer: Producent.
        generation: Etykieta generacji sprzętu, jeśli dotyczy.
    """

    id: int
    code: str
    name: str
    manufacturer: str
    generation: str | None


class IntakeItemView(BaseModel):
    """Pozycja poczekalni gotowa do wyświetlenia w widoku zatwierdzania.

    Attributes:
        id: Identyfikator pozycji.
        batch_id: Partia, do której należy pozycja.
        position: Kolejność pozycji w obrębie partii.
        title: Tytuł (może być None, gdy AI go nie odczytało).
        platform_id: Identyfikator platformy w słowniku, jeśli przypisana.
        platform_code: Kod platformy, jeśli przypisana.
        platform_name: Nazwa platformy, jeśli przypisana.
        platform_manufacturer: Producent platformy, jeśli przypisana.
        platform_other: Opisowa platforma, gdy platform_code == "other".
        price_pln: Cena w PLN (może być None).
        condition: Stan egzemplarza (może być None).
        ai_warning: Zbiorcze ostrzeżenie z grupowania AI.
        status: Status pozycji w cyklu życia poczekalni.
        listing_id: Identyfikator opublikowanej oferty, jeśli już istnieje.
        photo_urls: Publiczne URL-e zdjęć pozycji, w kolejności.
    """

    id: int
    batch_id: int
    position: int
    title: str | None
    platform_id: int | None
    platform_code: str | None
    platform_name: str | None
    platform_manufacturer: str | None
    platform_other: str | None
    price_pln: Decimal | None
    condition: str | None
    ai_warning: str | None
    status: str
    listing_id: int | None
    photo_urls: list[str]


class IntakeItemUpdate(BaseModel):
    """Korekta pól pozycji poczekalni wprowadzana ręcznie przez człowieka.

    Wszystkie pola są opcjonalne - w PATCH przesyła się tylko to, co się
    zmienia (`model_fields_set` decyduje, co trafi do UPDATE-u).

    Attributes:
        title: Poprawiony tytuł.
        platform_id: Poprawiona platforma (id ze słownika `platform`).
        platform_other: Opis platformy, gdy platform_id wskazuje na "other".
        price_pln: Poprawiona cena w PLN.
        condition: Poprawiony stan egzemplarza.
    """

    title: str | None = None
    platform_id: int | None = None
    platform_other: str | None = None
    price_pln: Decimal | None = None
    condition: Literal["new", "used"] | None = None

    @field_validator("price_pln")
    @classmethod
    def _cena_nieujemna(cls, value: Decimal | None) -> Decimal | None:
        """Odrzuca ujemna cene przed wysłaniem jej do bazy.

        Args:
            value: Poprawiona cena w PLN (może być None).

        Returns:
            Cene bez zmian.

        Raises:
            ValueError: Gdy cena jest ujemna.
        """
        if value is not None and value < 0:
            raise ValueError("Cena nie może być ujemna.")
        return value


class BatchView(BaseModel):
    """Wpis partii do listy na ekranie `/ui/batches`.

    Attributes:
        id: Identyfikator partii.
        created_at: Moment utworzenia partii.
        photo_count: Liczba zdjęć wgranych w partii.
        item_count: Liczba pozycji utworzonych przez `extract_batch`.
        status_counts: Rozbicie pozycji wg `intake_item.status`
            (np. {"pending": 3, "published": 1}) - statusy bez żadnej
            pozycji w tej partii są pominięte.
    """

    id: int
    created_at: datetime
    photo_count: int
    item_count: int
    status_counts: dict[str, int]


class ListingStatusView(BaseModel):
    """Stan oferty po synchronizacji z OLX (`sync_advert_status`).

    Attributes:
        id: Identyfikator oferty.
        status: Nasz status (`listing_status`) po zmapowaniu z OLX
            (`_map_olx_status`).
        olx_status: Surowy status zwrócony przez OLX, niezależnie od
            mapowania.
        posted_at: Moment, w ktorym oferta stała się aktywna (None, jeśli
            jeszcze nie była).
    """

    id: int
    status: str
    olx_status: str | None
    posted_at: datetime | None


_ITEM_VIEW_SELECT = """\
SELECT
    ii.id,
    ii.batch_id,
    ii.position,
    ii.title,
    ii.platform_id,
    p.code AS platform_code,
    p.name AS platform_name,
    p.manufacturer::TEXT AS platform_manufacturer,
    ii.platform_other,
    ii.price_pln,
    ii.condition::TEXT AS condition,
    ii.ai_warning,
    ii.status::TEXT AS status,
    ii.listing_id
FROM intake_item ii
LEFT JOIN platform p ON p.id = ii.platform_id
"""


async def _batch_exists(session: AsyncSession, batch_id: int) -> bool:
    """Sprawdza, czy partia o podanym id istnieje.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.

    Returns:
        True, jeśli partia istnieje.
    """
    result = await session.execute(
        text("SELECT 1 FROM intake_batch WHERE id = :batch_id"),
        {"batch_id": batch_id},
    )
    return result.first() is not None


async def _photo_urls_for_item(session: AsyncSession, item_id: int) -> list[str]:
    """Zwraca publiczne URL-e zdjęć przypisanych do pozycji, w kolejności.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.

    Returns:
        Lista URL-i zdjęć posortowana wg intake_photo.position.
    """
    result = await session.execute(
        text(
            "SELECT public_url FROM intake_photo "
            "WHERE item_id = :item_id ORDER BY position"
        ),
        {"item_id": item_id},
    )
    return [row[0] for row in result.all()]


def _row_to_item_view(row: Row[Any], photo_urls: list[str]) -> IntakeItemView:
    """Sklada wiersz z `_ITEM_VIEW_SELECT` i URL-e zdjęć w IntakeItemView.

    Args:
        row: Wiersz (mapping) zwrócony przez zapytanie `_ITEM_VIEW_SELECT`.
        photo_urls: URL-e zdjęć pozycji, w kolejności.

    Returns:
        Zlozony widok pozycji poczekalni.
    """
    mapping = row._mapping
    return IntakeItemView(
        id=mapping["id"],
        batch_id=mapping["batch_id"],
        position=mapping["position"],
        title=mapping["title"],
        platform_id=mapping["platform_id"],
        platform_code=mapping["platform_code"],
        platform_name=mapping["platform_name"],
        platform_manufacturer=mapping["platform_manufacturer"],
        platform_other=mapping["platform_other"],
        price_pln=mapping["price_pln"],
        condition=mapping["condition"],
        ai_warning=mapping["ai_warning"],
        status=mapping["status"],
        listing_id=mapping["listing_id"],
        photo_urls=photo_urls,
    )


async def _get_item_view(session: AsyncSession, item_id: int) -> IntakeItemView:
    """Pobiera pelny widok jednej pozycji poczekalni.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.

    Returns:
        Widok pozycji.

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
    """
    result = await session.execute(
        text(f"{_ITEM_VIEW_SELECT} WHERE ii.id = :item_id"),
        {"item_id": item_id},
    )
    row = result.first()
    if row is None:
        raise IntakeNotFoundError(f"Pozycja o id {item_id} nie istnieje.")

    photo_urls = await _photo_urls_for_item(session, item_id)
    return _row_to_item_view(row, photo_urls)


async def create_batch(
    session: AsyncSession, files: list[tuple[str | None, bytes]]
) -> int:
    """Tworzy nową partię poczekalni i wgrywa jej zdjęcia do R2.

    Kolejność plikow na wejsciu jest zachowywana jako `intake_photo.position`
    - ta kolejność niesie informacje o granicach egzemplarzy, wykorzystywana
    później przez `extract_batch`/`zibicom.grouping.group_photos`.

    Args:
        session: Sesja bazy danych.
        files: Lista (nazwa_pliku, surowe_bajty) w kolejności wgrania.

    Returns:
        Identyfikator utworzonej partii.

    Raises:
        IntakeValidationError: Gdy lista plikow jest pusta albo ktorys plik
            nie daje się zdekodować jako obraz.
    """
    if not files:
        raise IntakeValidationError("Należy przeslac co najmniej jedno zdjęcie.")

    batch_result = await session.execute(
        text("INSERT INTO intake_batch DEFAULT VALUES RETURNING id")
    )
    batch_id = batch_result.scalar_one()

    for position, (filename, raw) in enumerate(files, start=1):
        try:
            normalized = photos.normalize_photo(raw)
        except ValueError as exc:
            raise IntakeValidationError(
                f"Plik {filename or f'#{position}'} nie jest poprawnym zdjęciem: {exc}"
            ) from exc

        public_url = photos.upload_photo(normalized)

        await session.execute(
            text(
                "INSERT INTO intake_photo "
                "(batch_id, position, original_filename, public_url) "
                "VALUES (:batch_id, :position, :filename, :public_url)"
            ),
            {
                "batch_id": batch_id,
                "position": position,
                "filename": filename,
                "public_url": public_url,
            },
        )

    await session.commit()
    return batch_id


async def _platform_id_for_code(
    session: AsyncSession, code: PlatformCode
) -> int | None:
    """Odnajduje id platformy w słowniku po jej kodzie.

    Args:
        session: Sesja bazy danych.
        code: Kod platformy rozpoznany przez AI.

    Returns:
        Identyfikator platformy, albo None, gdy kodu nie ma w słowniku
        (np. słownik nie został jeszcze zaladowany dla nowej platformy).
    """
    result = await session.execute(
        text("SELECT id FROM platform WHERE code = :code"),
        {"code": code.value},
    )
    row = result.first()
    return row[0] if row is not None else None


async def _save_group(
    session: AsyncSession,
    batch_id: int,
    position: int,
    group: grouping.GroupedListing,
    photo_ids: list[int],
) -> int:
    """Zapisuje jeden domknięty egzemplarz jako intake_item i COMMITUJE.

    Insert + przypisanie zdjęć + commit w jednym kroku (nie tylko `add` do
    sesji): operator ma zobaczyc każdy nowo domknięty egzemplarz na GUI
    najpóźniej 2s po jego zamknięciu (`extraction_progress` odpytuje z
    zupełnie innej sesji/transakcji), a commit dopiero na końcu całej
    partii oznaczałby wielominutowe oczekiwanie na pierwsza kartę. Commit
    daje też wznawialność "za darmo": jeśli proces padnie między dwoma
    wywołaniami `_save_group`, ta funkcja NIGDY nie została wywołana dla
    bieżącej (jeszcze otwartej) grupy, więc nie ma częściowego zapisu do
    posprzątania - INSERT+UPDATE poniżej są niecommitowane razem, więc
    ewentualny crash między nimi cofa oba (patrz `extract_batch`, który
    przed wywołaniem tej funkcji sprawdza, czy grupa nie została już
    zapisana w poprzednim przebiegu).

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.
        position: Numer pozycji w partii (intake_item.position).
        group: Scalony opis egzemplarza (`grouping.IncrementalGrouper`).
        photo_ids: Id zdjęć należących do tego egzemplarza, w kolejności.

    Returns:
        Id nowo utworzonej pozycji.
    """
    platform_id = await _platform_id_for_code(session, group.platform)

    item_result = await session.execute(
        text(
            "INSERT INTO intake_item "
            "(batch_id, position, title, platform_id, platform_other, "
            " price_pln, condition, ai_warning) "
            "VALUES (:batch_id, :position, :title, :platform_id, "
            " :platform_other, :price_pln, "
            " CAST(:condition AS listing_condition), :ai_warning) "
            "RETURNING id"
        ),
        {
            "batch_id": batch_id,
            "position": position,
            "title": group.title,
            "platform_id": platform_id,
            "platform_other": group.platform_other,
            "price_pln": group.price_pln,
            "condition": group.condition,
            "ai_warning": group.warning,
        },
    )
    item_id = item_result.scalar_one()

    await session.execute(
        text("UPDATE intake_photo SET item_id = :item_id WHERE id = ANY(:photo_ids)"),
        {"item_id": item_id, "photo_ids": photo_ids},
    )
    await session.commit()
    return item_id


def _recognize_with_fallback(
    photo_id: int, batch_id: int, public_url: str
) -> PhotoExtraction:
    """Rozpoznaje jedno zdjęcie; błąd tego JEDNEGO zdjęcia nie przerywa partii.

    `vision.recognize_photo` już samo łapie większość błędów wywołania
    Gemini i zwraca bezpieczny wynik zamiast rzucać wyjątek - jedyny
    wyjątek od tej reguły to `GeminiQuotaExceededError` (celowo
    propagowany dalej, żeby przerwać całą partię, patrz jego docstring) i
    błąd POBRANIA zdjęcia z R2 (`photos.download_photo`), którego
    `recognize_photo` w ogóle nie widzi. Ta funkcja domyka tę drugą lukę:
    każdy inny błąd (sieć, R2, nieoczekiwany wyjątek) jest logowany i
    zamieniany na PhotoExtraction z notatką zamiast wywracać całą partię -
    jedno wadliwe zdjęcie ma zostać pominięte, nie zablokować reszty.

    Args:
        photo_id: Identyfikator zdjęcia (do logu).
        batch_id: Identyfikator partii (do logu).
        public_url: Publiczny URL zdjęcia do pobrania z R2.

    Returns:
        Wynik rozpoznania, albo bezpieczny placeholder z notatką o błędzie.

    Raises:
        vision.GeminiQuotaExceededError: Gdy wyczerpano dzienny limit
            Gemini - przerywa całą partię (ponawianie nic by nie dało).
    """
    try:
        raw = photos.download_photo(public_url)
        return vision.recognize_photo(raw)
    except vision.GeminiQuotaExceededError:
        raise
    except Exception as exc:
        logger.exception(
            "Rozpoznanie zdjęcia %s (partia %s) nie powiodło się - "
            "kontynuuje partie z pustym opisem tego zdjęcia.",
            photo_id,
            batch_id,
        )
        return PhotoExtraction(
            platform=PlatformCode.OTHER,
            title_confident=False,
            price_confident=False,
            note=f"Błąd rozpoznania: {exc}",
        )


async def extract_batch(session: AsyncSession, batch_id: int) -> int:
    """Puszcza zdjęcia partii przez rozpoznanie AI i grupuje je w pozycje.

    PRZYROSTOWA i WZNAWIALNA. Każdy domknięty egzemplarz (granica -
    `grouping.IncrementalGrouper`, patrz tamten docstring) jest zapisywany
    do intake_item i COMMITOWANY natychmiast (`_save_group`) - operator
    widzi i może edytować pierwsze pozycje, zanim reszta partii się
    doliczy, zamiast czekac na koniec całej partii (przy 150 zdjęciach to
    kilkanaście minut bezczynności).

    WZNAWIALNOŚĆ jest tu najważniejszym wymaganiem: przed tą zmianą
    ekstrakcja była atomowa (cała partia albo nic), więc przerwanie w
    trakcie (restart procesu, błąd) nigdy nie zostawiało częściowego
    stanu do posprzątania. Teraz zostawia - i ponowne wywołanie tej
    funkcji NA TEJ SAMEJ partii MUSI je bezpiecznie kontynuować, a nie
    zdublować. Osiągnięte przez pominięcie:
    - zdjęć z już wypełnionym `ai_raw` (nie wywołuje ponownie ani R2, ani
      Gemini - wczytuje zapisany wcześniej wynik),
    - egzemplarzy, których zdjęcia mają już przypisane `item_id` (nie
      tworzy drugiego intake_item dla tej samej grupy).
    Bez tego drugiego punktu wznowienie tworzyłoby DUPLIKATY pozycji -
    a każda duplikat, który dotrwa do publikacji, to podwojne ogłoszenie
    na OLX (ten błąd już raz w tym projekcie wystąpił). Bezpieczeństwo
    bierze się z tego, że `grouping.IncrementalGrouper` jest odtwarzany
    od zdjęcia #1 partii przy KAŻDYM wywołaniu (również wznowieniu) -
    deterministyczny algorytm na tych samych danych (ai_raw się nie
    zmienia) zawsze wyznacza te same granice grup, więc grupy już
    zapisane w poprzednim przebiegu są rozpoznawane po tym, że ich zdjęcia
    mają już `item_id`, i po prostu pomijane.

    Błąd rozpoznania POJEDYNCZEGO zdjęcia (sieć, R2, nieoczekiwany błąd
    Gemini) nie przerywa partii - `_recognize_with_fallback` loguje go i
    zwraca placeholder z notatką, ekstrakcja partii biegnie dalej.
    Wyjatkiem jest `vision.GeminiQuotaExceededError` (wyczerpany dzienny
    limit Gemini) - to błąd systemowy, nie wada jednego zdjęcia, więc
    nadal przerywa całą partię (status 'failed'), tak jak poprzednio.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.

    Returns:
        Liczba pozycji NOWO utworzonych w TYM wywołaniu (przy wznowieniu
        nie liczy pozycji zapisanych w poprzednim przebiegu).

    Raises:
        IntakeNotFoundError: Gdy partia nie istnieje.
        IntakeError: Gdy partia zostanie przerwana (np. wyczerpany limit
            Gemini) - status partii ustawiany na 'failed'.
    """
    if not await _batch_exists(session, batch_id):
        raise IntakeNotFoundError(f"Partia o id {batch_id} nie istnieje.")

    await session.execute(
        text("UPDATE intake_batch SET status = 'extracting' WHERE id = :batch_id"),
        {"batch_id": batch_id},
    )

    photo_rows = (
        await session.execute(
            text(
                "SELECT id, public_url, ai_raw, item_id FROM intake_photo "
                "WHERE batch_id = :batch_id ORDER BY position"
            ),
            {"batch_id": batch_id},
        )
    ).all()
    next_position = (
        await session.execute(
            text("SELECT COUNT(*) FROM intake_item WHERE batch_id = :batch_id"),
            {"batch_id": batch_id},
        )
    ).scalar_one() + 1

    grouper = grouping.IncrementalGrouper()
    created = 0
    try:
        # (photo_id, czy ta grupa była już zapisana w poprzednim przebiegu)
        # dla zdjęć bieżącej, jeszcze niedomknietej grupy.
        pending: list[tuple[int, bool]] = []

        for photo_id, public_url, ai_raw, item_id in photo_rows:
            pending.append((photo_id, item_id is not None))

            if ai_raw is not None:
                extraction = PhotoExtraction.model_validate(ai_raw)
            else:
                extraction = _recognize_with_fallback(photo_id, batch_id, public_url)
                await session.execute(
                    text(
                        "UPDATE intake_photo SET ai_raw = CAST(:ai_raw AS jsonb) "
                        "WHERE id = :photo_id"
                    ),
                    {
                        "photo_id": photo_id,
                        "ai_raw": json.dumps(extraction.model_dump(mode="json")),
                    },
                )
                # Commit per zdjęcie: GUI odpytuje postęp rozpoznania z
                # zupełnie innej sesji/transakcji (patrz extraction_progress).
                await session.commit()

            closed = grouper.add_photo(extraction)
            if closed is not None:
                group = pending[:-1]
                pending = [pending[-1]]
                already_saved = any(saved for _, saved in group)
                if not already_saved:
                    await _save_group(
                        session,
                        batch_id,
                        next_position,
                        closed,
                        [pid for pid, _ in group],
                    )
                    next_position += 1
                    created += 1

        closed = grouper.close()
        if closed is not None and not any(saved for _, saved in pending):
            await _save_group(
                session, batch_id, next_position, closed, [pid for pid, _ in pending]
            )
            created += 1

        await session.execute(
            text("UPDATE intake_batch SET status = 'review' WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
    except Exception as exc:
        logger.exception("Rozpoznawanie partii %s nie powiodło się.", batch_id)
        await session.rollback()
        await session.execute(
            text("UPDATE intake_batch SET status = 'failed' WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
        await session.commit()
        raise IntakeError(
            f"Rozpoznawanie partii {batch_id} nie powiodło się: {exc}"
        ) from exc

    await session.commit()
    return created


async def extraction_progress(session: AsyncSession, batch_id: int) -> tuple[int, int]:
    """Zwraca postęp ekstrakcji partii do paska postępu na `/ui`.

    Wywoływane z osobnej sesji/transakcji niż ta, w której biegnie
    `extract_batch` w tle - działa tylko dzięki temu, że `extract_batch`
    commituje `ai_raw` per zdjęcie, a nie raz na koniec.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.

    Returns:
        Krotkę (przetworzone, wszystkie): liczba zdjęć z wypełnionym
        `ai_raw` i liczba wszystkich zdjęć partii.
    """
    row = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE ai_raw IS NOT NULL), count(*) "
                "FROM intake_photo WHERE batch_id = :batch_id"
            ),
            {"batch_id": batch_id},
        )
    ).one()
    return row[0], row[1]


async def list_batches(session: AsyncSession) -> list[BatchView]:
    """Zwraca wszystkie partie do ekranu listy, od najnowszej.

    Jedno zapytanie SQL: liczba zdjęć i liczba pozycji są policzone w
    podzapytaniach zgrupowanych po `batch_id` (a nie przez bezpośredni JOIN
    intake_photo + intake_item do intake_batch), żeby nie mnożyć wierszy
    iloczynem kartezjańskim zdjęć i pozycji tej samej partii. Rozbicie wg
    statusu pozycji jest agregowane do jsonb (`jsonb_object_agg`) w kolejnym
    podzapytaniu - bez żadnego zapytania w pętli po partiach (N+1).

    Args:
        session: Sesja bazy danych.

    Returns:
        Partie posortowane malejąco wg id (najnowsza pierwsza).
    """
    rows = (
        await session.execute(
            text(
                """
                SELECT
                    b.id,
                    b.created_at,
                    COALESCE(ph.photo_count, 0) AS photo_count,
                    COALESCE(it.item_count, 0) AS item_count,
                    COALESCE(sc.status_counts, '{}'::jsonb) AS status_counts
                FROM intake_batch b
                LEFT JOIN (
                    SELECT batch_id, COUNT(*) AS photo_count
                    FROM intake_photo
                    GROUP BY batch_id
                ) ph ON ph.batch_id = b.id
                LEFT JOIN (
                    SELECT batch_id, COUNT(*) AS item_count
                    FROM intake_item
                    GROUP BY batch_id
                ) it ON it.batch_id = b.id
                LEFT JOIN (
                    SELECT batch_id, jsonb_object_agg(status, cnt) AS status_counts
                    FROM (
                        SELECT batch_id, status::TEXT AS status, COUNT(*) AS cnt
                        FROM intake_item
                        GROUP BY batch_id, status
                    ) per_status
                    GROUP BY batch_id
                ) sc ON sc.batch_id = b.id
                ORDER BY b.id DESC
                """
            )
        )
    ).all()
    return [BatchView(**row._mapping) for row in rows]


async def _list_items_by_batch(
    session: AsyncSession, batch_id: int, *, after_item_id: int | None = None
) -> list[IntakeItemView]:
    """Wspólna implementacja `list_items`/`list_items_after`.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.
        after_item_id: Gdy podane, zwraca wyłącznie pozycje z id większym
            niż ta wartość (patrz `list_items_after`).

    Returns:
        Pozycje partii w kolejności `intake_item.position`.
    """
    where = "ii.batch_id = :batch_id"
    params: dict[str, Any] = {"batch_id": batch_id}
    if after_item_id is not None:
        where += " AND ii.id > :after_item_id"
        params["after_item_id"] = after_item_id

    rows = (
        await session.execute(
            text(f"{_ITEM_VIEW_SELECT} WHERE {where} ORDER BY ii.position"),
            params,
        )
    ).all()

    items = []
    for row in rows:
        photo_urls = await _photo_urls_for_item(session, row._mapping["id"])
        items.append(_row_to_item_view(row, photo_urls))
    return items


async def list_items(session: AsyncSession, batch_id: int) -> list[IntakeItemView]:
    """Zwraca pozycje partii przygotowane do zatwierdzenia przez człowieka.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.

    Returns:
        Pozycje partii w kolejności `intake_item.position`, każda ze
        składowymi nazwy platformy i lista URL-i własnych zdjęć.

    Raises:
        IntakeNotFoundError: Gdy partia nie istnieje.
    """
    if not await _batch_exists(session, batch_id):
        raise IntakeNotFoundError(f"Partia o id {batch_id} nie istnieje.")
    return await _list_items_by_batch(session, batch_id)


async def list_items_after(
    session: AsyncSession, batch_id: int, after_item_id: int
) -> list[IntakeItemView]:
    """Zwraca pozycje partii utworzone PO danym id - do przyrostowego dopięcia kart.

    Używane przez poling ekstrakcji w toku (fragment postępu): każde
    odpytanie prosi tylko o pozycje nowsze niż ostatnia już pokazana w
    przeglądarce, żeby UI mogło je DOPIĄĆ (`hx-swap-oob="beforeend"`) bez
    przerenderowywania już wyświetlonych kart - inaczej znikałaby
    niezapisana edycja operatora w pierwszej karcie przy kolejnym
    odpytaniu.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.
        after_item_id: Zwróć tylko pozycje z id większym niż ten (0, żeby
            zwrócić wszystkie pozycje partii).

    Returns:
        Pozycje partii z id > after_item_id, w kolejności `intake_item.position`.

    Raises:
        IntakeNotFoundError: Gdy partia nie istnieje.
    """
    if not await _batch_exists(session, batch_id):
        raise IntakeNotFoundError(f"Partia o id {batch_id} nie istnieje.")
    return await _list_items_by_batch(session, batch_id, after_item_id=after_item_id)


async def update_item(
    session: AsyncSession, item_id: int, payload: IntakeItemUpdate
) -> IntakeItemView:
    """Zapisuje reczna korektę pól pozycji poczekalni.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.
        payload: Pola do zmiany (tylko jawnie ustawione trafiają do UPDATE-u).

    Returns:
        Zaktualizowany widok pozycji.

    Raises:
        IntakeNotFoundError: Gdy pozycja albo wskazana platforma nie istnieje.
        IntakeValidationError: Gdy pozycja jest już opublikowana, albo brak
            pól do zmiany.
    """
    current = await _get_item_view(session, item_id)
    if current.status == "published":
        raise IntakeValidationError("Nie można edytować już opublikowanej pozycji.")

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise IntakeValidationError("Brak pól do zmiany.")

    if "platform_id" in fields and fields["platform_id"] is not None:
        exists = await session.execute(
            text("SELECT 1 FROM platform WHERE id = :platform_id"),
            {"platform_id": fields["platform_id"]},
        )
        if exists.first() is None:
            raise IntakeValidationError(
                f"Platforma o id {fields['platform_id']} nie istnieje."
            )

    assignments = []
    params: dict[str, Any] = {"item_id": item_id}
    for field, value in fields.items():
        target = (
            f"CAST(:{field} AS listing_condition)"
            if field == "condition"
            else f":{field}"
        )
        assignments.append(f"{field} = {target}")
        params[field] = value

    await session.execute(
        text(f"UPDATE intake_item SET {', '.join(assignments)} WHERE id = :item_id"),
        params,
    )
    await session.commit()
    return await _get_item_view(session, item_id)


async def approve_item(session: AsyncSession, item_id: int) -> IntakeItemView:
    """Zatwierdza pozycje poczekalni po walidacji kompletności danych.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.

    Returns:
        Zatwierdzony widok pozycji.

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
        IntakeValidationError: Gdy pozycja nie ma statusu "pending", albo
            brakuje jej tytułu lub ceny.
    """
    current = await _get_item_view(session, item_id)
    if current.status != "pending":
        raise IntakeValidationError(
            f"Pozycja ma status '{current.status}' - zatwierdzić można tylko "
            "pozycje ze statusem 'pending'."
        )

    problems = []
    if not current.title:
        problems.append("brak tytułu")
    if current.price_pln is None:
        problems.append("brak ceny")
    if problems:
        raise IntakeValidationError(
            "Nie można zatwierdzić pozycji: " + ", ".join(problems) + "."
        )

    await session.execute(
        text("UPDATE intake_item SET status = 'approved' WHERE id = :item_id"),
        {"item_id": item_id},
    )
    await session.commit()
    return await _get_item_view(session, item_id)


async def reject_item(session: AsyncSession, item_id: int) -> IntakeItemView:
    """Odrzuca pozycje poczekalni.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.

    Returns:
        Odrzucony widok pozycji.

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
        IntakeValidationError: Gdy pozycja nie ma statusu "pending".
    """
    current = await _get_item_view(session, item_id)
    if current.status != "pending":
        raise IntakeValidationError(
            f"Pozycja ma status '{current.status}' - odrzucić można tylko "
            "pozycje ze statusem 'pending'."
        )

    await session.execute(
        text("UPDATE intake_item SET status = 'rejected' WHERE id = :item_id"),
        {"item_id": item_id},
    )
    await session.commit()
    return await _get_item_view(session, item_id)


async def _find_or_create_game(
    session: AsyncSession, title: str, platform_id: int
) -> int:
    """Znajduje istniejącą `game` po (lower(title), platform_id) albo ją tworzy.

    Brak wyścigu między SELECT-em a INSERT-em (klasyczny problem UPSERT-u
    bez unikalnego indeksu) nie jest tu problemem: publikacja jest
    wywoływana ręcznie, pojedynczo, dla JEDNEJ pozycji na raz
    (POST /api/intake/items/{id}/publish) - nie ma współbieżnych zadań o ta
    sama parę (tytuł, platforma), które wymagałyby ON CONFLICT.

    Args:
        session: Sesja bazy danych.
        title: Tytuł gry (dopasowanie case-insensitive).
        platform_id: Id platformy.

    Returns:
        Id istniejącej albo nowo utworzonej `game`.
    """
    existing = (
        await session.execute(
            text(
                "SELECT id FROM game WHERE lower(title) = lower(:title) "
                "AND platform_id = :platform_id"
            ),
            {"title": title, "platform_id": platform_id},
        )
    ).first()
    if existing is not None:
        return existing[0]

    created = await session.execute(
        text(
            "INSERT INTO game (title, platform_id) VALUES (:title, :platform_id) "
            "RETURNING id"
        ),
        {"title": title, "platform_id": platform_id},
    )
    return created.scalar_one()


async def _resolve_platform_for_publish(
    session: AsyncSession, current: IntakeItemView
) -> tuple[str, str, str, str | None, int]:
    """Pobiera i waliduje dane platformy potrzebne do zbudowania payloadu OLX.

    Czysto lokalna walidacja (SELECT + sprawdzenie kategorii) - ZERO wywołań
    OLX. Celowo osobna funkcja, wywoływana w `publish_item` PRZED
    `olx.get_access_token`: brak kategorii dla platformy (przypadek "other")
    ma failować natychmiast, bez wymagania wcześniej ważnej autoryzacji OLX
    - to tani, lokalny błąd, nie powód do angażowania OLX.

    Args:
        session: Sesja bazy danych.
        current: Widok pozycji z już zweryfikowanym platform_id.

    Returns:
        (manufacturer, platform_generation, console_name,
        olx_attribute_value, olx_category_id) - gotowe do przekazania do
        `_build_advert_payload_for_item`.

    Raises:
        IntakeValidationError: Gdy platforma nie istnieje, albo nie ma
            ustalonej kategorii OLX (platform.olx_category_id - przypadek
            "other").
    """
    platform_row = (
        await session.execute(
            text(
                "SELECT name, manufacturer::TEXT AS manufacturer, generation, "
                "olx_attribute_value, olx_category_id "
                "FROM platform WHERE id = :platform_id"
            ),
            {"platform_id": current.platform_id},
        )
    ).first()
    if platform_row is None:
        raise IntakeValidationError(
            f"Platforma o id {current.platform_id} nie istnieje."
        )
    platform_name, manufacturer, generation, olx_attribute_value, olx_category_id = (
        platform_row
    )
    # Kategoria OLX jest per producent (migracja 0005) - "other" celowo nie
    # ma ustalonej kategorii (różne, nieprzewidywalne rodzaje przedmiotów),
    # więc bez tej walidacji create_advert wysłałby ogłoszenie z
    # category_id=NULL/nieprawidłowym zamiast czytelnego błędu PRZED
    # jakimkolwiek wywołaniem OLX.
    if olx_category_id is None:
        raise IntakeValidationError(
            f"Platforma '{platform_name}' nie ma ustalonej kategorii OLX "
            "(platform.olx_category_id) - nie można opublikować oferty. "
            "Ustal kategorię przez GET /api/olx/categories/search i "
            "uzupełnij ją w słowniku platform."
        )
    # Platforma "other" nie ma generation/olx_attribute_value w słowniku -
    # platform_other (opis wpisany ręcznie przy zatwierdzaniu) jest wtedy
    # jedynym sensownym opisem konsoli.
    platform_generation = generation or current.platform_other or platform_name
    console_name = current.platform_other or platform_name
    return (
        manufacturer,
        platform_generation,
        console_name,
        olx_attribute_value,
        olx_category_id,
    )


def _build_advert_payload_for_item(
    current: IntakeItemView,
    *,
    manufacturer: str,
    platform_generation: str,
    console_name: str,
    olx_attribute_value: str | None,
    olx_category_id: int,
) -> dict[str, Any]:
    """Buduje payload OLX (tytuł/opis/payload), bez żadnej publikacji.

    Przyjmuje już zresolwowane dane platformy (`_resolve_platform_for_publish`)
    zamiast pobierać je samodzielnie - `publish_item` resolwuje je PRZED
    `olx.get_access_token` (patrz tamten docstring), więc ponowne pobieranie
    tutaj byłoby zbędnym zapytaniem.

    Współdzielone przez `publish_item` (rzeczywista publikacja) i
    `preview_publish_item` (podgląd bez create_advert) - obie ścieżki MUSZA
    używać dokładnie tych samych funkcji (`olx.build_title`,
    `olx.build_description`, `olx.build_advert_payload`), inaczej podgląd
    przestałby być wiarygodna diagnostyka tego, co faktycznie poszłoby do
    OLX. NIE wywołuje `olx.resolve_delivery_attribute` - "ad_delivery" jest
    polem widocznym w odczycie ogłoszenia, ale odrzucanym przy tworzeniu
    (patrz `olx.build_advert_payload`), więc nie ma sensu go tu ustalać.

    Synchroniczna i bezstanowa (zero zapytań do bazy/OLX) - w odróżnieniu od
    poprzedniej wersji, która wywoływała resolve_delivery_attribute.

    Args:
        current: Widok pozycji z już zweryfikowanym title/price_pln/condition.
        manufacturer: Producent platformy (`_resolve_platform_for_publish`).
        platform_generation: Etykieta generacji do tytułu.
        console_name: Nazwa konsoli do opisu.
        olx_attribute_value: Wartość atrybutu platformy, albo None.
        olx_category_id: Id kategorii OLX.

    Returns:
        Payload gotowy do wysłania w treści POST /adverts.

    Raises:
        olx.OlxValidationError: Gdy tytuł albo liczba zdjęć przekracza limit
            OLX.
    """
    settings = get_settings()
    title = olx.build_title(current.title, platform_generation)
    description = olx.build_description(
        manufacturer=manufacturer,
        console_name=console_name,
        game_title=current.title,
        condition=current.condition,
    )
    return olx.build_advert_payload(
        title=title,
        description=description,
        category_id=olx_category_id,
        city_id=settings.olx_city_id,
        district_id=settings.olx_district_id,
        price_pln=current.price_pln,
        condition=current.condition,
        platform_olx_attribute_value=olx_attribute_value,
        image_urls=current.photo_urls,
        contact_name=settings.olx_contact_name,
    )


_OLX_STATUS_TO_LISTING_STATUS = {
    "active": "active",
    "new": "pending",
    "waiting": "pending",
    "moderated": "pending",
    "removed": "removed",
    "outdated": "removed",
    # `disabled` jest niejednoznaczny: tuż po POST /adverts oznacza "jeszcze
    # nie aktywowane" (OLX aktywuje asynchronicznie, kilka minut), a po czasie
    # - realnie zdjęte. Mapujemy na stan przejściowy, bo błąd w te stronę
    # naprawia reconciler przy kolejnym odpytaniu; błąd w stronę terminalna
    # jest trwały - nikt już takiego listingu nie sprawdzi, a FIFO go nie widzi.
    "disabled": "pending",
}


def _map_olx_status(raw_status: str | None) -> str:
    """Mapuje surowy status OLX na nasz enum `listing_status`.

    Wystawienie ogłoszenia to nie to samo, co bycie widocznym - OLX zwraca
    status "new"/"waiting"/"moderated" przed moderacją i "active" dopiero po
    niej (zweryfikowane empirycznie: to samo ogłoszenie miało "disabled"
    zaraz po utworzeniu, a "active" kilka minut później). FIFO przy
    sprzedaży stacjonarnej (listing_fifo_idx) szuka WYŁĄCZNIE status='active',
    więc pomyłkowe zostawienie 'pending' dla już aktywnego ogłoszenia
    oznaczałoby, że FIFO nigdy go nie znajdzie.

    Args:
        raw_status: Surowy status z odpowiedzi OLX (`create_advert`/
            `olx.fetch_advert`), albo None.

    Returns:
        Jedna z wartości `listing_status`: "active", "pending" albo
        "removed". Nieznany/brakujący status mapuje na "pending" (bezpieczny
        domyślny stan - ani fałszywie aktywny w FIFO, ani przedwcześnie
        zdjęty) i jest logowany jako ostrzeżenie, żeby dodać go do mapowania
        zamiast cicho tracić informacje.
    """
    mapped = _OLX_STATUS_TO_LISTING_STATUS.get(raw_status) if raw_status else None
    if mapped is not None:
        return mapped
    logger.warning(
        "Nieznany status OLX %r - mapuje na 'pending'. Dodaj go do "
        "_OLX_STATUS_TO_LISTING_STATUS w zibicom.intake.",
        raw_status,
    )
    return "pending"


async def publish_item(session: AsyncSession, item_id: int) -> IntakeItemView:
    """Publikuje zatwierdzoną pozycję na OLX i promuje ją do tabel produkcyjnych.

    Wykonywane w JEDNEJ transakcji: znalezienie/utworzenie `game`, utworzenie
    `listing` i `listing_photo`, wywołanie OLX (`olx.create_advert`), a na
    koniec ustawienie `intake_item.status='published'` razem z `listing_id`.
    Cokolwiek zawiedzie po drodze wycofuje CALOSC - nie może zostać
    ogłoszenie na OLX bez odpowiadajacego mu rekordu w bazie.

    `listing.status` po publikacji NIE jest już na sztywno 'pending' -
    mapuje się z surowego statusu OLX (`_map_olx_status`), bo OLX może
    zwrócić w odpowiedzi na create_advert ogłoszenie już aktywne (bez
    moderacji dla zaufanych kont) - FIFO przy sprzedaży stacjonarnej
    (listing_fifo_idx) szuka WYŁĄCZNIE status='active', więc pozostawienie
    'pending' dla już aktywnego ogłoszenia oznaczałoby, że FIFO nigdy go nie
    znajdzie. Status MOŻE też zmienić się PO tej funkcji, bez naszego
    udziału (moderacją z opóźnieniem, wygaśnięcie, zdjęcie przez OLX) - do
    tego służy `sync_advert_status`.

    Token OLX jest zdobywany PRZED jakimkolwiek zapisem do bazy w tej
    funkcji - `olx.get_access_token` może przy okazji zacommitować odświeżony
    token (rotacja refresh tokenu), a zrobione później przedwcześnie
    zatwierdziłoby częściowy stan tej transakcji (patrz docstring
    `olx.get_access_token` i `olx.create_advert`).

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji poczekalni.

    Returns:
        Opublikowany widok pozycji (status='published', wypełnione listing_id).

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
        IntakeValidationError: Gdy pozycja nie ma statusu 'approved', nie ma
            przypisanej platformy, albo platforma nie ma ustalonej kategorii
            OLX (platform.olx_category_id - przypadek "other").
        olx.OlxError: Gdy publikacja na OLX się nie powiedzie (brak
            autoryzacji, naruszenie limitu OLX, błąd API) - transakcja jest
            wtedy w całości wycofywana.
    """
    current = await _get_item_view(session, item_id)
    if current.status != "approved":
        raise IntakeValidationError(
            f"Pozycja ma status '{current.status}' - publikować można tylko "
            "pozycje ze statusem 'approved'."
        )
    if current.platform_id is None:
        raise IntakeValidationError("Nie można opublikować pozycji bez platformy.")
    # approve_item już wymusił obecność tytułu i ceny; condition jest
    # wymagane do zbudowania atrybutu stanu w payloadzie OLX.
    if current.title is None or current.price_pln is None or current.condition is None:
        raise IntakeValidationError(
            "Nie można opublikować pozycji bez tytułu, ceny albo stanu."
        )
    (
        manufacturer,
        platform_generation,
        console_name,
        olx_attribute_value,
        olx_category_id,
    ) = await _resolve_platform_for_publish(session, current)

    try:
        access_token = await olx.get_access_token(session)

        game_id = await _find_or_create_game(
            session, current.title, current.platform_id
        )

        listing_id = (
            await session.execute(
                text(
                    "INSERT INTO listing (game_id, condition, price_pln, status) "
                    "VALUES (:game_id, CAST(:condition AS listing_condition), "
                    ":price_pln, 'pending') RETURNING id"
                ),
                {
                    "game_id": game_id,
                    "condition": current.condition,
                    "price_pln": current.price_pln,
                },
            )
        ).scalar_one()

        for position, url in enumerate(current.photo_urls, start=1):
            await session.execute(
                text(
                    "INSERT INTO listing_photo "
                    "(listing_id, position, public_url, is_primary) "
                    "VALUES (:listing_id, :position, :url, :is_primary)"
                ),
                {
                    "listing_id": listing_id,
                    "position": position,
                    "url": url,
                    "is_primary": position == 1,
                },
            )

        payload = _build_advert_payload_for_item(
            current,
            manufacturer=manufacturer,
            platform_generation=platform_generation,
            console_name=console_name,
            olx_attribute_value=olx_attribute_value,
            olx_category_id=olx_category_id,
        )

        advert = await olx.create_advert(
            session, payload, access_token=access_token, listing_id=listing_id
        )
        raw_status = advert.get("status")
        mapped_status = _map_olx_status(raw_status)

        await session.execute(
            text(
                "UPDATE listing SET olx_advert_id = :advert_id, "
                "status = CAST(:status AS listing_status), "
                "olx_status = :olx_status, "
                "olx_payload = CAST(:payload AS jsonb), "
                "posted_at = CASE WHEN :status = 'active' "
                "THEN now() ELSE posted_at END "
                "WHERE id = :listing_id"
            ),
            {
                "advert_id": advert.get("id"),
                "status": mapped_status,
                "olx_status": raw_status,
                "payload": json.dumps(payload),
                "listing_id": listing_id,
            },
        )
        await session.execute(
            text(
                "UPDATE intake_item SET status = 'published', listing_id = :listing_id "
                "WHERE id = :item_id"
            ),
            {"listing_id": listing_id, "item_id": item_id},
        )
    except Exception:
        await session.rollback()
        raise

    await session.commit()
    return await _get_item_view(session, item_id)


async def approve_and_publish(session: AsyncSession, item_id: int) -> IntakeItemView:
    """Zatwierdza i publikuje pozycje poczekalni w jednej akcji operatora.

    Łączy `approve_item` i `publish_item` (wywołane sekwencyjnie, każda ze
    swoim commitem - patrz ich docstringi) w jeden klik na karcie: przegląd
    pozycji i tak odbywa się wzrokowo przed kliknięciem "Publikuj", więc
    osobny krok "Zatwierdź" byl zbędny. Pośrednie przejście statusu
    pending -> approved -> published zostaje mimo to zapisane w bazie
    (ścieżka audytu) - to dwie transakcje, nie jedna atomowa.

    `approve_item` wymaga statusu 'pending' (patrz jej docstring), więc
    pozycje już zatwierdzone wcześniej (istnieją z pracy sprzed merge'a
    "Zatwierdź"+"Publikuj" w jeden przycisk - Publikuj jest dla nich widoczne
    na karcie i w `publish_batch`) omijają ten krok i idą wprost do
    `publish_item`, zamiast rzucać błąd o złym statusie.

    Zero duplikacji logiki: `approve_item`/`publish_item` zostają nietknięte
    i nadal są używane bezpośrednio przez JSON API (POST .../approve,
    POST .../publish) oraz `publish_batch`.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.

    Returns:
        Opublikowany widok pozycji (status='published', wypełnione listing_id).

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
        IntakeValidationError: Gdy pozycja jest już powiązana z listingiem
            (guard idempotencji niżej - zastępuje usuwane z UI potwierdzenie
            w przeglądarce jako realna ochrona przed dwuklikiem), albo gdy
            walidacja `approve_item`/`publish_item` się nie powiedzie (brak
            tytułu/ceny/platformy/kategorii OLX itd).
        olx.OlxError: Gdy publikacja na OLX się nie powiedzie.
    """
    current = await _get_item_view(session, item_id)
    if current.listing_id is not None:
        olx_advert_id = (
            await session.execute(
                text("SELECT olx_advert_id FROM listing WHERE id = :listing_id"),
                {"listing_id": current.listing_id},
            )
        ).scalar_one_or_none()
        detail = f", olx_advert_id={olx_advert_id}" if olx_advert_id else ""
        raise IntakeValidationError(
            f"Pozycja jest już powiązana z listingiem {current.listing_id}"
            f"{detail} - nie można opublikować ponownie."
        )

    if current.status == "pending":
        await approve_item(session, item_id)
    return await publish_item(session, item_id)


async def preview_publish_item(session: AsyncSession, item_id: int) -> dict[str, Any]:
    """Buduje podgląd payloadu OLX dla pozycji, BEZ publikacji.

    Używa `_build_advert_payload_for_item` - dokładnie tych samych funkcji
    co `publish_item` (`olx.build_title`, `olx.build_description`,
    `olx.build_advert_payload`) - ale NIE wywołuje `olx.create_advert` i NIE
    zapisuje niczego do `game`/`listing`/`listing_photo`. Do diagnozowania
    błędów walidacji OLX (np. za długi tytuł, zła kategoria) bez zużywania
    próby na prawdziwej publikacji - OLX nie ma środowiska testowego, więc
    każda proba `publish_item` to prawdziwe ogłoszenie.

    W ODRÓŻNIENIU od `publish_item`, dostępne dla pozycji w DOWOLNYM
    statusie (nie tylko 'approved') - żeby dało się zdiagnozować problem
    PRZED zatwierdzeniem.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji poczekalni.

    Returns:
        Payload, który poszedłby do POST /adverts przy prawdziwej publikacji
        (`publish_item`).

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
        IntakeValidationError: Gdy pozycji brakuje platformy, tytułu, ceny
            albo stanu, platforma nie istnieje, albo nie ma ustalonej
            kategorii OLX (platform.olx_category_id - przypadek "other").
        olx.OlxValidationError: Gdy tytuł albo liczba zdjęć przekracza limit
            OLX.
    """
    current = await _get_item_view(session, item_id)
    if current.platform_id is None:
        raise IntakeValidationError("Nie można zbudować podglądu bez platformy.")
    if current.title is None or current.price_pln is None or current.condition is None:
        raise IntakeValidationError(
            "Nie można zbudować podglądu bez tytułu, ceny albo stanu."
        )
    (
        manufacturer,
        platform_generation,
        console_name,
        olx_attribute_value,
        olx_category_id,
    ) = await _resolve_platform_for_publish(session, current)
    return _build_advert_payload_for_item(
        current,
        manufacturer=manufacturer,
        platform_generation=platform_generation,
        console_name=console_name,
        olx_attribute_value=olx_attribute_value,
        olx_category_id=olx_category_id,
    )


class BulkPublishResult(BaseModel):
    """Podsumowanie masowej publikacji zatwierdzonych pozycji jednej partii.

    Attributes:
        published: Liczba pozycji opublikowanych pomyślnie.
        failed: Liczba pozycji, których publikacja się nie powiodła.
        skipped: Liczba pozycji pominiętych - status inny niż
            'pending'/'approved', albo brak tytułu/ceny.
        aborted: Czy przebieg został przerwany przez circuit breaker (3
            błędy pod rząd).
        errors: Pary (item_id, komunikat błędu) dla nieudanych pozycji, w
            kolejności wystąpienia.
    """

    published: int
    failed: int
    skipped: int
    aborted: bool
    errors: list[tuple[int, str]]


_BULK_PUBLISH_DELAY_SECONDS = 0.3
_BULK_PUBLISH_CIRCUIT_BREAKER_THRESHOLD = 3


async def publish_batch(session: AsyncSession, batch_id: int) -> BulkPublishResult:
    """Zatwierdza i publikuje sekwencyjnie wszystkie gotowe pozycje partii na OLX.

    Obejmuje pozycje w statusie 'pending' i 'approved' - odkąd "Zatwierdź"
    przestało być osobnym krokiem operatora (`approve_and_publish`), masowa
    publikacja musi również zatwierdzać pozycje w locie, nie tylko już
    zatwierdzone.

    SEKWENCYJNIE - celowo bez jakiejkolwiek współbieżności (żaden
    asyncio.gather/TaskGroup). OLX rotuje refresh token przy każdym
    odświeżeniu access tokenu (`olx.get_access_token`): dwa równoległe
    zadania trafiające na wygasły token próbowałyby odświeżyć go
    jednocześnie, a pierwsze unieważniłoby token, którego używa drugie - to
    bezpowrotna utrata autoryzacji (naprawa tylko ręcznym OAuth). Między
    kolejnymi próbami publikacji `asyncio.sleep(0.3)` - higiena wobec
    obcego API (limit OLX 4500 zadań/5 min nie jest zagrożony: 150 gier to
    ok. 3% limitu).

    Pozycja bez tytułu/ceny jest POMIJANA (liczona w `skipped`, z wpisem w
    `errors`), a NIE próbowana i liczona jako `failed` - brak tych danych
    jest wada danych tej jednej pozycji (do poprawienia ręcznie na karcie),
    nie objawem problemu systemowego, więc nie powinna zużywać próby ani
    wpływać na circuit breaker.

    Każdy inny błąd pojedynczej pozycji (walidacja platformy/kategorii OLX,
    błąd API OLX) jest łapany, logowany (`logger.exception`) i zapisywany do
    wyniku jako `failed` - NIE przerywa przebiegu, bo jedna wadliwa pozycja
    nie może zablokować całej partii. Trzy takie błędy POD RZAD uruchamiają
    circuit breaker: przebieg jest przerywany (`aborted=True`) zamiast
    próbować pozostałe pozycje, bo seria błędów zwykle oznacza problem
    systemowy (wygasła autoryzacja, zła konfiguracja), a nie wadę
    pojedynczej pozycji - dobijanie reszty tworzyłoby tylko kolejne
    nieudane próby na produkcyjnym API OLX (OLX nie ma środowiska
    testowego).

    Wywołuje `approve_and_publish` per pozycja - zero duplikacji logiki
    zatwierdzania, publikacji, promocji do `game`/`listing`/`listing_photo`
    i mapowania statusu OLX.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.

    Returns:
        Podsumowanie przebiegu.

    Raises:
        IntakeNotFoundError: Gdy partia nie istnieje.
    """
    if not await _batch_exists(session, batch_id):
        raise IntakeNotFoundError(f"Partia o id {batch_id} nie istnieje.")

    rows = (
        await session.execute(
            text(
                "SELECT id, status::TEXT, title, price_pln FROM intake_item "
                "WHERE batch_id = :batch_id ORDER BY position"
            ),
            {"batch_id": batch_id},
        )
    ).all()

    published = failed = skipped = 0
    consecutive_failures = 0
    attempted = 0
    aborted = False
    errors: list[tuple[int, str]] = []

    for item_id, item_status, title, price_pln in rows:
        if item_status not in ("pending", "approved"):
            skipped += 1
            continue
        if not title or price_pln is None:
            skipped += 1
            errors.append((item_id, "Pominięto: brak tytułu lub ceny."))
            continue

        if attempted > 0:
            await asyncio.sleep(_BULK_PUBLISH_DELAY_SECONDS)
        attempted += 1

        try:
            await approve_and_publish(session, item_id)
        except Exception as exc:
            logger.exception(
                "Publikacja pozycji %s w partii %s (masowa publikacja) "
                "nie powiodła się.",
                item_id,
                batch_id,
            )
            failed += 1
            consecutive_failures += 1
            errors.append((item_id, str(exc)))
            if consecutive_failures >= _BULK_PUBLISH_CIRCUIT_BREAKER_THRESHOLD:
                aborted = True
                break
        else:
            published += 1
            consecutive_failures = 0

    return BulkPublishResult(
        published=published,
        failed=failed,
        skipped=skipped,
        aborted=aborted,
        errors=errors,
    )


async def publish_progress(session: AsyncSession, batch_id: int) -> tuple[int, int]:
    """Zwraca postęp masowej publikacji partii do paska postępu na `/ui`.

    Liczony wyłącznie z bazy (bez stanu w pamieci procesu) - pozycje już
    opublikowane względem wszystkich pozycji, które są (albo byly, zanim
    zostaly opublikowane) w zasięgu `publish_batch` (pending/approved) w tej
    partii. `approve_and_publish` zmienia status pozycji na 'published'
    dopiero po udanym zapisie, więc ten iloraz rosnie w miarę przebiegu
    `publish_batch`.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.

    Returns:
        Krotkę (opublikowane, wszystkie): liczba pozycji ze statusem
        'published' i suma pozycji 'published' + 'approved' + 'pending' w
        partii.
    """
    row = (
        await session.execute(
            text(
                "SELECT count(*) FILTER (WHERE status = 'published'), "
                "count(*) FILTER (WHERE status IN "
                "('published', 'approved', 'pending')) "
                "FROM intake_item WHERE batch_id = :batch_id"
            ),
            {"batch_id": batch_id},
        )
    ).one()
    return row[0], row[1]


async def sync_advert_status(
    session: AsyncSession, listing_id: int
) -> ListingStatusView:
    """Odświeża status oferty z OLX (GET /adverts/{id}) i zapisuje go lokalnie.

    Status oferty MOŻE zmienić się po naszej stronie bez naszego udziału -
    moderacją z opóźnieniem, wygaśnięcie po `valid_to`, zdjęcie przez OLX
    (zweryfikowane empirycznie: to samo ogłoszenie miało status "disabled"
    zaraz po utworzeniu, a "active" kilka minut później) - `publish_item`
    zapisuje tylko migawkę z chwili publikacji. Ta funkcja pobiera aktualny
    stan (`olx.fetch_advert`) i aktualizuje `listing.status` (po zmapowaniu
    przez `_map_olx_status`) oraz `listing.olx_status` (surowa wartość).

    `posted_at` jest ustawiane na `now()` TYLKO przy przejściu w 'active' po
    raz pierwszy (bylo NULL) - kolejne synchronizacje już aktywnej oferty
    (albo przejście z 'active' do 'removed' po wygasnieciu) go nie ruszają,
    bo to nadal ten sam, pierwszy moment aktywacji.

    Args:
        session: Sesja bazy danych.
        listing_id: Identyfikator oferty.

    Returns:
        Zaktualizowany stan oferty.

    Raises:
        IntakeNotFoundError: Gdy oferta o podanym id nie istnieje.
        IntakeValidationError: Gdy oferta nigdy nie została opublikowana na
            OLX (brak `olx_advert_id`) - nie ma wtedy czego synchronizować.
        olx.OlxAuthError: Gdy brak ważnej autoryzacji OLX.
        olx.OlxApiError: Gdy wywołanie OLX się nie powiedzie.
    """
    row = (
        await session.execute(
            text("SELECT olx_advert_id FROM listing WHERE id = :listing_id"),
            {"listing_id": listing_id},
        )
    ).first()
    if row is None:
        raise IntakeNotFoundError(f"Oferta o id {listing_id} nie istnieje.")
    advert_id = row[0]
    if advert_id is None:
        raise IntakeValidationError(
            f"Oferta o id {listing_id} nie została jeszcze opublikowana na "
            "OLX (brak olx_advert_id) - nie ma czego synchronizować."
        )

    advert = await olx.fetch_advert(session, advert_id)
    raw_status = advert.get("status")
    mapped_status = _map_olx_status(raw_status)

    await session.execute(
        text(
            "UPDATE listing SET status = CAST(:status AS listing_status), "
            "olx_status = :olx_status, "
            "posted_at = CASE WHEN :status = 'active' AND posted_at IS NULL "
            "THEN now() ELSE posted_at END "
            "WHERE id = :listing_id"
        ),
        {"status": mapped_status, "olx_status": raw_status, "listing_id": listing_id},
    )
    await session.commit()

    updated = (
        await session.execute(
            text(
                "SELECT id, status::TEXT AS status, olx_status, posted_at "
                "FROM listing WHERE id = :listing_id"
            ),
            {"listing_id": listing_id},
        )
    ).first()
    mapping = updated._mapping
    return ListingStatusView(
        id=mapping["id"],
        status=mapping["status"],
        olx_status=mapping["olx_status"],
        posted_at=mapping["posted_at"],
    )


async def list_platforms(session: AsyncSession) -> list[PlatformView]:
    """Zwraca aktywne platformy ze słownika, do listy wyboru w formularzu.

    Args:
        session: Sesja bazy danych.

    Returns:
        Platformy posortowane wg producenta i nazwy.
    """
    rows = (
        await session.execute(
            text(
                "SELECT id, code, name, manufacturer::TEXT AS manufacturer, "
                "generation FROM platform WHERE is_active "
                "ORDER BY manufacturer, name"
            )
        )
    ).all()
    return [PlatformView(**row._mapping) for row in rows]


class SyncPendingResult(BaseModel):
    """Podsumowanie przebiegu reconcilera statusow.

    Attributes:
        checked: Liczba listingów odpytanych w tym przebiegu.
        activated: Liczba listingów, które przeszły w stan `active`.
        still_pending: Liczba listingów nadal czekających na aktywację.
        terminal: Liczba listingów w stanie końcowym (`removed`).
        failed: Liczba listingów, których nie udało się odpytać.
    """

    checked: int
    activated: int
    still_pending: int
    terminal: int
    failed: int


async def sync_pending_listings(
    session: AsyncSession,
    batch_limit: int = 100,
) -> SyncPendingResult:
    """Dosynchronizowuje statusy listingów czekających na aktywację w OLX.

    OLX aktywuje ogłoszenia asynchronicznie - `POST /adverts` zwraca
    `disabled`, a `active` pojawia się dopiero po kilku minutach. Bez tego
    przebiegu listing zostaje w `pending` i jest niewidoczny dla FIFO
    (`listing_fifo_idx` obejmuje wyłącznie `status = 'active'`).

    Błąd pojedynczego listingu nie przerywa przebiegu - pozostałe musza
    zostać odpytane, a nieudany rekord trafi do kolejnego uruchomienia.

    Args:
        session: Sesja bazodanowa.
        batch_limit: Maksymalna liczba listingów odpytanych w jednym
            przebiegu. Chroni przed wysypaniem tysiąca zadań do OLX naraz.

    Returns:
        Podsumowanie liczbowe przebiegu.
    """
    rows = await session.execute(
        text(
            """
            SELECT id
            FROM listing
            WHERE status = 'pending'
              AND olx_advert_id IS NOT NULL
            ORDER BY id
            LIMIT :limit
            """
        ),
        {"limit": batch_limit},
    )
    listing_ids = [row[0] for row in rows]

    activated = still_pending = terminal = failed = 0
    for listing_id in listing_ids:
        try:
            view = await sync_advert_status(session, listing_id)
        except Exception:
            # Log obowiązkowy - cicha obsługa błędu ukryłaby błąd konfiguracji
            # (wygasły token, zmiana API) jako "nic się nie stało".
            logger.exception("Nie udało się zsynchronizować listingu %s.", listing_id)
            failed += 1
            continue

        if view.status == "active":
            activated += 1
        elif view.status == "pending":
            still_pending += 1
        else:
            terminal += 1

    logger.info(
        "Reconciler: sprawdzono %d, aktywowano %d, nadal pending %d, "
        "terminalne %d, błędy %d.",
        len(listing_ids),
        activated,
        still_pending,
        terminal,
        failed,
    )
    return SyncPendingResult(
        checked=len(listing_ids),
        activated=activated,
        still_pending=still_pending,
        terminal=terminal,
        failed=failed,
    )


async def get_item(session: AsyncSession, item_id: int) -> IntakeItemView:
    """Zwraca widok pojedynczej pozycji poczekalni.

    Publiczny odpowiednik `_get_item_view` - warstwa prezentacji potrzebuje
    odczytać stan pozycji po nieudanej operacji, żeby przerenderować kartę
    z komunikatem błędu.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.

    Returns:
        Widok pozycji.

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
    """
    return await _get_item_view(session, item_id)
