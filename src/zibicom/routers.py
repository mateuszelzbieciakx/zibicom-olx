"""Endpointy HTTP warstwy poczekalni (intake).

Upload zdjec, rozpoznanie AI, przeglad i zatwierdzanie pozycji przed
publikacja na OLX.
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
    """Odpowiedz po utworzeniu partii.

    Attributes:
        batch_id: Identyfikator utworzonej partii.
        photo_count: Liczba wgranych zdjec.
    """

    batch_id: int
    photo_count: int


class ExtractResponse(BaseModel):
    """Odpowiedz po rozpoznaniu i zgrupowaniu zdjec partii.

    Attributes:
        batch_id: Identyfikator partii.
        item_count: Liczba utworzonych pozycji.
    """

    batch_id: int
    item_count: int


class OlxAuthorizeResponse(BaseModel):
    """Odpowiedz z URL-em logowania OAuth do otwarcia w przegladarce.

    Attributes:
        url: Pelny URL logowania OLX.
    """

    url: str


class OlxCategoryView(BaseModel):
    """Zwiezly ksztalt kategorii OLX zwracany przez /api/olx/categories*.

    Attributes:
        id: Id kategorii OLX.
        name: Nazwa kategorii.
        parent_id: Id kategorii-rodzica (0 dla kategorii glownych).
        is_leaf: Czy w tej kategorii mozna wystawic ogloszenie (brak dzieci).
        photos_limit: Maksymalna liczba zdjec dozwolona w tej kategorii.
    """

    id: int
    name: str
    parent_id: int
    is_leaf: bool
    photos_limit: int | None


class OlxCategoryAttributeValueView(BaseModel):
    """Jedna z dozwolonych wartosci atrybutu wyboru.

    Attributes:
        code: Wartosc do wyslania w payloadzie ogloszenia (np. "xbox360").
        label: Czytelna etykieta (np. "Xbox 360").
    """

    code: str | None
    label: str | None


class OlxCategoryAttributeView(BaseModel):
    """Zwiezly ksztalt atrybutu kategorii OLX.

    Attributes:
        code: Kod atrybutu (klucz w payloadzie ogloszenia -
            zibicom.olx.build_advert_payload).
        label: Czytelna nazwa atrybutu.
        required: Czy OLX wymaga podania tego atrybutu przy publikacji.
        values: Dozwolone wartosci (atrybuty wyboru) - pusta lista dla
            wolnego tekstu/liczby.
    """

    code: str | None
    label: str | None
    required: bool
    values: list[OlxCategoryAttributeValueView]


class OlxCityView(BaseModel):
    """Zwiezly ksztalt miasta OLX zwracany przez /api/olx/cities.

    Attributes:
        id: Id miasta OLX.
        name: Nazwa miasta.
        county: Powiat.
        region_id: Id wojewodztwa.
    """

    id: int
    name: str
    county: str | None
    region_id: int | None


class OlxDistrictView(BaseModel):
    """Zwiezly ksztalt dzielnicy OLX (GET /api/olx/cities/{city_id}/districts).

    Attributes:
        id: Id dzielnicy OLX.
        name: Nazwa dzielnicy.
    """

    id: int
    name: str


class OlxExchangeRequest(BaseModel):
    """Kod autoryzacyjny przepisany recznie z paska adresu po przekierowaniu OLX.

    Attributes:
        code: Wartosc parametru `code` z adresu, na ktory przekierowal OLX.
    """

    code: str


def _http_exception_for(exc: intake.IntakeError | olx.OlxError) -> HTTPException:
    """Mapuje wyjatek domenowy (poczekalni albo integracji OLX) na HTTPException.

    Args:
        exc: Wyjatek zglosszony przez `zibicom.intake` albo `zibicom.olx`.

    Returns:
        HTTPException 404 dla braku zasobu, 400 dla naruszenia reguly
        biznesowej (w tym braku autoryzacji OLX albo bledu OLX API) - do
        rzucenia przez wywolujacego (`raise ... from exc`).
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
        list[UploadFile], File(description="Zdjecia w kolejnosci wgrania.")
    ],
) -> BatchCreateResponse:
    """Tworzy nowa partie poczekalni i wgrywa jej zdjecia do R2."""
    payload = [(f.filename, await f.read()) for f in files]
    try:
        batch_id = await intake.create_batch(session, payload)
    except intake.IntakeError as exc:
        raise _http_exception_for(exc) from exc
    return BatchCreateResponse(batch_id=batch_id, photo_count=len(payload))


@router.post(
    "/api/intake/batches/{batch_id}/extract",
    response_model=ExtractResponse,
    tags=["intake"],
)
async def extract_batch(batch_id: int, session: SessionDep) -> ExtractResponse:
    """Rozpoznaje zdjecia partii przez AI i grupuje je w pozycje."""
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
    """Zapisuje reczna korekte pol pozycji poczekalni."""
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
    """Zatwierdza pozycje poczekalni po walidacji kompletnosci danych."""
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
    """Odrzuca pozycje poczekalni."""
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
    """Publikuje zatwierdzona pozycje na OLX i promuje ja do tabel produkcyjnych."""
    try:
        return await intake.publish_item(session, item_id)
    except (intake.IntakeError, olx.OlxError) as exc:
        raise _http_exception_for(exc) from exc


@router.get(
    "/api/intake/items/{item_id}/publish/preview",
    response_model=dict[str, Any],
    tags=["intake"],
)
async def preview_publish_item(item_id: int, session: SessionDep) -> dict[str, Any]:
    """Buduje podglad payloadu OLX dla pozycji, BEZ publikacji.

    Dokladnie ten sam payload, ktory poszedlby do OLX przy POST
    .../publish (te same funkcje: build_title, build_description,
    build_advert_payload), ale bez wywolania create_advert - do
    diagnozowania bledow walidacji OLX bez zuzywania proby na prawdziwej
    publikacji. Dostepne dla pozycji w dowolnym statusie (nie tylko
    'approved'), zeby dalo sie zdiagnozowac problem przed zatwierdzeniem.
    """
    try:
        return await intake.preview_publish_item(session, item_id)
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
    """Odswieza status oferty z OLX (GET /adverts/{id}) i zapisuje go lokalnie.

    Status oferty moze zmienic sie po naszej stronie bez naszego udzialu
    (moderacja, wygasniecie, zdjecie przez OLX) - ten endpoint pobiera
    aktualny stan i mapuje go na nasz listing_status, m.in. zeby FIFO przy
    sprzedazy stacjonarnej (listing_fifo_idx, WHERE status='active')
    faktycznie widzialo aktywne oferty.
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
    """Zwraca slownik platform do listy wyboru."""
    return await intake.list_platforms(session)


@router.get(
    "/api/olx/authorize",
    response_model=OlxAuthorizeResponse,
    tags=["olx"],
)
async def olx_authorize() -> OlxAuthorizeResponse:
    """Zwraca URL logowania OAuth OLX do recznego otwarcia w przegladarce."""
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
    """Zwraca stan autoryzacji OLX (bez wywolywania API OLX)."""
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

    Bez `parent_id` zwraca kategorie glowne. Ogloszenie mozna wystawic
    tylko w kategorii z `is_leaf=true` - do znalezienia takiej bez recznego
    klikania po drzewie sluzy GET /api/olx/categories/search.
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
    """Rekurencyjnie przeszukuje cale drzewo kategorii OLX pod katem lisci.

    Zwraca tylko kategorie z `is_leaf=true` (jedyne, w ktorych mozna
    wystawic ogloszenie), ktorych nazwa zawiera `q`.
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

    Do ustalenia, jak przekazac w payloadzie ogloszenia cechy, ktorych OLX
    nie wyraza przez osobna kategorie (np. konkretna konsola w obrebie
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

    Wyszukiwanie ignoruje wielkosc liter i znaki diakrytyczne.
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

    Puste dla miast bez podzialu na dzielnice (wiekszosc malych
    miejscowosci) - to prawidlowy wynik, nie blad.
    """
    try:
        return await olx.fetch_districts(session, city_id)
    except olx.OlxError as exc:
        raise _http_exception_for(exc) from exc
