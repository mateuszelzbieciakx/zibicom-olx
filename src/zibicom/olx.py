"""Integracja z OLX Partner API: autoryzacja, publikacja ogloszen.

OLX NIE ma srodowiska testowego - kazda publikacja to prawdziwe ogloszenie
na koncie firmowym. Autoryzacja jest POLRECZNA: aplikacja nie ma dzialajacego
publicznego callbacku (redirect_uri wskazuje na R2, ktory nie obsluguje
zadan), wiec czlowiek otwiera URL logowania, loguje sie, a po przekierowaniu
przepisuje parametr `code` z paska adresu i wkleja go do POST /api/olx/exchange.

Refresh token ROTUJE przy KAZDYM odswiezeniu access tokenu (zywotnosc ok.
miesiaca) - nowa wartosc jest zapisywana natychmiast po otrzymaniu, inaczej
kolejne odswiezenie nie ma juz czym sie uwierzytelnic i autoryzacja jest
bezpowrotnie utracona.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel
from sqlalchemy import text

from zibicom import crypto
from zibicom.config import get_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

_AUTHORIZE_PATH = "/oauth/authorize/"
_TOKEN_PATH = "/api/open/oauth/token"
_ADVERTS_PATH = "/adverts"
_CATEGORIES_PATH = "/categories"
_CITIES_PATH = "/cities"

DEFAULT_SCOPE = "v2 read write"
REFRESH_MARGIN_SECONDS = 60
MAX_TITLE_LENGTH = 70
MAX_IMAGES = 8

# Kody atrybutow OLX (per kategoria) - zweryfikowane empirycznie dla
# kategorii Xbox (2273, GET /api/olx/categories/2273/attributes, patrz
# `fetch_category_attributes`): "state" jest WYMAGANY i przyjmuje dokladnie
# nasze wartosci enuma listing_condition ("new"/"used"), wiec mapuje sie 1:1
# bez slownika. "type" jest opcjonalny - dozwolone kody sa per kategoria
# (czyli per producent, patrz migracje 0005/0006), stad
# platform.olx_attribute_value zamiast stalej wartosci tutaj. Sony (2272) i
# Nintendo (2274) NIE byly jeszcze zweryfikowane - jesli maja inny kod niz
# "type" dla tego samego atrybutu, trzeba to sprawdzic przed pierwsza
# prawdziwa publikacja dla tych producentow. Bledny kod konczy sie 4xx z OLX
# (zalogowanym w olx_operation przez create_advert) - NIE tworzy ogloszenia,
# wiec pomylka tutaj jest bezpieczna do naprawienia.
CONDITION_ATTRIBUTE_CODE = "state"
PLATFORM_ATTRIBUTE_CODE = "type"

_CONDITION_PL = {"new": "nowa", "used": "używana"}
_MANUFACTURER_PL = {"sony": "Sony", "microsoft": "Microsoft", "nintendo": "Nintendo"}

# Klucze NIGDY nie trafiajace w postaci jawnej do olx_operation - zapisanie
# tam tokenu zniweczyloby sens jego szyfrowania w olx_token (patrz
# migrations/0004_olx_token.sql).
_SENSITIVE_KEYS = {"client_secret", "refresh_token", "access_token", "code"}


class OlxError(Exception):
    """Blad domenowy integracji OLX - komunikat po polsku, gotowy dla klienta."""


class OlxAuthError(OlxError):
    """Brak waznej autoryzacji OLX (kod nigdy nie zostal wymieniony)."""


class OlxApiError(OlxError):
    """Wywolanie OLX API sie nie powiodlo (blad sieci albo odpowiedz 4xx/5xx)."""


class OlxValidationError(OlxError):
    """Payload ogloszenia narusza ograniczenie OLX (np. za dlugi tytul)."""


class OlxStatus(BaseModel):
    """Stan autoryzacji OLX do wyswietlenia w panelu.

    Attributes:
        authorized: Czy w bazie jest zapisany refresh token.
        access_token_valid: Czy biezacy access token jest jeszcze wazny, bez
            probowania go odswiezyc.
        access_expires_at: Moment wygasniecia biezacego access tokenu.
        scope: Zakres uprawnien z ostatniej autoryzacji/odswiezenia.
    """

    authorized: bool
    access_token_valid: bool
    access_expires_at: datetime | None
    scope: str | None


# Domyslny User-Agent httpx ("python-httpx/...") jest przez CloudFront/WAF
# OLX traktowany jako ruch botowy i blokowany 403-ka z pustym cialem - bez
# wzgledu na poprawnosc autoryzacji. Potwierdzone eksperymentalnie: to samo
# zadanie z wlasnym User-Agentem dostaje 200. Naglowki musza wiec byc
# ustawione na kliencie (dla WSZYSTKICH wywolan OLX), nie doklejane per-request.
_DEFAULT_HEADERS = {
    "User-Agent": "zibicom-olx/0.1",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Partner API (kategorie/miasta/adverts) odrzuca zadania bez naglowka
# Version 400-ka "Missing required 'Version' header!". Endpoint OAuth
# (/api/open/oauth/token) NIE jest czescia Partner API i dziala poprawnie
# BEZ tego naglowka - stad osobny klient zamiast dopisania Version do
# _DEFAULT_HEADERS, zeby nie ryzykowac zepsucia dzialajacego juz OAuth.
_PARTNER_HEADERS = {**_DEFAULT_HEADERS, "Version": "2.0"}


@lru_cache
def _http_client() -> httpx.AsyncClient:
    """Buduje (raz na proces) klienta HTTP do endpointu OAuth OLX.

    Tworzenie nowego klienta przy kazdym wywolaniu prowadzi do tego samego
    bledu, co przy kliencie Gemini (zibicom.vision._client) - GC zamyka
    porzucony transport i kolejne wywolania w tym samym procesie dostaja
    "Cannot send a request, as the client has been closed". `lru_cache` bez
    argumentow trzyma jedna, wspoldzielona instancje przez caly czas zycia
    procesu.

    Uzywany WYLACZNIE przez `_token_request` (/api/open/oauth/token) - do
    Partner API sluzy `_partner_http_client` (inne wymagane naglowki).

    Returns:
        Asynchroniczny klient httpx z rozsadnym timeoutem i naglowkami
        (`_DEFAULT_HEADERS`) omijajacymi blokade WAF/CloudFront OLX.
    """
    return httpx.AsyncClient(timeout=30.0, headers=_DEFAULT_HEADERS)


@lru_cache
def _partner_http_client() -> httpx.AsyncClient:
    """Buduje (raz na proces) klienta HTTP do Partner API OLX.

    Jak `_http_client` (jeden wspoldzielony klient na caly proces, zeby
    uniknac "client has been closed"), ale z dodatkowym naglowkiem Version
    wymaganym WYLACZNIE przez Partner API (kategorie/miasta/adverts) - patrz
    komentarz przy `_PARTNER_HEADERS`.

    Returns:
        Asynchroniczny klient httpx z rozsadnym timeoutem i naglowkami
        `_PARTNER_HEADERS`.
    """
    return httpx.AsyncClient(timeout=30.0, headers=_PARTNER_HEADERS)


async def dispose_http_client() -> None:
    """Zamyka wspoldzielone klienty HTTP (wywolywane przy zamykaniu aplikacji)."""
    if _http_client.cache_info().currsize:
        await _http_client().aclose()
    _http_client.cache_clear()
    if _partner_http_client.cache_info().currsize:
        await _partner_http_client().aclose()
    _partner_http_client.cache_clear()


def _error_detail(response: httpx.Response) -> str:
    """Wydobywa czytelny szczegol bledu z odpowiedzi OLX (do logu i wyjatku).

    Uzywa `response.text` (nie sparsowanego JSON-a) - odpowiedzi blokady
    WAF/CloudFront (patrz `_DEFAULT_HEADERS`) czesto nie sa poprawnym JSON-em
    albo sa calkowicie puste, wiec poleganie na `response.json()` gubilo
    tresc bledu i zostawialo tylko "None" w logu, co uniemozliwialo
    diagnoze.

    Args:
        response: Surowa odpowiedz httpx zakonczona bledem.

    Returns:
        Tekst odpowiedzi przyciety do 500 znakow, albo jawny opis pustej
        odpowiedzi, gdy `response.text` jest puste/samymi bialymi znakami.
    """
    body_text = response.text
    if not body_text.strip():
        return "pusta odpowiedz — prawdopodobnie blokada WAF/CloudFront"
    return body_text[:500]


def _redact(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Maskuje wrazliwe pola przed zapisaniem wywolania w olx_operation.

    Args:
        payload: Surowy slownik zadania/odpowiedzi, albo None.

    Returns:
        Kopia slownika z wartosciami kluczy z `_SENSITIVE_KEYS` zastapionymi
        `"***"`, albo None, gdy wejscie bylo None.
    """
    if payload is None:
        return None
    return {k: ("***" if k in _SENSITIVE_KEYS else v) for k, v in payload.items()}


async def _log_operation(
    session: AsyncSession,
    *,
    listing_id: int | None,
    operation: str,
    request_payload: dict[str, Any] | None,
    response_payload: dict[str, Any] | None,
    http_status: int | None,
    succeeded: bool,
    olx_error: str | None,
) -> None:
    """Zapisuje wpis audytowy wywolania OLX API w `olx_operation`.

    Funkcja NIE commituje - o momencie zatwierdzenia decyduje wywolujacy
    (patrz `create_advert`, ktore jest czescia wiekszej transakcji publikacji).

    Args:
        session: Sesja bazy danych.
        listing_id: Id oferty, ktorej dotyczy wywolanie, albo None dla
            wywolan niezwiazanych z oferta (np. odswiezenie tokenu).
        operation: Krotka nazwa operacji (np. "create_advert", "refresh_token").
        request_payload: Wyslany payload (juz zredagowany przez `_redact`).
        response_payload: Odpowiedz OLX (juz zredagowana przez `_redact`).
        http_status: Kod HTTP odpowiedzi, albo None przy bledzie sieciowym.
        succeeded: Czy wywolanie zakonczylo sie sukcesem.
        olx_error: Czytelny opis bledu, gdy `succeeded` jest False.
    """
    await session.execute(
        text(
            "INSERT INTO olx_operation "
            "(listing_id, operation, request_payload, response_payload, "
            " http_status, succeeded, olx_error) "
            "VALUES (:listing_id, :operation, CAST(:request_payload AS jsonb), "
            " CAST(:response_payload AS jsonb), :http_status, :succeeded, :olx_error)"
        ),
        {
            "listing_id": listing_id,
            "operation": operation,
            "request_payload": (
                json.dumps(request_payload) if request_payload is not None else None
            ),
            "response_payload": (
                json.dumps(response_payload) if response_payload is not None else None
            ),
            "http_status": http_status,
            "succeeded": succeeded,
            "olx_error": olx_error,
        },
    )


async def _token_request(
    session: AsyncSession, *, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Wysyla zadanie do endpointu tokenu OLX i loguje wywolanie w olx_operation.

    Wspolna implementacja dla `exchange_code` (grant_type=authorization_code)
    i odswiezania w `get_access_token` (grant_type=refresh_token) - obie
    sciezki maja identyczna logike wywolania, logowania bledow i parsowania.

    Args:
        session: Sesja bazy danych.
        operation: Nazwa operacji do zapisu w olx_operation ("oauth_exchange"
            albo "refresh_token").
        payload: Cialo zadania POST (zawiera client_secret/refresh_token/code
            - redagowane przed zapisem do olx_operation).

    Returns:
        Sparsowana odpowiedz JSON OLX (zawiera access_token, refresh_token,
        expires_in, scope).

    Raises:
        OlxApiError: Gdy wywolanie sieciowe sie nie powiodlo albo OLX
            zwrocilo blad / odpowiedz niezgodna z oczekiwanym ksztaltem.
    """
    settings = get_settings()
    client = _http_client()
    request_log = _redact(payload)

    try:
        response = await client.post(
            f"{settings.olx_auth_base_url}{_TOKEN_PATH}", json=payload
        )
    except httpx.HTTPError as exc:
        await _log_operation(
            session,
            listing_id=None,
            operation=operation,
            request_payload=request_log,
            response_payload=None,
            http_status=None,
            succeeded=False,
            olx_error=str(exc),
        )
        raise OlxApiError(
            f"Wywolanie OLX ({operation}) nie powiodlo sie: {exc}"
        ) from exc

    try:
        body = response.json()
    except ValueError:
        body = None

    succeeded = response.status_code < 400 and isinstance(body, dict)
    error_detail = None if succeeded else _error_detail(response)
    await _log_operation(
        session,
        listing_id=None,
        operation=operation,
        request_payload=request_log,
        response_payload=_redact(body) if isinstance(body, dict) else None,
        http_status=response.status_code,
        succeeded=succeeded,
        olx_error=error_detail,
    )

    if not succeeded:
        raise OlxApiError(
            f"OLX zwrocilo blad {response.status_code} przy {operation}: {error_detail}"
        )
    return body


async def _save_tokens(session: AsyncSession, body: dict[str, Any]) -> None:
    """Zapisuje pare tokenow z odpowiedzi OLX, zaszyfrowanych Fernetem.

    UPSERT na singletonie `olx_token` (id=1) - `exchange_code` tworzy
    pierwszy wiersz, kazde kolejne odswiezenie go nadpisuje. Funkcja NIE
    commituje - wywolujacy robi to natychmiast po powrocie (patrz hard fact
    o rotacji refresh tokenu).

    Args:
        session: Sesja bazy danych.
        body: Odpowiedz OLX zawierajaca access_token, refresh_token,
            expires_in i opcjonalnie scope.
    """
    expires_at = datetime.now(UTC) + timedelta(seconds=int(body["expires_in"]))
    await session.execute(
        text(
            "INSERT INTO olx_token "
            "(id, access_token_encrypted, refresh_token_encrypted, "
            " access_expires_at, scope) "
            "VALUES (1, :access_token, :refresh_token, :expires_at, :scope) "
            "ON CONFLICT (id) DO UPDATE SET "
            "access_token_encrypted = EXCLUDED.access_token_encrypted, "
            "refresh_token_encrypted = EXCLUDED.refresh_token_encrypted, "
            "access_expires_at = EXCLUDED.access_expires_at, "
            "scope = EXCLUDED.scope"
        ),
        {
            "access_token": crypto.encrypt(body["access_token"]),
            "refresh_token": crypto.encrypt(body["refresh_token"]),
            "expires_at": expires_at,
            "scope": body.get("scope"),
        },
    )


def build_authorize_url() -> str:
    """Buduje URL logowania OAuth OLX do otwarcia recznie w przegladarce.

    Autoryzacja jest polreczna (patrz docstring modulu) - ta funkcja tylko
    sklada URL, nie wykonuje zadnego wywolania sieciowego.

    Returns:
        Pelny URL `GET .../oauth/authorize/` ze scope "v2 read write".
    """
    settings = get_settings()
    params = {
        "client_id": settings.olx_client_id.get_secret_value(),
        "response_type": "code",
        "redirect_uri": settings.olx_redirect_uri,
        "scope": DEFAULT_SCOPE,
    }
    return f"{settings.olx_auth_base_url}{_AUTHORIZE_PATH}?{urlencode(params)}"


async def exchange_code(session: AsyncSession, code: str) -> None:
    """Wymienia kod autoryzacyjny (z paska adresu) na pierwsza pare tokenow.

    Args:
        session: Sesja bazy danych.
        code: Kod `code` przepisany recznie z paska adresu po przekierowaniu
            OLX (wazny tylko przez chwile - trzeba go wymienic od razu).

    Raises:
        OlxApiError: Gdy wymiana sie nie powiedzie (np. kod wygasl).
    """
    settings = get_settings()
    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.olx_client_id.get_secret_value(),
        "client_secret": settings.olx_client_secret.get_secret_value(),
        "code": code,
        "redirect_uri": settings.olx_redirect_uri,
    }
    body = await _token_request(session, operation="oauth_exchange", payload=payload)
    await _save_tokens(session, body)
    await session.commit()


async def get_access_token(session: AsyncSession) -> str:
    """Zwraca wazny access token, odswiezajac go z 60-sekundowym marginesem.

    WAZNE dla wywolujacych w wiekszej transakcji biznesowej (patrz
    `zibicom.intake.publish_item`): gdy token wymaga odswiezenia, ta funkcja
    zapisuje NOWA pare tokenow i NATYCHMIAST commituje - refresh token
    rotuje przy kazdym odswiezeniu, wiec zwloka w zapisie oznacza trwala
    utrate autoryzacji. Wywolujacy MUSI wiec wywolac te funkcje PRZED
    rozpoczeciem wlasnych, jeszcze niezacommitowanych zapisow w tej samej
    sesji - inaczej ten commit przedwczesnie zatwierdzi tez ich czesciowy
    stan.

    Args:
        session: Sesja bazy danych.

    Returns:
        Wazny access token (jawny str, gotowy do naglowka Authorization).

    Raises:
        OlxAuthError: Gdy autoryzacja nigdy nie zostala wykonana (brak
            wiersza w `olx_token`).
        OlxApiError: Gdy odswiezenie tokenu sie nie powiedzie.
    """
    row = (
        await session.execute(
            text(
                "SELECT access_token_encrypted, refresh_token_encrypted, "
                "access_expires_at FROM olx_token WHERE id = 1"
            )
        )
    ).first()
    if row is None:
        raise OlxAuthError(
            "Brak autoryzacji OLX - wykonaj GET /api/olx/authorize, zaloguj sie "
            "w przegladarce pod zwroconym URL-em, a nastepnie przeslij kod "
            "z paska adresu przez POST /api/olx/exchange."
        )

    access_encrypted, refresh_encrypted, expires_at = row
    now = datetime.now(UTC)
    margin = timedelta(seconds=REFRESH_MARGIN_SECONDS)
    if (
        access_encrypted is not None
        and expires_at is not None
        and expires_at > now + margin
    ):
        return crypto.decrypt(access_encrypted)

    settings = get_settings()
    payload = {
        "grant_type": "refresh_token",
        "client_id": settings.olx_client_id.get_secret_value(),
        "client_secret": settings.olx_client_secret.get_secret_value(),
        "refresh_token": crypto.decrypt(refresh_encrypted),
    }
    body = await _token_request(session, operation="refresh_token", payload=payload)
    await _save_tokens(session, body)
    await session.commit()
    return body["access_token"]


async def get_status(session: AsyncSession) -> OlxStatus:
    """Sprawdza stan autoryzacji OLX BEZ odswiezania tokenu i bez wywolan API.

    W odroznieniu od `get_access_token`, to czysty odczyt stanu z bazy - do
    wyswietlenia w panelu (GET /api/olx/status).

    Args:
        session: Sesja bazy danych.

    Returns:
        Stan autoryzacji: czy w ogole skonfigurowana, czy biezacy access
        token jest jeszcze wazny, kiedy wygasa i z jakim zakresem.
    """
    row = (
        await session.execute(
            text("SELECT access_expires_at, scope FROM olx_token WHERE id = 1")
        )
    ).first()
    if row is None:
        return OlxStatus(
            authorized=False,
            access_token_valid=False,
            access_expires_at=None,
            scope=None,
        )

    expires_at, scope = row
    valid = expires_at is not None and expires_at > datetime.now(UTC)
    return OlxStatus(
        authorized=True,
        access_token_valid=valid,
        access_expires_at=expires_at,
        scope=scope,
    )


def _unwrap_data(body: object) -> object:
    """Rozpakuje klucz "data", w ktory Partner API OLX opakowuje kazda odpowiedz.

    Partner API zwraca np. `{"data": [...]}` dla list (kategorie/miasta) i
    `{"data": {...}}` dla pojedynczego obiektu (utworzone ogloszenie) - bez
    tego rozpakowania parser dostaje wrapper zamiast oczekiwanego ksztaltu i
    odrzuca poprawna odpowiedz jako "nieoczekiwana odpowiedz (200)". Jedna
    funkcja uzywana przez wszystkie wywolania Partner API
    (`_parse_list_response`, `create_advert`), zeby nie powtarzac tej samej
    logiki (i tej samej pomylki) w kazdym miejscu z osobna.

    Args:
        body: Sparsowane JSON cialo odpowiedzi, albo None.

    Returns:
        `body["data"]`, gdy `body` jest dict-em z kluczem "data"; w
        przeciwnym razie `body` bez zmian.
    """
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _parse_list_response(
    response: httpx.Response, *, operation: str
) -> list[dict[str, Any]]:
    """Parsuje odpowiedz JSON, ktora ma byc (po rozpakowaniu "data") tablica obiektow.

    Args:
        response: Surowa odpowiedz httpx.
        operation: Nazwa operacji, do czytelnego komunikatu bledu.

    Returns:
        Sparsowana lista obiektow.

    Raises:
        OlxApiError: Gdy status jest bledem albo cialo (po rozpakowaniu) nie
            jest tablica.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    data = _unwrap_data(body)
    if response.status_code >= 400 or not isinstance(data, list):
        raise OlxApiError(
            f"OLX zwrocilo nieoczekiwana odpowiedz przy {operation} "
            f"({response.status_code}): {body!r}"
        )
    return data


# Pola kategorii istotne dla klienta (do wyboru category_id i ustalenia,
# czy mozna w niej wystawic ogloszenie) - OLX zwraca wiecej pol niz to,
# odrzucane przez `_compact_category`.
_CATEGORY_FIELDS = ("id", "name", "parent_id", "is_leaf", "photos_limit")

# Zabezpieczenie `_collect_leaf_matches` przed nieskonczona rekurencja, gdyby
# OLX kiedys zwrocilo cykliczne/zle dane parent_id. NIE chroni przed limitem
# OLX (4500 zadan/5 min) - to robi cache w `_category_tree_cache`, bo
# rekurencja i tak dziala na juz pobranym, cache'owanym drzewie i nie
# wykonuje przy tym zadnych dodatkowych wywolan OLX.
_MAX_CATEGORY_SEARCH_DEPTH = 20

# Cache calego (plaskiego) drzewa kategorii OLX w pamieci procesu - patrz
# `_fetch_category_tree`. Zwykla zmienna modulu (nie `@lru_cache`), bo
# wypelniana jest wewnatrz funkcji async po udanym wywolaniu, nie przy samym
# wejsciu do niej.
_category_tree_cache: list[dict[str, Any]] | None = None


def _compact_category(raw: dict[str, Any]) -> dict[str, Any]:
    """Redukuje surowy rekord kategorii OLX do pol istotnych dla klienta.

    Args:
        raw: Surowy rekord kategorii z odpowiedzi OLX.

    Returns:
        Slownik z wylacznie kluczami `_CATEGORY_FIELDS`.
    """
    return {field: raw.get(field) for field in _CATEGORY_FIELDS}


async def _fetch_category_tree(session: AsyncSession) -> list[dict[str, Any]]:
    """Pobiera (i cache'uje w pamieci procesu) cale plaskie drzewo kategorii OLX.

    OLX zwraca CALE drzewo kategorii jednym wywolaniem GET /categories -
    kazdy rekord ma id/name/parent_id/is_leaf/photos_limit, kategorie glowne
    maja parent_id=0, a wystawic ogloszenie mozna TYLKO w kategorii z
    is_leaf=true. Drzewo zmienia sie rzadko, a limit OLX to 4500 zadan/5 min,
    wiec wynik pierwszego wywolania jest trzymany w `_category_tree_cache` i
    ponownie uzywany przez kazde kolejne zejscie w glab
    (`fetch_categories`) i wyszukiwanie (`search_leaf_categories`) w tym
    samym procesie - zamiast odpytywac OLX za kazdym razem.

    Args:
        session: Sesja bazy danych (do zdobycia access tokenu przy
            pierwszym, niecache'owanym wywolaniu).

    Returns:
        Plaska lista kategorii w zwiezlym ksztalcie (`_compact_category`).

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX (tylko przy pierwszym,
            niecache'owanym wywolaniu).
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie (tylko przy
            pierwszym, niecache'owanym wywolaniu).
    """
    global _category_tree_cache
    if _category_tree_cache is not None:
        return _category_tree_cache

    token = await get_access_token(session)
    settings = get_settings()
    response = await _partner_http_client().get(
        f"{settings.olx_api_base_url}{_CATEGORIES_PATH}",
        headers={"Authorization": f"Bearer {token}"},
    )
    raw = _parse_list_response(response, operation="fetch_categories")
    _category_tree_cache = [_compact_category(c) for c in raw]
    return _category_tree_cache


async def fetch_categories(
    session: AsyncSession, *, parent_id: int | None = None, q: str | None = None
) -> list[dict[str, Any]]:
    """Zwraca kategorie OLX na jednym poziomie drzewa (dzieci `parent_id`).

    Args:
        session: Sesja bazy danych.
        parent_id: Id kategorii-rodzica, ktorej dzieci zwrocic; None dla
            kategorii glownych (parent_id=0 w danych OLX).
        q: Fragment nazwy do dodatkowego filtrowania (case-insensitive) w
            obrebie zwracanego poziomu - OLX ignoruje wyszukiwanie tekstowe
            w zadaniu, wiec filtrowanie robimy lokalnie na pobranej (i
            cache'owanej - patrz `_fetch_category_tree`) liscie.

    Returns:
        Kategorie bedace dziecmi `parent_id` (albo glowne, gdy `parent_id`
        jest None), ewentualnie dalej zawezone przez `q`.

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX.
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie.
    """
    tree = await _fetch_category_tree(session)
    effective_parent = 0 if parent_id is None else parent_id
    level = [c for c in tree if c.get("parent_id") == effective_parent]
    if not q:
        return level
    needle = q.strip().lower()
    return [c for c in level if needle in str(c.get("name", "")).lower()]


def _collect_leaf_matches(
    tree_by_parent: dict[int, list[dict[str, Any]]],
    parent_id: int,
    needle: str,
    depth: int,
) -> list[dict[str, Any]]:
    """Rekurencyjnie zbiera liscie (is_leaf=true) pasujace do `needle` w poddrzewie.

    Args:
        tree_by_parent: Kategorie zgrupowane po `parent_id`.
        parent_id: Korzen poddrzewa do przeszukania.
        needle: Znormalizowany (lowercase, przyciety) fragment nazwy.
        depth: Biezaca glebokosc rekurencji - patrz `_MAX_CATEGORY_SEARCH_DEPTH`.

    Returns:
        Kategorie-liscie z poddrzewa `parent_id`, ktorych nazwa zawiera
        `needle`.
    """
    if depth > _MAX_CATEGORY_SEARCH_DEPTH:
        return []
    matches: list[dict[str, Any]] = []
    for child in tree_by_parent.get(parent_id, []):
        if child.get("is_leaf"):
            if needle in str(child.get("name", "")).lower():
                matches.append(child)
        else:
            matches.extend(
                _collect_leaf_matches(tree_by_parent, child["id"], needle, depth + 1)
            )
    return matches


async def search_leaf_categories(session: AsyncSession, q: str) -> list[dict[str, Any]]:
    """Rekurencyjnie przeszukuje cale drzewo kategorii OLX pod katem lisci.

    Pozwala znalezc docelowa kategorie (np. "Gry i konsole") bez recznego
    klikania po drzewie poziom po poziomie - tylko liscie (is_leaf=true) sa
    zwracane, bo tylko w nich mozna wystawic ogloszenie. Dziala na
    cache'owanym drzewie (`_fetch_category_tree`) - realne wywolanie OLX
    padnie WYLACZNIE przy pierwszym uzyciu w danym procesie.

    Args:
        session: Sesja bazy danych.
        q: Fragment nazwy do wyszukania (case-insensitive).

    Returns:
        Kategorie-liscie, ktorych nazwa zawiera `q`.

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX.
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie.
    """
    tree = await _fetch_category_tree(session)
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for category in tree:
        by_parent.setdefault(category.get("parent_id", 0), []).append(category)
    needle = q.strip().lower()
    return _collect_leaf_matches(by_parent, 0, needle, depth=0)


def _compact_attribute(raw: dict[str, Any]) -> dict[str, Any]:
    """Redukuje surowy rekord atrybutu kategorii OLX do pol istotnych dla klienta.

    Ksztalt zweryfikowany empirycznie (GET /categories/2273/attributes):
    wymagalnosc jest pod kluczem "validation.required", ale dozwolone
    wartosci sa na NAJWYZSZYM poziomie rekordu ("values", nie
    "validation.values"), a kazda z nich to `{"code": ..., "label": ...}` -
    "code" jest wartoscia do wyslania w payloadzie ogloszenia (np. atrybut
    "type" kategorii Xbox ma wartosc "code": "xbox360", "label": "Xbox 360").

    Args:
        raw: Surowy rekord atrybutu z odpowiedzi OLX.

    Returns:
        Slownik z kluczami: code, label, required, values (lista
        `{"code", "label"}` dozwolonych wartosci - pusta dla atrybutow
        wolnego tekstu/liczby, bez listy wyboru).
    """
    validation = raw.get("validation") or {}
    required = bool(validation.get("required", raw.get("required", False)))
    raw_values = raw.get("values") or []
    values = [
        {"code": v.get("code", v.get("value")), "label": v.get("label")}
        if isinstance(v, dict)
        else {"code": v, "label": None}
        for v in raw_values
    ]
    return {
        "code": raw.get("code"),
        "label": raw.get("label", raw.get("name")),
        "required": required,
        "values": values,
    }


async def fetch_category_attributes(
    session: AsyncSession, category_id: int
) -> list[dict[str, Any]]:
    """Pobiera wymagane i opcjonalne atrybuty danej kategorii OLX.

    Potrzebne do ustalenia, jak przekazac w payloadzie ogloszenia cechy,
    ktorych OLX NIE wyraza przez osobna kategorie (patrz migracja
    0005_olx_category_mapping.sql - jedna kategoria "Gry" na producenta, bez
    rozroznienia generacji konsoli) - np. konkretna konsola w obrebie
    kategorii "Xbox" (2273), obok juz znanego atrybutu stanu
    (`CONDITION_ATTRIBUTE_CODE`).

    Args:
        session: Sesja bazy danych.
        category_id: Id kategorii OLX (docelowo lisc drzewa - patrz
            `search_leaf_categories`).

    Returns:
        Atrybuty w zwiezlym ksztalcie (`_compact_attribute`): code, label,
        required, values.

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX.
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie.
    """
    token = await get_access_token(session)
    settings = get_settings()
    response = await _partner_http_client().get(
        f"{settings.olx_api_base_url}{_CATEGORIES_PATH}/{category_id}/attributes",
        headers={"Authorization": f"Bearer {token}"},
    )
    raw_attributes = _parse_list_response(
        response, operation="fetch_category_attributes"
    )
    return [_compact_attribute(a) for a in raw_attributes]


_DELIVERY_ATTRIBUTE_CODE = "delivery"


async def resolve_delivery_attribute(
    session: AsyncSession, category_id: int
) -> str | None:
    """Ustala kod opcji dostawy "InPost Paczkomat 24/7 S" dla danej kategorii.

    Kod (UUID) tej opcji jest SPECYFICZNY DLA KATEGORII - potwierdzony dla
    kategorii 2273 (Xbox): "ef5414d2-1fa4-4344-bf09-d1528cfb58e1". Ten sam
    UUID moze NIE dzialac dla 2272 (PlayStation) czy 2274 (Nintendo), wiec
    zamiast zakladac wspolna wartosc, funkcja pobiera atrybuty WLASCIWEJ
    kategorii (`fetch_category_attributes`) i dopasowuje po fragmencie
    etykiety (label), nie po stalym UUID - "InPost Paczkomat" w tresci
    etykiety, konczacej sie rozmiarem "S" (odrozniajac od "...24/7 M"/"L").

    Args:
        session: Sesja bazy danych.
        category_id: Id kategorii OLX, w ktorej publikowane jest ogloszenie
            (`platform.olx_category_id`).

    Returns:
        Kod (UUID) wartosci atrybutu "delivery" pasujacej etykiecie, albo
        None, gdy kategoria nie ma atrybutu "delivery" albo zadna jego
        wartosc nie pasuje - wywolujacy (`build_advert_payload`) ma wtedy
        pominac "ad_delivery" w payloadzie, nie rzucac bledem (to pole
        opcjonalne).

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX.
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie.
    """
    attributes = await fetch_category_attributes(session, category_id)
    for attribute in attributes:
        if attribute.get("code") != _DELIVERY_ATTRIBUTE_CODE:
            continue
        for value in attribute.get("values", []):
            label = str(value.get("label") or "")
            if "InPost Paczkomat" in label and label.rstrip().endswith("S"):
                return value.get("code")
    return None


# Pola miasta istotne dla klienta (do wyboru city_id) - OLX zwraca tez
# municipality/latitude/longitude, odrzucane przez `_compact_city`.
_CITY_FIELDS = ("id", "name", "county", "region_id")

# GET /cities NIE zwraca calej Polski w jednej odpowiedzi (dziesiatki
# tysiecy miejscowosci) - domyslnie ucina po ~1000 rekordow, BEZ zadnych
# metadanych stronicowania w ciele ani naglowkach (zweryfikowane empirycznie
# - brak "links"/"meta"/page-info, tylko {"data": [...]}). OLX akceptuje za
# to `limit`/`offset` w query stringu; 10000 to potwierdzony maksymalny
# `limit` (powyzej OLX odrzuca zadanie 400-ka "This value should be between
# 0 and 10000").
_CITY_PAGE_LIMIT = 10000
# Zabezpieczenie `_fetch_city_list` przed nieskonczona petla, gdyby OLX
# przestal kiedys zwracac strone krotsza niz `_CITY_PAGE_LIMIT` (sygnal
# konca danych, na ktorym opiera sie petla). 20 stron * 10000 = 200k miast -
# dzisiejszy pelny zbior to ok. 53 tys., wiec to margines, nie realny limit.
_MAX_CITY_PAGES = 20

# Cache pelnej (przefiltrowanej i splaszczonej) listy miast OLX w pamieci
# procesu - miasta praktycznie sie nie zmieniaja, a pelne pobranie to od
# razu kilka zadan do OLX (limit 4500 zadan/5 min), wiec robimy to raz na
# proces, tak samo jak `_category_tree_cache`.
_city_list_cache: list[dict[str, Any]] | None = None


def _compact_city(raw: dict[str, Any]) -> dict[str, Any]:
    """Redukuje surowy rekord miasta OLX do pol istotnych dla klienta.

    Args:
        raw: Surowy rekord miasta z odpowiedzi OLX.

    Returns:
        Slownik z wylacznie kluczami `_CITY_FIELDS`.
    """
    return {field: raw.get(field) for field in _CITY_FIELDS}


def _normalize_search_text(value: str) -> str:
    """Sprowadza tekst do porownywalnej postaci: male litery, bez diakrytykow.

    Pozwala "krakow"/"krak" znalezc "Krakow" - `unicodedata` dekomponuje
    wiekszosc polskich znakow diakrytycznych (np. "o" z ogonkiem -> "o" +
    znak kombinujacy, ktory potem odrzucamy), ale NIE "l" przekreslone
    ("Lodz") - to jedyny polski znak bez takiej dekompozycji, wiec jest
    tlumaczony recznie PRZED normalizacja.

    Args:
        value: Tekst wejsciowy (np. nazwa miasta albo fraza wyszukiwania).

    Returns:
        Tekst maly-literowy, bez znakow diakrytycznych.
    """
    value = value.replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


async def _fetch_city_list(session: AsyncSession) -> list[dict[str, Any]]:
    """Pobiera (i cache'uje w pamieci procesu) cala liste miast OLX.

    Petla stronicuje przez `limit`/`offset` (patrz komentarz przy
    `_CITY_PAGE_LIMIT`) dopoki OLX nie zwroci strony krotszej niz
    `_CITY_PAGE_LIMIT` - to sygnal konca danych. Wynik pierwszego wywolania
    jest trzymany w `_city_list_cache` i ponownie uzywany przez kazde
    kolejne wyszukiwanie (`fetch_cities`) w tym samym procesie.

    Args:
        session: Sesja bazy danych (do zdobycia access tokenu przy
            pierwszym, niecache'owanym wywolaniu).

    Returns:
        Plaska lista miast w zwiezlym ksztalcie (`_compact_city`).

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX (tylko przy pierwszym,
            niecache'owanym wywolaniu).
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie (tylko przy
            pierwszym, niecache'owanym wywolaniu).
    """
    global _city_list_cache
    if _city_list_cache is not None:
        return _city_list_cache

    token = await get_access_token(session)
    settings = get_settings()
    client = _partner_http_client()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{settings.olx_api_base_url}{_CITIES_PATH}"

    cities: list[dict[str, Any]] = []
    offset = 0
    for _ in range(_MAX_CITY_PAGES):
        response = await client.get(
            url,
            headers=headers,
            params={"limit": _CITY_PAGE_LIMIT, "offset": offset},
        )
        page = _parse_list_response(response, operation="fetch_cities")
        cities.extend(_compact_city(c) for c in page)
        if len(page) < _CITY_PAGE_LIMIT:
            break
        offset += _CITY_PAGE_LIMIT

    _city_list_cache = cities
    return cities


async def fetch_cities(
    session: AsyncSession, q: str | None = None
) -> list[dict[str, Any]]:
    """Wyszukuje miasta OLX po nazwie - pomocnicze, do ustalenia city_id.

    OLX ignoruje wyszukiwanie tekstowe w zadaniu do /cities, wiec
    filtrowanie robimy lokalnie na pobranej (i cache'owanej - patrz
    `_fetch_city_list`) liscie, bez uwzgledniania wielkosci liter i znakow
    diakrytycznych (`_normalize_search_text`) - dzieki temu "krakow" i
    "krak" znajduja "Krakow". Trafienia zaczynajace sie od `q` sa
    sortowane jako pierwsze (stabilnie, wiec kolejnosc OLX w obrebie kazdej
    z dwoch grup jest zachowana).

    Args:
        session: Sesja bazy danych.
        q: Fragment nazwy do wyszukania, albo None dla pelnej listy.

    Returns:
        Miasta pasujace do `q` (albo wszystkie, gdy `q` jest puste).

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX.
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie.
    """
    cities = await _fetch_city_list(session)
    if not q:
        return cities
    needle = _normalize_search_text(q.strip())
    if not needle:
        return cities

    matches = [
        c for c in cities if needle in _normalize_search_text(str(c.get("name", "")))
    ]
    matches.sort(
        key=lambda c: (
            not _normalize_search_text(str(c.get("name", ""))).startswith(needle)
        )
    )
    return matches


_DISTRICT_FIELDS = ("id", "name")


def _compact_district(raw: dict[str, Any]) -> dict[str, Any]:
    """Redukuje surowy rekord dzielnicy OLX do pol istotnych dla klienta.

    Args:
        raw: Surowy rekord dzielnicy z odpowiedzi OLX.

    Returns:
        Slownik z wylacznie kluczami `_DISTRICT_FIELDS`.
    """
    return {field: raw.get(field) for field in _DISTRICT_FIELDS}


async def fetch_districts(session: AsyncSession, city_id: int) -> list[dict[str, Any]]:
    """Pobiera dzielnice danego miasta OLX - do ustalenia district_id.

    Uzywa GET /cities/{city_id}/districts, NIE GET /cities/{city_id} - ten
    drugi zwraca ten sam plaski rekord miasta co /cities (id/name/county/
    region_id/...), bez zadnego pola z dzielnicami (zweryfikowane
    empirycznie). GET /districts (bez city_id w sciezce) tez istnieje, ale
    zwraca NIEFILTROWANA liste WSZYSTKICH dzielnic w Polsce - `city_id` jako
    query param jest ignorowany, tak samo jak wyszukiwanie tekstowe przy
    /categories i /cities (patrz `fetch_categories`/`fetch_cities`).

    Male miejscowosci nie maja podzialu na dzielnice - OLX zwraca wtedy
    pusta liste (zweryfikowane empirycznie), nie blad.

    Args:
        session: Sesja bazy danych.
        city_id: Id miasta OLX (z /api/olx/cities).

    Returns:
        Dzielnice miasta w zwiezlym ksztalcie (`_compact_district`): id,
        name. Pusta lista, gdy miasto nie ma podzialu na dzielnice.

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX.
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie.
    """
    token = await get_access_token(session)
    settings = get_settings()
    response = await _partner_http_client().get(
        f"{settings.olx_api_base_url}{_CITIES_PATH}/{city_id}/districts",
        headers={"Authorization": f"Bearer {token}"},
    )
    districts = _parse_list_response(response, operation="fetch_districts")
    return [_compact_district(d) for d in districts]


def build_title(game_title: str, platform_generation: str) -> str:
    """Buduje tytul ogloszenia wg sprawdzonego szablonu sklepu.

    Gdy pelny tytul przekracza MAX_TITLE_LENGTH, kolejno odrzucane sa
    OPCJONALNE segmenty koncowki - najpierw " | Wymiana", potem takze
    " | Wysyłka" - NIGDY tytul gry ani nazwa platformy: to jedyne segmenty
    niosace realna informacje o ofercie, wiec obciecie ktoregos w polowie
    (albo w calosci) zniweczyloby sens ogloszenia. Jesli nawet bez obu
    opcjonalnych segmentow tytul sie nie miesci (skrajnie dlugi tytul gry),
    to przypadek do recznej korekty w poczekalni, nie do automatycznego
    obcinania - stad blad zamiast dalszego skracania.

    Args:
        game_title: Tytul gry.
        platform_generation: Etykieta generacji platformy (platform.generation,
            np. "PS4/PS5", "Xbox 360"), albo zamiennik opisowy, gdy platforma
            jest "other".

    Returns:
        Tytul gotowy do wyslania w payloadzie OLX - pelny, albo (gdy za
        dlugi) bez " | Wymiana", albo (gdy nadal za dlugi) bez
        " | Wymiana" i " | Wysyłka". Tytul gry i platforma sa zawsze w
        calosci.

    Raises:
        OlxValidationError: Gdy tytul przekracza MAX_TITLE_LENGTH znakow
            NAWET bez obu opcjonalnych segmentow - lepiej to wykryc przed
            wyslaniem niz dostac 4xx z OLX (bez srodowiska testowego kazda
            proba sie liczy).
    """
    base = f"{game_title} | {platform_generation} | Sklep | Kraków"
    candidates = [
        f"{base} | Wysyłka | Wymiana",
        f"{base} | Wysyłka",
        base,
    ]
    for title in candidates:
        if len(title) <= MAX_TITLE_LENGTH:
            return title

    raise OlxValidationError(
        f"Tytul ogloszenia ma {len(base)} znakow (limit {MAX_TITLE_LENGTH}) "
        f'nawet bez segmentow " | Wysyłka" i " | Wymiana": {base!r}. Tytul '
        "gry i platforma nigdy nie sa ucinane - skroc recznie tytul gry w "
        "poczekalni i sprobuj ponownie."
    )


def build_description(
    *,
    manufacturer: str,
    console_name: str,
    game_title: str,
    condition: Literal["new", "used"],
) -> str:
    """Buduje opis ogloszenia wg sprawdzonego szablonu sklepu.

    Args:
        manufacturer: Kod producenta platformy (platform.manufacturer:
            "sony"/"microsoft"/"nintendo"/"other").
        console_name: Nazwa konsoli do opisu (np. "PlayStation 4").
        game_title: Tytul gry.
        condition: Stan egzemplarza.

    Returns:
        Pelny opis ogloszenia po polsku.
    """
    manufacturer_pl = _MANUFACTURER_PL.get(manufacturer, manufacturer.capitalize())
    condition_pl = _CONDITION_PL[condition]
    # Zlozone z krotkich linii zrodlowych (limit 88 znakow), ale KAZDY akapit
    # to jedna logiczna linia tresci ogloszenia - stad "\n\n".join, nie
    # wieloliniowy f-string (ten psulby sie na dlugich akapitach ponizej).
    paragraphs = [
        f"Sklep ZibiCom zaprasza do zakupu gry na konsole {manufacturer_pl} "
        f"{console_name} - {game_title}",
        f"Gra jest {condition_pl}.",
        "Odbiór osobisty w Krakowie w sklepie znajdującym się przy Ulicy Wlotowej 2a.",
        "PROSIMY O KONTAKT PRZED ZŁOŻENIEM ZAMÓWIENIA/PRZYJAZDEM W CELU "
        "POTWIERDZENIA DOSTĘPNOŚCI PRODUKTU I ZROBIENIA REZERWACJI!",
        "Na miejscu posiadamy setki gier na konsole !",
        "Oferujesz zamianę? Masz w domu niepotrzebne gry? Przynieś je w rozliczeniu !",
        "Prowadzimy skup, sprzedaż oraz wymianę gier na konsole PS2, PS3, "
        "PS4, PS5, PSP, PS Vita, Xbox 360, Xbox One, Xbox Series, Nintendo "
        "Switch 1 i 2",
        "Odwiedź nas na Facebooku gdzie znajdziesz nowości, konkursy, "
        "promocje oraz wiele wiele więcej!",
        "Zapraszamy na nasze pozostałe ogłoszenia !",
        "W przypadku chęci kupna kilku rzeczy z kilku aukcji, prosimy o kontakt.",
    ]
    return "\n\n".join(paragraphs)


def build_advert_payload(
    *,
    title: str,
    description: str,
    category_id: int,
    city_id: int,
    district_id: int,
    price_pln: Decimal,
    condition: Literal["new", "used"],
    platform_olx_attribute_value: str | None,
    image_urls: Sequence[str],
    contact_name: str,
) -> dict[str, Any]:
    """Buduje payload ogloszenia zgodny z OLX Partner API (POST /adverts).

    OLX pobiera zdjecia SAM ze wskazanych URL-i - nie przyjmuje binarnego
    uploadu. `image_urls` musza wiec byc juz publicznie dostepne (R2), co
    zapewnia `zibicom.photos.upload_photo` przed wywolaniem tej funkcji.

    `advertiser_type` i `contact.name` sa wymagane przez OLX (bez nich
    create_advert dostaje 400) - konto zibicom jest firmowe, wiec pierwsze
    jest stala wartoscia "business"; nazwa kontaktowa pochodzi z
    konfiguracji (`Settings.olx_contact_name`), nie jest stala tutaj.

    NIE ma tu "auto_extend_enabled" ANI "ad_delivery", mimo ze oba sa
    widoczne w odczycie ogloszenia (GET /adverts/{id}) - POST /adverts
    odrzuca oba (pierwsze 400-ka "Ten formularz nie powinien zawierac
    dodatkowych pol", drugie pustym 400 "Data validation error occurred",
    ustalonym przez porownanie udanego payloadu bez "ad_delivery" z
    odrzuconym, ktory je mial - wszystkie inne pola byly identyczne). Obecnosc
    pola w odczycie NIE oznacza, ze jest akceptowane przy zapisie - to
    najwyrazniej stan wynikowy ustawiany przez OLX po stronie serwera, nie
    pole wejsciowe. `resolve_delivery_attribute` zostaje w kodzie (przyda
    sie, gdy ustalimy wlasciwy sposob ustawiania dostawy - prawdopodobnie
    osobne wywolanie API, nie pole payloadu tworzenia), ale NIE jest tu
    wywolywane.

    Args:
        title: Tytul (zibicom.olx.build_title).
        description: Opis (zibicom.olx.build_description).
        category_id: Id kategorii OLX (z konfiguracji albo /api/olx/categories).
        city_id: Id miasta OLX (z konfiguracji albo /api/olx/cities).
        district_id: Id dzielnicy OLX (z konfiguracji albo
            /api/olx/cities/{city_id}/districts), albo 0, gdy nieustawiona -
            wtedy pomijana w payloadzie. OLX wymaga jej TYLKO dla miast z
            podzialem na dzielnice (np. Krakow) - wyslanie jej dla malej
            miejscowosci bez dzielnic psuje publikacje, wiec NIE jest
            dolaczana bezwarunkowo tak jak city_id.
        price_pln: Cena w PLN.
        condition: Stan egzemplarza.
        platform_olx_attribute_value: Wartosc atrybutu platformy
            (`PLATFORM_ATTRIBUTE_CODE`, `platform.olx_attribute_value`),
            albo None, gdy nieustawiona w slowniku - wtedy atrybut platformy
            jest pomijany w payloadzie (w odroznieniu od atrybutu stanu,
            ktory OLX wymaga zawsze).
        image_urls: Publiczne URL-e zdjec (R2), maks. MAX_IMAGES.
        contact_name: Nazwa kontaktowa wyswietlana w ogloszeniu
            (Settings.olx_contact_name).

    Returns:
        Payload gotowy do wyslania w tresci POST /adverts.

    Raises:
        OlxValidationError: Gdy zdjec jest wiecej niz MAX_IMAGES.
    """
    if len(image_urls) > MAX_IMAGES:
        raise OlxValidationError(
            f"OLX przyjmuje maksymalnie {MAX_IMAGES} zdjec, "
            f"otrzymano {len(image_urls)}."
        )

    # "state" mapuje sie 1:1 na nasz enum listing_condition ("new"/"used") -
    # to sa dokladnie kody, ktorych oczekuje OLX (zweryfikowane empirycznie,
    # patrz komentarz przy CONDITION_ATTRIBUTE_CODE), zaden slownik nie jest
    # potrzebny.
    attributes = [{"code": CONDITION_ATTRIBUTE_CODE, "value": condition}]
    if platform_olx_attribute_value:
        attributes.append(
            {"code": PLATFORM_ATTRIBUTE_CODE, "value": platform_olx_attribute_value}
        )

    location: dict[str, Any] = {"city_id": city_id}
    if district_id > 0:
        location["district_id"] = district_id

    return {
        "title": title,
        "description": description,
        "category_id": category_id,
        "location": location,
        "price": {"value": float(price_pln), "currency": "PLN"},
        "images": [{"url": url} for url in image_urls],
        "attributes": attributes,
        "advertiser_type": "business",
        "contact": {"name": contact_name},
    }


async def create_advert(
    session: AsyncSession,
    payload: dict[str, Any],
    *,
    access_token: str,
    listing_id: int | None = None,
) -> dict[str, Any]:
    """Publikuje ogloszenie na OLX (POST /adverts) i loguje wywolanie w olx_operation.

    Przyjmuje juz zdobyty `access_token` (zamiast wywolywac
    `get_access_token` samodzielnie) - patrz docstring `get_access_token` o
    tym, dlaczego wywolujacy musi zdobyc token PRZED otwarciem wiekszej
    transakcji. Ta funkcja sama NIE commituje - jest czescia transakcji
    publikacji pozycji poczekalni (`zibicom.intake.publish_item`), ktora ma
    zatwierdzic sie w calosci na koniec, dopiero po sukcesie OLX.

    Ogloszenie po publikacji przechodzi moderacje OLX (status
    new -> waiting -> active) - status w odpowiedzi NIE oznacza, ze
    ogloszenie jest juz widoczne.

    Args:
        session: Sesja bazy danych (ta sama, w ktorej trwa transakcja
            publikacji).
        payload: Payload ogloszenia (zibicom.olx.build_advert_payload).
        access_token: Wazny access token, zdobyty przez `get_access_token`.
        listing_id: Id oferty, do ktorej przypisac wpis audytowy.

    Returns:
        Odpowiedz OLX (zawiera co najmniej pole "id" utworzonego ogloszenia
        i jego "status" moderacji).

    Raises:
        OlxApiError: Gdy wywolanie sieciowe albo odpowiedz OLX wskazuje na
            blad - transakcja publikacji jest wtedy wycofywana przez
            wywolujacego, wiec w bazie NIE zostaje oferta bez odpowiednika
            na OLX ani (przy bledzie samego wywolania) ogloszenie na OLX bez
            rekordu w bazie.
    """
    settings = get_settings()
    request_log = _redact(payload)

    try:
        response = await _partner_http_client().post(
            f"{settings.olx_api_base_url}{_ADVERTS_PATH}",
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    except httpx.HTTPError as exc:
        await _log_operation(
            session,
            listing_id=listing_id,
            operation="create_advert",
            request_payload=request_log,
            response_payload=None,
            http_status=None,
            succeeded=False,
            olx_error=str(exc),
        )
        raise OlxApiError(
            f"Wywolanie OLX create_advert nie powiodlo sie: {exc}"
        ) from exc

    try:
        body = response.json()
    except ValueError:
        body = None
    data = _unwrap_data(body)

    succeeded = response.status_code < 400 and isinstance(data, dict)
    error_detail = None if succeeded else _error_detail(response)
    await _log_operation(
        session,
        listing_id=listing_id,
        operation="create_advert",
        request_payload=request_log,
        response_payload=data if isinstance(data, dict) else None,
        http_status=response.status_code,
        succeeded=succeeded,
        olx_error=error_detail,
    )

    if not succeeded:
        raise OlxApiError(
            f"OLX zwrocilo blad {response.status_code} przy tworzeniu "
            f"ogloszenia: {error_detail}"
        )
    return data
