"""Endpointy HTTP warstwy poczekalni (intake).

Upload zdjęć, rozpoznanie AI, przegląd i zatwierdzanie pozycji przed
publikacją na OLX.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from zibicom import intake, olx
from zibicom.db import get_session

router = APIRouter()

SessionDep = Annotated[AsyncSession, Depends(get_session)]


class BatchCreateResponse(BaseModel):
    """Odpowiedź po utworzeniu partii.

    Attributes:
        batch_id: Identyfikator utworzonej partii.
        photo_count: Liczba wgranych zdjęć.
    """

    batch_id: int
    photo_count: int


class ExtractResponse(BaseModel):
    """Odpowiedź po rozpoznaniu i zgrupowaniu zdjęć partii.

    Attributes:
        batch_id: Identyfikator partii.
        item_count: Liczba utworzonych pozycji.
    """

    batch_id: int
    item_count: int


class OlxAuthorizeResponse(BaseModel):
    """Odpowiedź z URL-em logowania OAuth do otwarcia w przeglądarce.

    Attributes:
        url: Pełny URL logowania OLX.
    """

    url: str


class OlxCategoryView(BaseModel):
    """Zwięzły kształt kategorii OLX zwracany przez /api/olx/categories*.

    Attributes:
        id: Id kategorii OLX.
        name: Nazwa kategorii.
        parent_id: Id kategorii-rodzica (0 dla kategorii głównych).
        is_leaf: Czy w tej kategorii można wystawić ogłoszenie (brak dzieci).
        photos_limit: Maksymalna liczba zdjęć dozwolona w tej kategorii.
    """

    id: int
    name: str
    parent_id: int
    is_leaf: bool
    photos_limit: int | None


class OlxCategoryAttributeValueView(BaseModel):
    """Jedna z dozwolonych wartości atrybutu wyboru.

    Attributes:
        code: Wartość do wysłania w payloadzie ogłoszenia (np. "xbox360").
        label: Czytelna etykieta (np. "Xbox 360").
    """

    code: str | None
    label: str | None


class OlxCategoryAttributeView(BaseModel):
    """Zwięzły kształt atrybutu kategorii OLX.

    Attributes:
        code: Kod atrybutu (klucz w payloadzie ogłoszenia -
            zibicom.olx.build_advert_payload).
        label: Czytelna nazwa atrybutu.
        required: Czy OLX wymaga podania tego atrybutu przy publikacji.
        values: Dozwolone wartości (atrybuty wyboru) - pusta lista dla
            wolnego tekstu/liczby.
    """

    code: str | None
    label: str | None
    required: bool
    values: list[OlxCategoryAttributeValueView]


class OlxCityView(BaseModel):
    """Zwięzły kształt miasta OLX zwracany przez /api/olx/cities.

    Attributes:
        id: Id miasta OLX.
        name: Nazwa miasta.
        county: Powiat.
        region_id: Id województwa.
    """

    id: int
    name: str
    county: str | None
    region_id: int | None


class OlxDistrictView(BaseModel):
    """Zwięzły kształt dzielnicy OLX (GET /api/olx/cities/{city_id}/districts).

    Attributes:
        id: Id dzielnicy OLX.
        name: Nazwa dzielnicy.
    """

    id: int
    name: str


class OlxExchangeRequest(BaseModel):
    """Kod autoryzacyjny przepisany ręcznie z paska adresu po przekierowaniu OLX.

    Attributes:
        code: Wartość parametru `code` z adresu, na który przekierował OLX.
    """

    code: str


def _http_exception_for(exc: intake.IntakeError | olx.OlxError) -> HTTPException:
    """Mapuje wyjątek domenowy (poczekalni albo integracji OLX) na HTTPException.

    Args:
        exc: Wyjątek zgłoszony przez `zibicom.intake` albo `zibicom.olx`.

    Returns:
        HTTPException 404 dla braku zasobu, 400 dla naruszenia reguły
        biznesowej (w tym braku autoryzacji OLX albo błędu OLX API) - do
        rzucenia przez wywołującego (`raise ... from exc`).
    """
    if isinstance(exc, intake.IntakeNotFoundError):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post(
    "/api/intake/batches",
    response_model=BatchCreateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["intake"],
)
async def create_batch(
    session: SessionDep,
    files: Annotated[
        list[UploadFile], File(description="Zdjęcia w kolejności wgrania.")
    ],
) -> BatchCreateResponse:
    """Tworzy nową partię poczekalni i wgrywa jej zdjęcia do R2."""
    payload = [(f.filename, await f.read()) for f in files]
    try:
        batch_id = await intake.create_batch(session, payload)
    except intake.IntakeError as exc:
        raise _http_exception_for(exc) from exc
    return BatchCreateResponse(batch_id=batch_id, photo_count=len(payload))


@router.get(
    "/api/intake/batches",
    response_model=list[intake.BatchView],
    tags=["intake"],
)
async def list_batches(session: SessionDep) -> list[intake.BatchView]:
    """Zwraca wszystkie partie poczekalni, od najnowszej."""
    return await intake.list_batches(session)


@router.post(
    "/api/intake/batches/{batch_id}/extract",
    response_model=ExtractResponse,
    tags=["intake"],
)
async def extract_batch(batch_id: int, session: SessionDep) -> ExtractResponse:
    """Rozpoznaje zdjęcia partii przez AI i grupuje je w pozycje."""
    try:
        item_count = await intake.extract_batch(session, batch_id)
    except intake.IntakeError as exc:
        raise _http_exception_for(exc) from exc
    return ExtractResponse(batch_id=batch_id, item_count=item_count)


@router.get(
    "/api/intake/batches/{batch_id}/items",
    response_model=list[intake.IntakeItemView],
    tags=["intake"],
)
async def list_items(batch_id: int, session: SessionDep) -> list[intake.IntakeItemView]:
    """Zwraca pozycje partii przygotowane do zatwierdzenia."""
    try:
        return await intake.list_items(session, batch_id)
    except intake.IntakeError as exc:
        raise _http_exception_for(exc) from exc


@router.patch(
    "/api/intake/items/{item_id}",
    response_model=intake.IntakeItemView,
    tags=["intake"],
)
async def update_item(
    item_id: int, payload: intake.IntakeItemUpdate, session: SessionDep
) -> intake.IntakeItemView:
    """Zapisuje ręczną korektę pól pozycji poczekalni."""
    try:
        return await intake.update_item(session, item_id, payload)
    except intake.IntakeError as exc:
        raise _http_exception_for(exc) from exc


@router.post(
    "/api/intake/items/{item_id}/approve",
    response_model=intake.IntakeItemView,
    tags=["intake"],
)
async def approve_item(item_id: int, session: SessionDep) -> intake.IntakeItemView:
    """Zatwierdza pozycję poczekalni po walidacji kompletności danych."""
    try:
        return await intake.approve_item(session, item_id)
    except intake.IntakeError as exc:
        raise _http_exception_for(exc) from exc


@router.post(
    "/api/intake/items/{item_id}/reject",
    response_model=intake.IntakeItemView,
    tags=["intake"],
)
async def reject_item(item_id: int, session: SessionDep) -> intake.IntakeItemView:
    """Odrzuca pozycję poczekalni."""
    try:
        return await intake.reject_item(session, item_id)
    except intake.IntakeError as exc:
        raise _http_exception_for(exc) from exc


@router.post(
    "/api/intake/items/{item_id}/publish",
    response_model=intake.IntakeItemView,
    tags=["intake"],
)
async def publish_item(item_id: int, session: SessionDep) -> intake.IntakeItemView:
    """Publikuje zatwierdzoną pozycję na OLX i promuje ją do tabel produkcyjnych."""
    try:
        return await intake.publish_item(session, item_id)
    except (intake.IntakeError, olx.OlxError) as exc:
        raise _http_exception_for(exc) from exc


@router.post(
    "/api/intake/items/{item_id}/approve-and-publish",
    response_model=intake.IntakeItemView,
    tags=["intake"],
)
async def approve_and_publish_item(
    item_id: int, session: SessionDep
) -> intake.IntakeItemView:
    """Zatwierdza i publikuje pozycję poczekalni w jednym kroku."""
    try:
        return await intake.approve_and_publish(session, item_id)
    except (intake.IntakeError, olx.OlxError) as exc:
        raise _http_exception_for(exc) from exc


@router.get(
    "/api/intake/items/{item_id}/publish/preview",
    response_model=dict[str, Any],
    tags=["intake"],
)
async def preview_publish_item(item_id: int, session: SessionDep) -> dict[str, Any]:
    """Buduje podgląd payloadu OLX dla pozycji, BEZ publikacji.

    Dokładnie ten sam payload, który poszedłby do OLX przy POST
    .../publish (te same funkcje: build_title, build_description,
    build_advert_payload), ale bez wywołania create_advert - do
    diagnozowania błędów walidacji OLX bez zużywania próby na prawdziwej
    publikacji. Dostępne dla pozycji w dowolnym statusie (nie tylko
    'approved'), żeby dało się zdiagnozować problem przed zatwierdzeniem.
    """
    try:
        return await intake.preview_publish_item(session, item_id)
    except (intake.IntakeError, olx.OlxError) as exc:
        raise _http_exception_for(exc) from exc


@router.post(
    "/api/intake/batches/{batch_id}/publish-all",
    response_model=intake.BulkPublishResult,
    tags=["intake"],
)
async def publish_batch(batch_id: int, session: SessionDep) -> intake.BulkPublishResult:
    """Zatwierdza i publikuje sekwencyjnie wszystkie gotowe pozycje partii na OLX.

    Obejmuje pozycje w statusie 'pending' i 'approved'. Sekwencyjnie i z
    pauzą między próbami (`intake.publish_batch`) - patrz tamten docstring
    o rotacji refresh tokenu OLX. Błąd pojedynczej pozycji nie przerywa
    przebiegu; seria 3 błędów pod rząd uruchamia circuit breaker
    (`aborted=True` w odpowiedzi).
    """
    try:
        return await intake.publish_batch(session, batch_id)
    except intake.IntakeError as exc:
        raise _http_exception_for(exc) from exc


@router.post(
    "/api/listings/sync-pending",
    response_model=intake.SyncPendingResult,
    tags=["listings"],
)
async def sync_pending_listings(
    session: SessionDep, batch_limit: int = 100
) -> intake.SyncPendingResult:
    """Dosynchronizowuje oferty czekające na aktywację w OLX.

    OLX aktywuje ogłoszenia asynchronicznie - `POST /adverts` zwraca
    `disabled`, a `active` pojawia się dopiero kilka minut później. Bez tego
    przebiegu oferta zostaje w `pending` i jest niewidoczna dla FIFO
    (listing_fifo_idx obejmuje wyłącznie status='active').

    Ścieżka statyczna MUSI być zadeklarowana przed
    /api/listings/{listing_id}/sync-status - FastAPI dopasowuje trasy w
    kolejności rejestracji i inaczej potraktowałby "sync-pending" jako
    wartość listing_id.
    """
    try:
        return await intake.sync_pending_listings(
            session, batch_limit=batch_limit
        )
    except (intake.IntakeError, olx.OlxError) as exc:
        raise _http_exception_for(exc) from exc


@router.post(
    "/api/listings/{listing_id}/sync-status",
    response_model=intake.ListingStatusView,
    tags=["listings"],
)
async def sync_advert_status(
    listing_id: int, session: SessionDep
) -> intake.ListingStatusView:
    """Odświeża status oferty z OLX (GET /adverts/{id}) i zapisuje go lokalnie.

    Status oferty może zmienić się po naszej stronie bez naszego udziału
    (moderacja, wygaśnięcie, zdjęcie przez OLX) - ten endpoint pobiera
    aktualny stan i mapuje go na nasz listing_status, m.in. żeby FIFO przy
    sprzedaży stacjonarnej (listing_fifo_idx, WHERE status='active')
    faktycznie widziało aktywne oferty.
    """
    try:
        return await intake.sync_advert_status(session, listing_id)
    except (intake.IntakeError, olx.OlxError) as exc:
        raise _http_exception_for(exc) from exc


@router.get(
    "/api/platforms",
    response_model=list[intake.PlatformView],
    tags=["platforms"],
)
async def list_platforms(session: SessionDep) -> list[intake.PlatformView]:
    """Zwraca słownik platform do listy wyboru."""
    return await intake.list_platforms(session)


@router.get(
    "/api/olx/authorize",
    response_model=OlxAuthorizeResponse,
    tags=["olx"],
)
async def olx_authorize() -> OlxAuthorizeResponse:
    """Zwraca URL logowania OAuth OLX do ręcznego otwarcia w przeglądarce."""
    return OlxAuthorizeResponse(url=olx.build_authorize_url())


@router.post(
    "/api/olx/exchange",
    response_model=olx.OlxStatus,
    tags=["olx"],
)
async def olx_exchange(
    payload: OlxExchangeRequest, session: SessionDep
) -> olx.OlxStatus:
    """Wymienia kod autoryzacyjny (przepisany z paska adresu) na tokeny."""
    try:
        await olx.exchange_code(session, payload.code)
    except olx.OlxError as exc:
        raise _http_exception_for(exc) from exc
    return await olx.get_status(session)


@router.get(
    "/api/olx/status",
    response_model=olx.OlxStatus,
    tags=["olx"],
)
async def olx_status(session: SessionDep) -> olx.OlxStatus:
    """Zwraca stan autoryzacji OLX (bez wywoływania API OLX)."""
    return await olx.get_status(session)


@router.get(
    "/api/olx/categories",
    response_model=list[OlxCategoryView],
    tags=["olx"],
)
async def olx_categories(
    session: SessionDep, parent_id: int | None = None, q: str | None = None
) -> list[dict[str, Any]]:
    """Zwraca kategorie OLX na jednym poziomie drzewa (dzieci `parent_id`).

    Bez `parent_id` zwraca kategorie główne. Ogłoszenie można wystawić
    tylko w kategorii z `is_leaf=true` - do znalezienia takiej bez ręcznego
    klikania po drzewie służy GET /api/olx/categories/search.
    """
    try:
        return await olx.fetch_categories(session, parent_id=parent_id, q=q)
    except olx.OlxError as exc:
        raise _http_exception_for(exc) from exc


@router.get(
    "/api/olx/categories/search",
    response_model=list[OlxCategoryView],
    tags=["olx"],
)
async def olx_categories_search(session: SessionDep, q: str) -> list[dict[str, Any]]:
    """Rekurencyjnie przeszukuje całe drzewo kategorii OLX pod kątem liści.

    Zwraca tylko kategorie z `is_leaf=true` (jedyne, w których można
    wystawić ogłoszenie), których nazwa zawiera `q`.
    """
    try:
        return await olx.search_leaf_categories(session, q)
    except olx.OlxError as exc:
        raise _http_exception_for(exc) from exc


@router.get(
    "/api/olx/categories/{category_id}/attributes",
    response_model=list[OlxCategoryAttributeView],
    tags=["olx"],
)
async def olx_category_attributes(
    category_id: int, session: SessionDep
) -> list[dict[str, Any]]:
    """Zwraca wymagane i opcjonalne atrybuty danej kategorii OLX.

    Do ustalenia, jak przekazać w payloadzie ogłoszenia cechy, których OLX
    nie wyraża przez osobną kategorię (np. konkretna konsola w obrębie
    kategorii producenta - patrz migracja 0005_olx_category_mapping.sql).
    """
    try:
        return await olx.fetch_category_attributes(session, category_id)
    except olx.OlxError as exc:
        raise _http_exception_for(exc) from exc


@router.get(
    "/api/olx/cities",
    response_model=list[OlxCityView],
    tags=["olx"],
)
async def olx_cities(session: SessionDep, q: str | None = None) -> list[dict[str, Any]]:
    """Wyszukuje miasta OLX po nazwie - do ustalenia city_id.

    Wyszukiwanie ignoruje wielkość liter i znaki diakrytyczne.
    """
    try:
        return await olx.fetch_cities(session, q)
    except olx.OlxError as exc:
        raise _http_exception_for(exc) from exc


@router.get(
    "/api/olx/cities/{city_id}/districts",
    response_model=list[OlxDistrictView],
    tags=["olx"],
)
async def olx_city_districts(city_id: int, session: SessionDep) -> list[dict[str, Any]]:
    """Zwraca dzielnice danego miasta OLX - do ustalenia district_id.

    Puste dla miast bez podziału na dzielnice (większość małych
    miejscowości) - to prawidłowy wynik, nie błąd.
    """
    try:
        return await olx.fetch_districts(session, city_id)
    except olx.OlxError as exc:
        raise _http_exception_for(exc) from exc
