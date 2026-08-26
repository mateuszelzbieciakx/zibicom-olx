"""Warstwa poczekalni (staging) dla partii zdjec przetwarzanych przez AI.

Rozpoznanie obrazem jest niepewne (model myli ceny i czasem tytuly), wiec
wyniki NIE trafiaja bezposrednio do tabel produkcyjnych `game`/`listing` -
zyja w `intake_batch`/`intake_item`/`intake_photo`, dopoki czlowiek ich nie
zatwierdzi. Publikacja zatwierdzonych pozycji do OLX to kolejny krok, poza
zakresem tego modulu.
"""

from __future__ import annotations

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
from zibicom.models import PlatformCode

logger = logging.getLogger(__name__)


class IntakeError(Exception):
    """Blad domenowy warstwy intake - komunikat po polsku, gotowy dla klienta."""


class IntakeNotFoundError(IntakeError):
    """Zadany zasob (partia albo pozycja) nie istnieje."""


class IntakeValidationError(IntakeError):
    """Zadanie narusza regule biznesowa poczekalni."""


class PlatformView(BaseModel):
    """Wpis slownika platform do listy wyboru.

    Attributes:
        id: Identyfikator platformy (uzywany jako platform_id w intake_item).
        code: Kod platformy zgodny z PlatformCode.
        name: Nazwa wyswietlana.
        manufacturer: Producent.
        generation: Etykieta generacji sprzetu, jesli dotyczy.
    """

    id: int
    code: str
    name: str
    manufacturer: str
    generation: str | None


class IntakeItemView(BaseModel):
    """Pozycja poczekalni gotowa do wyswietlenia w widoku zatwierdzania.

    Attributes:
        id: Identyfikator pozycji.
        batch_id: Partia, do ktorej nalezy pozycja.
        position: Kolejnosc pozycji w obrebie partii.
        title: Tytul (moze byc None, gdy AI go nie odczytalo).
        platform_id: Identyfikator platformy w slowniku, jesli przypisana.
        platform_code: Kod platformy, jesli przypisana.
        platform_name: Nazwa platformy, jesli przypisana.
        platform_manufacturer: Producent platformy, jesli przypisana.
        platform_other: Opisowa platforma, gdy platform_code == "other".
        price_pln: Cena w PLN (moze byc None).
        condition: Stan egzemplarza (moze byc None).
        ai_warning: Zbiorcze ostrzezenie z grupowania AI.
        status: Status pozycji w cyklu zycia poczekalni.
        listing_id: Identyfikator opublikowanej oferty, jesli juz istnieje.
        photo_urls: Publiczne URL-e zdjec pozycji, w kolejnosci.
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
    """Korekta pol pozycji poczekalni wprowadzana recznie przez czlowieka.

    Wszystkie pola sa opcjonalne - w PATCH przesyla sie tylko to, co sie
    zmienia (`model_fields_set` decyduje, co trafi do UPDATE-u).

    Attributes:
        title: Poprawiony tytul.
        platform_id: Poprawiona platforma (id ze slownika `platform`).
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
        """Odrzuca ujemna cene przed wyslaniem jej do bazy.

        Args:
            value: Poprawiona cena w PLN (moze byc None).

        Returns:
            Cene bez zmian.

        Raises:
            ValueError: Gdy cena jest ujemna.
        """
        if value is not None and value < 0:
            raise ValueError("Cena nie moze byc ujemna.")
        return value


class ListingStatusView(BaseModel):
    """Stan oferty po synchronizacji z OLX (`sync_advert_status`).

    Attributes:
        id: Identyfikator oferty.
        status: Nasz status (`listing_status`) po zmapowaniu z OLX
            (`_map_olx_status`).
        olx_status: Surowy status zwrocony przez OLX, niezaleznie od
            mapowania.
        posted_at: Moment, w ktorym oferta stala sie aktywna (None, jesli
            jeszcze nie byla).
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
        True, jesli partia istnieje.
    """
    result = await session.execute(
        text("SELECT 1 FROM intake_batch WHERE id = :batch_id"),
        {"batch_id": batch_id},
    )
    return result.first() is not None


async def _photo_urls_for_item(session: AsyncSession, item_id: int) -> list[str]:
    """Zwraca publiczne URL-e zdjec przypisanych do pozycji, w kolejnosci.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.

    Returns:
        Lista URL-i zdjec posortowana wg intake_photo.position.
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
    """Sklada wiersz z `_ITEM_VIEW_SELECT` i URL-e zdjec w IntakeItemView.

    Args:
        row: Wiersz (mapping) zwrocony przez zapytanie `_ITEM_VIEW_SELECT`.
        photo_urls: URL-e zdjec pozycji, w kolejnosci.

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
    """Tworzy nowa partie poczekalni i wgrywa jej zdjecia do R2.

    Kolejnosc plikow na wejsciu jest zachowywana jako `intake_photo.position`
    - ta kolejnosc niesie informacje o granicach egzemplarzy, wykorzystywana
    pozniej przez `extract_batch`/`zibicom.grouping.group_photos`.

    Args:
        session: Sesja bazy danych.
        files: Lista (nazwa_pliku, surowe_bajty) w kolejnosci wgrania.

    Returns:
        Identyfikator utworzonej partii.

    Raises:
        IntakeValidationError: Gdy lista plikow jest pusta albo ktorys plik
            nie daje sie zdekodowac jako obraz.
    """
    if not files:
        raise IntakeValidationError("Nalezy przeslac co najmniej jedno zdjecie.")

    batch_result = await session.execute(
        text("INSERT INTO intake_batch DEFAULT VALUES RETURNING id")
    )
    batch_id = batch_result.scalar_one()

    for position, (filename, raw) in enumerate(files, start=1):
        try:
            normalized = photos.normalize_photo(raw)
        except ValueError as exc:
            raise IntakeValidationError(
                f"Plik {filename or f'#{position}'} nie jest poprawnym zdjeciem: {exc}"
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
    """Odnajduje id platformy w slowniku po jej kodzie.

    Args:
        session: Sesja bazy danych.
        code: Kod platformy rozpoznany przez AI.

    Returns:
        Identyfikator platformy, albo None, gdy kodu nie ma w slowniku
        (np. slownik nie zostal jeszcze zaladowany dla nowej platformy).
    """
    result = await session.execute(
        text("SELECT id FROM platform WHERE code = :code"),
        {"code": code.value},
    )
    row = result.first()
    return row[0] if row is not None else None


async def extract_batch(session: AsyncSession, batch_id: int) -> int:
    """Puszcza zdjecia partii przez rozpoznanie AI i grupuje je w pozycje.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.

    Returns:
        Liczba utworzonych pozycji (intake_item).

    Raises:
        IntakeNotFoundError: Gdy partia nie istnieje.
        IntakeError: Gdy pobranie ktoregos zdjecia z R2 sie nie powiedzie.
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
                "SELECT id, public_url FROM intake_photo "
                "WHERE batch_id = :batch_id ORDER BY position"
            ),
            {"batch_id": batch_id},
        )
    ).all()

    try:
        extractions = []
        for photo_id, public_url in photo_rows:
            raw = photos.download_photo(public_url)
            extraction = vision.recognize_photo(raw)
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
            extractions.append(extraction)

        groups = grouping.group_photos(extractions)

        photo_ids = iter(row[0] for row in photo_rows)
        created = 0
        for position, group in enumerate(groups, start=1):
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

            group_photo_ids = [next(photo_ids) for _ in group.photos]
            await session.execute(
                text(
                    "UPDATE intake_photo SET item_id = :item_id "
                    "WHERE id = ANY(:photo_ids)"
                ),
                {"item_id": item_id, "photo_ids": group_photo_ids},
            )
            created += 1

        await session.execute(
            text("UPDATE intake_batch SET status = 'review' WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
    except Exception as exc:
        logger.exception("Rozpoznawanie partii %s nie powiodlo sie.", batch_id)
        await session.rollback()
        await session.execute(
            text("UPDATE intake_batch SET status = 'failed' WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
        await session.commit()
        raise IntakeError(
            f"Rozpoznawanie partii {batch_id} nie powiodlo sie: {exc}"
        ) from exc

    await session.commit()
    return created


async def list_items(session: AsyncSession, batch_id: int) -> list[IntakeItemView]:
    """Zwraca pozycje partii przygotowane do zatwierdzenia przez czlowieka.

    Args:
        session: Sesja bazy danych.
        batch_id: Identyfikator partii.

    Returns:
        Pozycje partii w kolejnosci `intake_item.position`, kazda ze
        skladowymi nazwy platformy i lista URL-i wlasnych zdjec.

    Raises:
        IntakeNotFoundError: Gdy partia nie istnieje.
    """
    if not await _batch_exists(session, batch_id):
        raise IntakeNotFoundError(f"Partia o id {batch_id} nie istnieje.")

    rows = (
        await session.execute(
            text(
                f"{_ITEM_VIEW_SELECT} "
                "WHERE ii.batch_id = :batch_id ORDER BY ii.position"
            ),
            {"batch_id": batch_id},
        )
    ).all()

    items = []
    for row in rows:
        photo_urls = await _photo_urls_for_item(session, row._mapping["id"])
        items.append(_row_to_item_view(row, photo_urls))
    return items


async def update_item(
    session: AsyncSession, item_id: int, payload: IntakeItemUpdate
) -> IntakeItemView:
    """Zapisuje reczna korekte pol pozycji poczekalni.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.
        payload: Pola do zmiany (tylko jawnie ustawione trafiaja do UPDATE-u).

    Returns:
        Zaktualizowany widok pozycji.

    Raises:
        IntakeNotFoundError: Gdy pozycja albo wskazana platforma nie istnieje.
        IntakeValidationError: Gdy pozycja jest juz opublikowana, albo brak
            pol do zmiany.
    """
    current = await _get_item_view(session, item_id)
    if current.status == "published":
        raise IntakeValidationError("Nie mozna edytowac juz opublikowanej pozycji.")

    fields = payload.model_dump(exclude_unset=True)
    if not fields:
        raise IntakeValidationError("Brak pol do zmiany.")

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
    """Zatwierdza pozycje poczekalni po walidacji kompletnosci danych.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji.

    Returns:
        Zatwierdzony widok pozycji.

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
        IntakeValidationError: Gdy pozycja nie ma statusu "pending", albo
            brakuje jej tytulu lub ceny.
    """
    current = await _get_item_view(session, item_id)
    if current.status != "pending":
        raise IntakeValidationError(
            f"Pozycja ma status '{current.status}' - zatwierdzic mozna tylko "
            "pozycje ze statusem 'pending'."
        )

    problems = []
    if not current.title:
        problems.append("brak tytulu")
    if current.price_pln is None:
        problems.append("brak ceny")
    if problems:
        raise IntakeValidationError(
            "Nie mozna zatwierdzic pozycji: " + ", ".join(problems) + "."
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
            f"Pozycja ma status '{current.status}' - odrzucic mozna tylko "
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
    """Znajduje istniejaca `game` po (lower(title), platform_id) albo ja tworzy.

    Brak wyscigu miedzy SELECT-em a INSERT-em (klasyczny problem UPSERT-u
    bez unikalnego indeksu) nie jest tu problemem: publikacja jest
    wywolywana recznie, pojedynczo, dla JEDNEJ pozycji na raz
    (POST /api/intake/items/{id}/publish) - nie ma wspolbieznych zadan o ta
    sama pare (tytul, platforma), ktore wymagalyby ON CONFLICT.

    Args:
        session: Sesja bazy danych.
        title: Tytul gry (dopasowanie case-insensitive).
        platform_id: Id platformy.

    Returns:
        Id istniejacej albo nowo utworzonej `game`.
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

    Czysto lokalna walidacja (SELECT + sprawdzenie kategorii) - ZERO wywolan
    OLX. Celowo osobna funkcja, wywolywana w `publish_item` PRZED
    `olx.get_access_token`: brak kategorii dla platformy (przypadek "other")
    ma failowac natychmiast, bez wymagania wczesniej waznej autoryzacji OLX
    - to tani, lokalny blad, nie powod do angazowania OLX.

    Args:
        session: Sesja bazy danych.
        current: Widok pozycji z juz zweryfikowanym platform_id.

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
    # ma ustalonej kategorii (rozne, nieprzewidywalne rodzaje przedmiotow),
    # wiec bez tej walidacji create_advert wyslalby ogloszenie z
    # category_id=NULL/nieprawidlowym zamiast czytelnego bledu PRZED
    # jakimkolwiek wywolaniem OLX.
    if olx_category_id is None:
        raise IntakeValidationError(
            f"Platforma '{platform_name}' nie ma ustalonej kategorii OLX "
            "(platform.olx_category_id) - nie mozna opublikowac oferty. "
            "Ustal kategorie przez GET /api/olx/categories/search i "
            "uzupelnij ja w slowniku platform."
        )
    # Platforma "other" nie ma generation/olx_attribute_value w slowniku -
    # platform_other (opis wpisany recznie przy zatwierdzaniu) jest wtedy
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
    """Buduje payload OLX (tytul/opis/payload), bez zadnej publikacji.

    Przyjmuje juz zresolwowane dane platformy (`_resolve_platform_for_publish`)
    zamiast pobierac je samodzielnie - `publish_item` resolwuje je PRZED
    `olx.get_access_token` (patrz tamten docstring), wiec ponowne pobieranie
    tutaj byloby zbednym zapytaniem.

    Wspoldzielone przez `publish_item` (rzeczywista publikacja) i
    `preview_publish_item` (podglad bez create_advert) - obie sciezki MUSZA
    uzywac dokladnie tych samych funkcji (`olx.build_title`,
    `olx.build_description`, `olx.build_advert_payload`), inaczej podglad
    przestalby byc wiarygodna diagnostyka tego, co faktycznie poszloby do
    OLX. NIE wywoluje `olx.resolve_delivery_attribute` - "ad_delivery" jest
    polem widocznym w odczycie ogloszenia, ale odrzucanym przy tworzeniu
    (patrz `olx.build_advert_payload`), wiec nie ma sensu go tu ustalac.

    Synchroniczna i bezstanowa (zero zapytan do bazy/OLX) - w odroznieniu od
    poprzedniej wersji, ktora wywolywala resolve_delivery_attribute.

    Args:
        current: Widok pozycji z juz zweryfikowanym title/price_pln/condition.
        manufacturer: Producent platformy (`_resolve_platform_for_publish`).
        platform_generation: Etykieta generacji do tytulu.
        console_name: Nazwa konsoli do opisu.
        olx_attribute_value: Wartosc atrybutu platformy, albo None.
        olx_category_id: Id kategorii OLX.

    Returns:
        Payload gotowy do wyslania w tresci POST /adverts.

    Raises:
        olx.OlxValidationError: Gdy tytul albo liczba zdjec przekracza limit
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
    "disabled": "removed",
}


def _map_olx_status(raw_status: str | None) -> str:
    """Mapuje surowy status OLX na nasz enum `listing_status`.

    Wystawienie ogloszenia to nie to samo, co bycie widocznym - OLX zwraca
    status "new"/"waiting"/"moderated" przed moderacja i "active" dopiero po
    niej (zweryfikowane empirycznie: to samo ogloszenie mialo "disabled"
    zaraz po utworzeniu, a "active" kilka minut pozniej). FIFO przy
    sprzedazy stacjonarnej (listing_fifo_idx) szuka WYLACZNIE status='active',
    wiec pomylkowe zostawienie 'pending' dla juz aktywnego ogloszenia
    oznaczaloby, ze FIFO nigdy go nie znajdzie.

    Args:
        raw_status: Surowy status z odpowiedzi OLX (`create_advert`/
            `olx.fetch_advert`), albo None.

    Returns:
        Jedna z wartosci `listing_status`: "active", "pending" albo
        "removed". Nieznany/brakujacy status mapuje na "pending" (bezpieczny
        domyslny stan - ani falszywie aktywny w FIFO, ani przedwczesnie
        zdjety) i jest logowany jako ostrzezenie, zeby dodac go do mapowania
        zamiast cicho tracic informacje.
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
    """Publikuje zatwierdzona pozycje na OLX i promuje ja do tabel produkcyjnych.

    Wykonywane w JEDNEJ transakcji: znalezienie/utworzenie `game`, utworzenie
    `listing` i `listing_photo`, wywolanie OLX (`olx.create_advert`), a na
    koniec ustawienie `intake_item.status='published'` razem z `listing_id`.
    Cokolwiek zawiedzie po drodze wycofuje CALOSC - nie moze zostac
    ogloszenie na OLX bez odpowiadajacego mu rekordu w bazie.

    `listing.status` po publikacji NIE jest juz na sztywno 'pending' -
    mapuje sie z surowego statusu OLX (`_map_olx_status`), bo OLX moze
    zwrocic w odpowiedzi na create_advert ogloszenie juz aktywne (bez
    moderacji dla zaufanych kont) - FIFO przy sprzedazy stacjonarnej
    (listing_fifo_idx) szuka WYLACZNIE status='active', wiec pozostawienie
    'pending' dla juz aktywnego ogloszenia oznaczaloby, ze FIFO nigdy go nie
    znajdzie. Status MOZE tez zmienic sie PO tej funkcji, bez naszego
    udzialu (moderacja z opoznieniem, wygasniecie, zdjecie przez OLX) - do
    tego sluzy `sync_advert_status`.

    Token OLX jest zdobywany PRZED jakimkolwiek zapisem do bazy w tej
    funkcji - `olx.get_access_token` moze przy okazji zacommitowac odswiezony
    token (rotacja refresh tokenu), a zrobione pozniej przedwczesnie
    zatwierdziloby czesciowy stan tej transakcji (patrz docstring
    `olx.get_access_token` i `olx.create_advert`).

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji poczekalni.

    Returns:
        Opublikowany widok pozycji (status='published', wypelnione listing_id).

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
        IntakeValidationError: Gdy pozycja nie ma statusu 'approved', nie ma
            przypisanej platformy, albo platforma nie ma ustalonej kategorii
            OLX (platform.olx_category_id - przypadek "other").
        olx.OlxError: Gdy publikacja na OLX sie nie powiedzie (brak
            autoryzacji, naruszenie limitu OLX, blad API) - transakcja jest
            wtedy w calosci wycofywana.
    """
    current = await _get_item_view(session, item_id)
    if current.status != "approved":
        raise IntakeValidationError(
            f"Pozycja ma status '{current.status}' - publikowac mozna tylko "
            "pozycje ze statusem 'approved'."
        )
    if current.platform_id is None:
        raise IntakeValidationError("Nie mozna opublikowac pozycji bez platformy.")
    # approve_item juz wymusil obecnosc tytulu i ceny; condition jest
    # wymagane do zbudowania atrybutu stanu w payloadzie OLX.
    if current.title is None or current.price_pln is None or current.condition is None:
        raise IntakeValidationError(
            "Nie mozna opublikowac pozycji bez tytulu, ceny albo stanu."
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


async def preview_publish_item(session: AsyncSession, item_id: int) -> dict[str, Any]:
    """Buduje podglad payloadu OLX dla pozycji, BEZ publikacji.

    Uzywa `_build_advert_payload_for_item` - dokladnie tych samych funkcji
    co `publish_item` (`olx.build_title`, `olx.build_description`,
    `olx.build_advert_payload`) - ale NIE wywoluje `olx.create_advert` i NIE
    zapisuje niczego do `game`/`listing`/`listing_photo`. Do diagnozowania
    bledow walidacji OLX (np. za dlugi tytul, zla kategoria) bez zuzywania
    proby na prawdziwej publikacji - OLX nie ma srodowiska testowego, wiec
    kazda proba `publish_item` to prawdziwe ogloszenie.

    W ODROZNIENIU od `publish_item`, dostepne dla pozycji w DOWOLNYM
    statusie (nie tylko 'approved') - zeby dalo sie zdiagnozowac problem
    PRZED zatwierdzeniem.

    Args:
        session: Sesja bazy danych.
        item_id: Identyfikator pozycji poczekalni.

    Returns:
        Payload, ktory poszedlby do POST /adverts przy prawdziwej publikacji
        (`publish_item`).

    Raises:
        IntakeNotFoundError: Gdy pozycja nie istnieje.
        IntakeValidationError: Gdy pozycji brakuje platformy, tytulu, ceny
            albo stanu, platforma nie istnieje, albo nie ma ustalonej
            kategorii OLX (platform.olx_category_id - przypadek "other").
        olx.OlxValidationError: Gdy tytul albo liczba zdjec przekracza limit
            OLX.
    """
    current = await _get_item_view(session, item_id)
    if current.platform_id is None:
        raise IntakeValidationError("Nie mozna zbudowac podgladu bez platformy.")
    if current.title is None or current.price_pln is None or current.condition is None:
        raise IntakeValidationError(
            "Nie mozna zbudowac podgladu bez tytulu, ceny albo stanu."
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


async def sync_advert_status(
    session: AsyncSession, listing_id: int
) -> ListingStatusView:
    """Odswieza status oferty z OLX (GET /adverts/{id}) i zapisuje go lokalnie.

    Status oferty MOZE zmienic sie po naszej stronie bez naszego udzialu -
    moderacja z opoznieniem, wygasniecie po `valid_to`, zdjecie przez OLX
    (zweryfikowane empirycznie: to samo ogloszenie mialo status "disabled"
    zaraz po utworzeniu, a "active" kilka minut pozniej) - `publish_item`
    zapisuje tylko migawke z chwili publikacji. Ta funkcja pobiera aktualny
    stan (`olx.fetch_advert`) i aktualizuje `listing.status` (po zmapowaniu
    przez `_map_olx_status`) oraz `listing.olx_status` (surowa wartosc).

    `posted_at` jest ustawiane na `now()` TYLKO przy przejsciu w 'active' po
    raz pierwszy (bylo NULL) - kolejne synchronizacje juz aktywnej oferty
    (albo przejscie z 'active' do 'removed' po wygasnieciu) go nie ruszaja,
    bo to nadal ten sam, pierwszy moment aktywacji.

    Args:
        session: Sesja bazy danych.
        listing_id: Identyfikator oferty.

    Returns:
        Zaktualizowany stan oferty.

    Raises:
        IntakeNotFoundError: Gdy oferta o podanym id nie istnieje.
        IntakeValidationError: Gdy oferta nigdy nie zostala opublikowana na
            OLX (brak `olx_advert_id`) - nie ma wtedy czego synchronizowac.
        olx.OlxAuthError: Gdy brak waznej autoryzacji OLX.
        olx.OlxApiError: Gdy wywolanie OLX sie nie powiedzie.
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
            f"Oferta o id {listing_id} nie zostala jeszcze opublikowana na "
            "OLX (brak olx_advert_id) - nie ma czego synchronizowac."
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
    """Zwraca aktywne platformy ze slownika, do listy wyboru w formularzu.

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
