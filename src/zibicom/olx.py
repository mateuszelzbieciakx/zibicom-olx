"""Integracja z OLX Partner API: autoryzacja, publikacja ogłoszeń.

OLX NIE ma środowiska testowego - każda publikacja to prawdziwe ogłoszenie
na koncie firmowym. Autoryzacja jest PÓŁRĘCZNA: aplikacja nie ma działającego
publicznego callbacku (redirect_uri wskazuje na R2, który nie obsługuje
żądań), więc człowiek otwiera URL logowania, loguje się, a po przekierowaniu
przepisuje parametr `code` z paska adresu i wkleja go do POST /api/olx/exchange.

Refresh token ROTUJE przy KAŻDYM odświeżeniu access tokenu (żywotność ok.
miesiąca) - nowa wartość jest zapisywana natychmiast po otrzymaniu, inaczej
kolejne odświeżenie nie ma już czym się uwierzytelnić i autoryzacja jest
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

# Kody atrybutów OLX (per kategoria) - zweryfikowane empirycznie dla
# kategorii Xbox (2273, GET /api/olx/categories/2273/attributes, patrz
# `fetch_category_attributes`): "state" jest WYMAGANY i przyjmuje dokładnie
# nasze wartości enuma listing_condition ("new"/"used"), więc mapuje się 1:1
# bez słownika. "type" jest opcjonalny - dozwolone kody są per kategoria
# (czyli per producent, patrz migracje 0005/0006), stąd
# platform.olx_attribute_value zamiast stałej wartości tutaj. Sony (2272) i
# Nintendo (2274) NIE były jeszcze zweryfikowane - jeśli mają inny kod niż
# "type" dla tego samego atrybutu, trzeba to sprawdzić przed pierwszą
# prawdziwą publikacją dla tych producentów. Błędny kod kończy się 4xx z OLX
# (zalogowanym w olx_operation przez create_advert) - NIE tworzy ogłoszenia,
# więc pomyłka tutaj jest bezpieczna do naprawienia.
CONDITION_ATTRIBUTE_CODE = "state"
PLATFORM_ATTRIBUTE_CODE = "type"

_CONDITION_PL = {"new": "nowa", "used": "używana"}
_MANUFACTURER_PL = {"sony": "Sony", "microsoft": "Microsoft", "nintendo": "Nintendo"}

# Klucze NIGDY nie trafiające w postaci jawnej do olx_operation - zapisanie
# tam tokenu zniweczyłoby sens jego szyfrowania w olx_token (patrz
# migrations/0004_olx_token.sql).
_SENSITIVE_KEYS = {"client_secret", "refresh_token", "access_token", "code"}


class OlxError(Exception):
    """Błąd domenowy integracji OLX - komunikat po polsku, gotowy dla klienta."""


class OlxAuthError(OlxError):
    """Brak ważnej autoryzacji OLX (kod nigdy nie został wymieniony)."""


class OlxApiError(OlxError):
    """Wywołanie OLX API się nie powiodło (błąd sieci albo odpowiedź 4xx/5xx)."""


class OlxValidationError(OlxError):
    """Payload ogłoszenia narusza ograniczenie OLX (np. za długi tytuł)."""


class OlxStatus(BaseModel):
    """Stan autoryzacji OLX do wyświetlenia w panelu.

    Attributes:
        authorized: Czy w bazie jest zapisany refresh token.
        access_token_valid: Czy bieżący access token jest jeszcze ważny, bez
            próbowania go odświeżyć.
        access_expires_at: Moment wygaśnięcia bieżącego access tokenu.
        scope: Zakres uprawnień z ostatniej autoryzacji/odświeżenia.
    """

    authorized: bool
    access_token_valid: bool
    access_expires_at: datetime | None
    scope: str | None


# Domyślny User-Agent httpx ("python-httpx/...") jest przez CloudFront/WAF
# OLX traktowany jako ruch botowy i blokowany 403-ką z pustym ciałem - bez
# względu na poprawność autoryzacji. Potwierdzone eksperymentalnie: to samo
# żądanie z własnym User-Agentem dostaje 200. Nagłówki muszą więc być
# ustawione na kliencie (dla WSZYSTKICH wywołań OLX), nie doklejane per-request.
_DEFAULT_HEADERS = {
    "User-Agent": "zibicom-olx/0.1",
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# Partner API (kategorie/miasta/adverts) odrzuca żądania bez nagłówka
# Version 400-ką "Missing required 'Version' header!". Endpoint OAuth
# (/api/open/oauth/token) NIE jest częścią Partner API i działa poprawnie
# BEZ tego nagłówka - stąd osobny klient zamiast dopisania Version do
# _DEFAULT_HEADERS, żeby nie ryzykować zepsucia działającego już OAuth.
_PARTNER_HEADERS = {**_DEFAULT_HEADERS, "Version": "2.0"}


@lru_cache
def _http_client() -> httpx.AsyncClient:
    """Buduje (raz na proces) klienta HTTP do endpointu OAuth OLX.

    Tworzenie nowego klienta przy każdym wywołaniu prowadzi do tego samego
    błędu, co przy kliencie Gemini (zibicom.vision._client) - GC zamyka
    porzucony transport i kolejne wywołania w tym samym procesie dostają
    "Cannot send a request, as the client has been closed". `lru_cache` bez
    argumentów trzyma jedną, współdzieloną instancję przez cały czas życia
    procesu.

    Używany WYŁĄCZNIE przez `_token_request` (/api/open/oauth/token) - do
    Partner API służy `_partner_http_client` (inne wymagane nagłówki).

    Returns:
        Asynchroniczny klient httpx z rozsądnym timeoutem i nagłówkami
        (`_DEFAULT_HEADERS`) omijającymi blokadę WAF/CloudFront OLX.
    """
    return httpx.AsyncClient(timeout=30.0, headers=_DEFAULT_HEADERS)


@lru_cache
def _partner_http_client() -> httpx.AsyncClient:
    """Buduje (raz na proces) klienta HTTP do Partner API OLX.

    Jak `_http_client` (jeden współdzielony klient na cały proces, żeby
    uniknąć "client has been closed"), ale z dodatkowym nagłówkiem Version
    wymaganym WYŁĄCZNIE przez Partner API (kategorie/miasta/adverts) - patrz
    komentarz przy `_PARTNER_HEADERS`.

    Returns:
        Asynchroniczny klient httpx z rozsądnym timeoutem i nagłówkami
        `_PARTNER_HEADERS`.
    """
    return httpx.AsyncClient(timeout=30.0, headers=_PARTNER_HEADERS)


async def dispose_http_client() -> None:
    """Zamyka współdzielone klienty HTTP (wywoływane przy zamykaniu aplikacji)."""
    if _http_client.cache_info().currsize:
        await _http_client().aclose()
    _http_client.cache_clear()
    if _partner_http_client.cache_info().currsize:
        await _partner_http_client().aclose()
    _partner_http_client.cache_clear()


def _error_detail(response: httpx.Response) -> str:
    """Wydobywa czytelny szczegół błędu z odpowiedzi OLX (do logu i wyjątku).

    Używa `response.text` (nie sparsowanego JSON-a) - odpowiedzi blokady
    WAF/CloudFront (patrz `_DEFAULT_HEADERS`) często nie są poprawnym JSON-em
    albo są całkowicie puste, więc poleganie na `response.json()` gubiło
    treść błędu i zostawiało tylko "None" w logu, co uniemożliwiało
    diagnozę.

    Args:
        response: Surowa odpowiedź httpx zakończona błędem.

    Returns:
        Tekst odpowiedzi przycięty do 500 znaków, albo jawny opis pustej
        odpowiedzi, gdy `response.text` jest puste/samymi białymi znakami.
    """
    body_text = response.text
    if not body_text.strip():
        return "pusta odpowiedź — prawdopodobnie blokada WAF/CloudFront"
    return body_text[:500]


def _redact(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Maskuje wrażliwe pola przed zapisaniem wywołania w olx_operation.

    Args:
        payload: Surowy słownik żądania/odpowiedzi, albo None.

    Returns:
        Kopia słownika z wartościami kluczy z `_SENSITIVE_KEYS` zastąpionymi
        `"***"`, albo None, gdy wejście było None.
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
    """Zapisuje wpis audytowy wywołania OLX API w `olx_operation`.

    Funkcja NIE commituje - o momencie zatwierdzenia decyduje wywołujący
    (patrz `create_advert`, które jest częścią większej transakcji publikacji).

    Args:
        session: Sesja bazy danych.
        listing_id: Id oferty, której dotyczy wywołanie, albo None dla
            wywołań niezwiązanych z ofertą (np. odświeżenie tokenu).
        operation: Krótka nazwa operacji (np. "create_advert", "refresh_token").
        request_payload: Wysłany payload (już zredagowany przez `_redact`).
        response_payload: Odpowiedź OLX (już zredagowana przez `_redact`).
        http_status: Kod HTTP odpowiedzi, albo None przy błędzie sieciowym.
        succeeded: Czy wywołanie zakończyło się sukcesem.
        olx_error: Czytelny opis błędu, gdy `succeeded` jest False.
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
    """Wysyła żądanie do endpointu tokenu OLX i loguje wywołanie w olx_operation.

    Wspólna implementacja dla `exchange_code` (grant_type=authorization_code)
    i odświeżania w `get_access_token` (grant_type=refresh_token) - obie
    ścieżki mają identyczną logikę wywołania, logowania błędów i parsowania.

    Args:
        session: Sesja bazy danych.
        operation: Nazwa operacji do zapisu w olx_operation ("oauth_exchange"
            albo "refresh_token").
        payload: Ciało żądania POST (zawiera client_secret/refresh_token/code
            - redagowane przed zapisem do olx_operation).

    Returns:
        Sparsowana odpowiedź JSON OLX (zawiera access_token, refresh_token,
        expires_in, scope).

    Raises:
        OlxApiError: Gdy wywołanie sieciowe się nie powiodło albo OLX
            zwróciło błąd / odpowiedź niezgodną z oczekiwanym kształtem.
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
            f"Wywołanie OLX ({operation}) nie powiodło się: {exc}"
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
            f"OLX zwróciło błąd {response.status_code} przy {operation}: {error_detail}"
        )
    return body


async def _save_tokens(session: AsyncSession, body: dict[str, Any]) -> None:
    """Zapisuje parę tokenów z odpowiedzi OLX, zaszyfrowanych Fernetem.

    UPSERT na singletonie `olx_token` (id=1) - `exchange_code` tworzy
    pierwszy wiersz, każde kolejne odświeżenie go nadpisuje. Funkcja NIE
    commituje - wywołujący robi to natychmiast po powrocie (patrz hard fact
    o rotacji refresh tokenu).

    Args:
        session: Sesja bazy danych.
        body: Odpowiedź OLX zawierająca access_token, refresh_token,
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
    """Buduje URL logowania OAuth OLX do otwarcia ręcznie w przeglądarce.

    Autoryzacja jest półręczna (patrz docstring modułu) - ta funkcja tylko
    składa URL, nie wykonuje żadnego wywołania sieciowego.

    Returns:
        Pełny URL `GET .../oauth/authorize/` ze scope "v2 read write".
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
    """Wymienia kod autoryzacyjny (z paska adresu) na pierwszą parę tokenów.

    Args:
        session: Sesja bazy danych.
        code: Kod `code` przepisany ręcznie z paska adresu po przekierowaniu
            OLX (ważny tylko przez chwilę - trzeba go wymienić od razu).

    Raises:
        OlxApiError: Gdy wymiana się nie powiedzie (np. kod wygasł).
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
    """Zwraca ważny access token, odświeżając go z 60-sekundowym marginesem.

    WAŻNE dla wywołujących w większej transakcji biznesowej (patrz
    `zibicom.intake.publish_item`): gdy token wymaga odświeżenia, ta funkcja
    zapisuje NOWĄ parę tokenów i NATYCHMIAST commituje - refresh token
    rotuje przy każdym odświeżeniu, więc zwłoka w zapisie oznacza trwałą
    utratę autoryzacji. Wywołujący MUSI więc wywołać tę funkcję PRZED
    rozpoczęciem własnych, jeszcze niezacommitowanych zapisów w tej samej
    sesji - inaczej ten commit przedwcześnie zatwierdzi też ich częściowy
    stan.

    Args:
        session: Sesja bazy danych.

    Returns:
        Ważny access token (jawny str, gotowy do nagłówka Authorization).

    Raises:
        OlxAuthError: Gdy autoryzacja nigdy nie została wykonana (brak
            wiersza w `olx_token`).
        OlxApiError: Gdy odświeżenie tokenu się nie powiedzie.
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
            "Brak autoryzacji OLX - wykonaj GET /api/olx/authorize, zaloguj się "
            "w przeglądarce pod zwróconym URL-em, a następnie prześlij kod "
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
    """Sprawdza stan autoryzacji OLX BEZ odświeżania tokenu i bez wywołań API.

    W odróżnieniu od `get_access_token`, to czysty odczyt stanu z bazy - do
    wyświetlenia w panelu (GET /api/olx/status).

    Args:
        session: Sesja bazy danych.

    Returns:
        Stan autoryzacji: czy w ogóle skonfigurowana, czy bieżący access
        token jest jeszcze ważny, kiedy wygasa i z jakim zakresem.
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
    """Rozpakuje klucz "data", w który Partner API OLX opakowuje każdą odpowiedź.

    Partner API zwraca np. `{"data": [...]}` dla list (kategorie/miasta) i
    `{"data": {...}}` dla pojedynczego obiektu (utworzone ogłoszenie) - bez
    tego rozpakowania parser dostaje wrapper zamiast oczekiwanego kształtu i
    odrzuca poprawną odpowiedź jako "nieoczekiwana odpowiedź (200)". Jedna
    funkcja używana przez wszystkie wywołania Partner API
    (`_parse_list_response`, `create_advert`), żeby nie powtarzać tej samej
    logiki (i tej samej pomyłki) w każdym miejscu z osobna.

    Args:
        body: Sparsowane JSON ciało odpowiedzi, albo None.

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
    """Parsuje odpowiedź JSON, która ma być (po rozpakowaniu "data") tablicą obiektów.

    Args:
        response: Surowa odpowiedź httpx.
        operation: Nazwa operacji, do czytelnego komunikatu błędu.

    Returns:
        Sparsowana lista obiektów.

    Raises:
        OlxApiError: Gdy status jest błędem albo ciało (po rozpakowaniu) nie
            jest tablicą.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    data = _unwrap_data(body)
    if response.status_code >= 400 or not isinstance(data, list):
        raise OlxApiError(
            f"OLX zwróciło nieoczekiwaną odpowiedź przy {operation} "
            f"({response.status_code}): {body!r}"
        )
    return data


# Pola kategorii istotne dla klienta (do wyboru category_id i ustalenia,
# czy można w niej wystawić ogłoszenie) - OLX zwraca więcej pól niż to,
# odrzucane przez `_compact_category`.
_CATEGORY_FIELDS = ("id", "name", "parent_id", "is_leaf", "photos_limit")

# Zabezpieczenie `_collect_leaf_matches` przed nieskończoną rekurencją, gdyby
# OLX kiedyś zwróciło cykliczne/złe dane parent_id. NIE chroni przed limitem
# OLX (4500 żądań/5 min) - to robi cache w `_category_tree_cache`, bo
# rekurencja i tak działa na już pobranym, cache'owanym drzewie i nie
# wykonuje przy tym żadnych dodatkowych wywołań OLX.
_MAX_CATEGORY_SEARCH_DEPTH = 20

# Cache całego (płaskiego) drzewa kategorii OLX w pamięci procesu - patrz
# `_fetch_category_tree`. Zwykła zmienna modułu (nie `@lru_cache`), bo
# wypełniana jest wewnątrz funkcji async po udanym wywołaniu, nie przy samym
# wejściu do niej.
_category_tree_cache: list[dict[str, Any]] | None = None


def _compact_category(raw: dict[str, Any]) -> dict[str, Any]:
    """Redukuje surowy rekord kategorii OLX do pól istotnych dla klienta.

    Args:
        raw: Surowy rekord kategorii z odpowiedzi OLX.

    Returns:
        Słownik z wyłącznie kluczami `_CATEGORY_FIELDS`.
    """
    return {field: raw.get(field) for field in _CATEGORY_FIELDS}


async def _fetch_category_tree(session: AsyncSession) -> list[dict[str, Any]]:
    """Pobiera (i cache'uje w pamięci procesu) całe płaskie drzewo kategorii OLX.

    OLX zwraca CAŁE drzewo kategorii jednym wywołaniem GET /categories -
    każdy rekord ma id/name/parent_id/is_leaf/photos_limit, kategorie główne
    mają parent_id=0, a wystawić ogłoszenie można TYLKO w kategorii z
    is_leaf=true. Drzewo zmienia się rzadko, a limit OLX to 4500 żądań/5 min,
    więc wynik pierwszego wywołania jest trzymany w `_category_tree_cache` i
    ponownie używany przez każde kolejne zejście w głąb
    (`fetch_categories`) i wyszukiwanie (`search_leaf_categories`) w tym
    samym procesie - zamiast odpytywać OLX za każdym razem.

    Args:
        session: Sesja bazy danych (do zdobycia access tokenu przy
            pierwszym, niecache'owanym wywołaniu).

    Returns:
        Płaska lista kategorii w zwięzłym kształcie (`_compact_category`).

    Raises:
        OlxAuthError: Gdy brak ważnej autoryzacji OLX (tylko przy pierwszym,
            niecache'owanym wywołaniu).
        OlxApiError: Gdy wywołanie OLX się nie powiedzie (tylko przy
            pierwszym, niecache'owanym wywołaniu).
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
        parent_id: Id kategorii-rodzica, której dzieci zwrócić; None dla
            kategorii głównych (parent_id=0 w danych OLX).
        q: Fragment nazwy do dodatkowego filtrowania (case-insensitive) w
            obrębie zwracanego poziomu - OLX ignoruje wyszukiwanie tekstowe
            w żądaniu, więc filtrowanie robimy lokalnie na pobranej (i
            cache'owanej - patrz `_fetch_category_tree`) liście.

    Returns:
        Kategorie będące dziećmi `parent_id` (albo główne, gdy `parent_id`
        jest None), ewentualnie dalej zawężone przez `q`.

    Raises:
        OlxAuthError: Gdy brak ważnej autoryzacji OLX.
        OlxApiError: Gdy wywołanie OLX się nie powiedzie.
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
    """Rekurencyjnie zbiera liście (is_leaf=true) pasujące do `needle` w poddrzewie.

    Args:
        tree_by_parent: Kategorie zgrupowane po `parent_id`.
        parent_id: Korzeń poddrzewa do przeszukania.
        needle: Znormalizowany (lowercase, przycięty) fragment nazwy.
        depth: Bieżąca głębokość rekurencji - patrz `_MAX_CATEGORY_SEARCH_DEPTH`.

    Returns:
        Kategorie-liście z poddrzewa `parent_id`, których nazwa zawiera
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
    """Rekurencyjnie przeszukuje całe drzewo kategorii OLX pod kątem liści.

    Pozwala znaleźć docelową kategorię (np. "Gry i konsole") bez ręcznego
    klikania po drzewie poziom po poziomie - tylko liście (is_leaf=true) są
    zwracane, bo tylko w nich można wystawić ogłoszenie. Działa na
    cache'owanym drzewie (`_fetch_category_tree`) - realne wywołanie OLX
    padnie WYŁĄCZNIE przy pierwszym użyciu w danym procesie.

    Args:
        session: Sesja bazy danych.
        q: Fragment nazwy do wyszukania (case-insensitive).

    Returns:
        Kategorie-liście, których nazwa zawiera `q`.

    Raises:
        OlxAuthError: Gdy brak ważnej autoryzacji OLX.
        OlxApiError: Gdy wywołanie OLX się nie powiedzie.
    """
    tree = await _fetch_category_tree(session)
    by_parent: dict[int, list[dict[str, Any]]] = {}
    for category in tree:
        by_parent.setdefault(category.get("parent_id", 0), []).append(category)
    needle = q.strip().lower()
    return _collect_leaf_matches(by_parent, 0, needle, depth=0)


def _compact_attribute(raw: dict[str, Any]) -> dict[str, Any]:
    """Redukuje surowy rekord atrybutu kategorii OLX do pól istotnych dla klienta.

    Kształt zweryfikowany empirycznie (GET /categories/2273/attributes):
    wymagalność jest pod kluczem "validation.required", ale dozwolone
    wartości są na NAJWYŻSZYM poziomie rekordu ("values", nie
    "validation.values"), a każda z nich to `{"code": ..., "label": ...}` -
    "code" jest wartością do wysłania w payloadzie ogłoszenia (np. atrybut
    "type" kategorii Xbox ma wartość "code": "xbox360", "label": "Xbox 360").

    Args:
        raw: Surowy rekord atrybutu z odpowiedzi OLX.

    Returns:
        Słownik z kluczami: code, label, required, values (lista
        `{"code", "label"}` dozwolonych wartości - pusta dla atrybutów
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

    Potrzebne do ustalenia, jak przekazać w payloadzie ogłoszenia cechy,
    których OLX NIE wyraża przez osobną kategorię (patrz migracja
    0005_olx_category_mapping.sql - jedna kategoria "Gry" na producenta, bez
    rozróżnienia generacji konsoli) - np. konkretna konsola w obrębie
    kategorii "Xbox" (2273), obok już znanego atrybutu stanu
    (`CONDITION_ATTRIBUTE_CODE`).

    Args:
        session: Sesja bazy danych.
        category_id: Id kategorii OLX (docelowo liść drzewa - patrz
            `search_leaf_categories`).

    Returns:
        Atrybuty w zwięzłym kształcie (`_compact_attribute`): code, label,
        required, values.

    Raises:
        OlxAuthError: Gdy brak ważnej autoryzacji OLX.
        OlxApiError: Gdy wywołanie OLX się nie powiedzie.
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
    UUID może NIE działać dla 2272 (PlayStation) czy 2274 (Nintendo), więc
    zamiast zakładać wspólną wartość, funkcja pobiera atrybuty WŁAŚCIWEJ
    kategorii (`fetch_category_attributes`) i dopasowuje po fragmencie
    etykiety (label), nie po stałym UUID - "InPost Paczkomat" w treści
    etykiety, kończącej się rozmiarem "S" (odróżniając od "...24/7 M"/"L").

    Args:
        session: Sesja bazy danych.
        category_id: Id kategorii OLX, w której publikowane jest ogłoszenie
            (`platform.olx_category_id`).

    Returns:
        Kod (UUID) wartości atrybutu "delivery" pasującej etykiecie, albo
        None, gdy kategoria nie ma atrybutu "delivery" albo żadna jego
        wartość nie pasuje - wywołujący (`build_advert_payload`) ma wtedy
        pominąć "ad_delivery" w payloadzie, nie rzucać błędem (to pole
        opcjonalne).

    Raises:
        OlxAuthError: Gdy brak ważnej autoryzacji OLX.
        OlxApiError: Gdy wywołanie OLX się nie powiedzie.
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


# Pola miasta istotne dla klienta (do wyboru city_id) - OLX zwraca też
# municipality/latitude/longitude, odrzucane przez `_compact_city`.
_CITY_FIELDS = ("id", "name", "county", "region_id")

# GET /cities NIE zwraca całej Polski w jednej odpowiedzi (dziesiątki
# tysięcy miejscowości) - domyślnie ucina po ~1000 rekordów, BEZ żadnych
# metadanych stronicowania w ciele ani nagłówkach (zweryfikowane empirycznie
# - brak "links"/"meta"/page-info, tylko {"data": [...]}). OLX akceptuje za
# to `limit`/`offset` w query stringu; 10000 to potwierdzony maksymalny
# `limit` (powyżej OLX odrzuca żądanie 400-ką "This value should be between
# 0 and 10000").
_CITY_PAGE_LIMIT = 10000
# Zabezpieczenie `_fetch_city_list` przed nieskończoną pętlą, gdyby OLX
# przestał kiedyś zwracać stronę krótszą niż `_CITY_PAGE_LIMIT` (sygnał
# końca danych, na którym opiera się pętla). 20 stron * 10000 = 200k miast -
# dzisiejszy pełny zbiór to ok. 53 tys., więc to margines, nie realny limit.
_MAX_CITY_PAGES = 20

# Cache pełnej (przefiltrowanej i spłaszczonej) listy miast OLX w pamięci
# procesu - miasta praktycznie się nie zmieniają, a pełne pobranie to od
# razu kilka żądań do OLX (limit 4500 żądań/5 min), więc robimy to raz na
# proces, tak samo jak `_category_tree_cache`.
_city_list_cache: list[dict[str, Any]] | None = None


def _compact_city(raw: dict[str, Any]) -> dict[str, Any]:
    """Redukuje surowy rekord miasta OLX do pól istotnych dla klienta.

    Args:
        raw: Surowy rekord miasta z odpowiedzi OLX.

    Returns:
        Słownik z wyłącznie kluczami `_CITY_FIELDS`.
    """
    return {field: raw.get(field) for field in _CITY_FIELDS}


def _normalize_search_text(value: str) -> str:
    """Sprowadza tekst do porównywalnej postaci: małe litery, bez diakrytyków.

    Pozwala "krakow"/"krak" znaleźć "Kraków" - `unicodedata` dekomponuje
    większość polskich znaków diakrytycznych (np. "ó" z ogonkiem -> "o" +
    znak kombinujący, który potem odrzucamy), ale NIE "ł" przekreślone
    ("Łódź") - to jedyny polski znak bez takiej dekompozycji, więc jest
    tłumaczony ręcznie PRZED normalizacją.

    Args:
        value: Tekst wejściowy (np. nazwa miasta albo fraza wyszukiwania).

    Returns:
        Tekst mały-literowy, bez znaków diakrytycznych.
    """
    value = value.replace("ł", "l").replace("Ł", "L")
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


async def _fetch_city_list(session: AsyncSession) -> list[dict[str, Any]]:
    """Pobiera (i cache'uje w pamięci procesu) całą listę miast OLX.

    Pętla stronicuje przez `limit`/`offset` (patrz komentarz przy
    `_CITY_PAGE_LIMIT`) dopóki OLX nie zwróci strony krótszej niż
    `_CITY_PAGE_LIMIT` - to sygnał końca danych. Wynik pierwszego wywołania
    jest trzymany w `_city_list_cache` i ponownie używany przez każde
    kolejne wyszukiwanie (`fetch_cities`) w tym samym procesie.

    Args:
        session: Sesja bazy danych (do zdobycia access tokenu przy
            pierwszym, niecache'owanym wywołaniu).

    Returns:
        Płaska lista miast w zwięzłym kształcie (`_compact_city`).

    Raises:
        OlxAuthError: Gdy brak ważnej autoryzacji OLX (tylko przy pierwszym,
            niecache'owanym wywołaniu).
        OlxApiError: Gdy wywołanie OLX się nie powiedzie (tylko przy
            pierwszym, niecache'owanym wywołaniu).
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

    OLX ignoruje wyszukiwanie tekstowe w żądaniu do /cities, więc
    filtrowanie robimy lokalnie na pobranej (i cache'owanej - patrz
    `_fetch_city_list`) liście, bez uwzględniania wielkości liter i znaków
    diakrytycznych (`_normalize_search_text`) - dzięki temu "krakow" i
    "krak" znajdują "Kraków". Trafienia zaczynające się od `q` są
    sortowane jako pierwsze (stabilnie, więc kolejność OLX w obrębie każdej
    z dwóch grup jest zachowana).

    Args:
        session: Sesja bazy danych.
        q: Fragment nazwy do wyszukania, albo None dla pełnej listy.

    Returns:
        Miasta pasujące do `q` (albo wszystkie, gdy `q` jest puste).

    Raises:
        OlxAuthError: Gdy brak ważnej autoryzacji OLX.
        OlxApiError: Gdy wywołanie OLX się nie powiedzie.
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
    """Redukuje surowy rekord dzielnicy OLX do pól istotnych dla klienta.

    Args:
        raw: Surowy rekord dzielnicy z odpowiedzi OLX.

    Returns:
        Słownik z wyłącznie kluczami `_DISTRICT_FIELDS`.
    """
    return {field: raw.get(field) for field in _DISTRICT_FIELDS}


async def fetch_districts(session: AsyncSession, city_id: int) -> list[dict[str, Any]]:
    """Pobiera dzielnice danego miasta OLX - do ustalenia district_id.

    Używa GET /cities/{city_id}/districts, NIE GET /cities/{city_id} - ten
    drugi zwraca ten sam płaski rekord miasta co /cities (id/name/county/
    region_id/...), bez żadnego pola z dzielnicami (zweryfikowane
    empirycznie). GET /districts (bez city_id w ścieżce) też istnieje, ale
    zwraca NIEFILTROWANĄ listę WSZYSTKICH dzielnic w Polsce - `city_id` jako
    query param jest ignorowany, tak samo jak wyszukiwanie tekstowe przy
    /categories i /cities (patrz `fetch_categories`/`fetch_cities`).

    Małe miejscowości nie mają podziału na dzielnice - OLX zwraca wtedy
    pustą listę (zweryfikowane empirycznie), nie błąd.

    Args:
        session: Sesja bazy danych.
        city_id: Id miasta OLX (z /api/olx/cities).

    Returns:
        Dzielnice miasta w zwięzłym kształcie (`_compact_district`): id,
        name. Pusta lista, gdy miasto nie ma podziału na dzielnice.

    Raises:
        OlxAuthError: Gdy brak ważnej autoryzacji OLX.
        OlxApiError: Gdy wywołanie OLX się nie powiedzie.
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
    """Buduje tytuł ogłoszenia wg sprawdzonego szablonu sklepu.

    Gdy pełny tytuł przekracza MAX_TITLE_LENGTH, kolejno odrzucane są
    OPCJONALNE segmenty końcówki - najpierw " | Wymiana", potem także
    " | Wysyłka" - NIGDY tytuł gry ani nazwa platformy: to jedyne segmenty
    niosące realną informację o ofercie, więc obcięcie któregoś w połowie
    (albo w całości) zniweczyłoby sens ogłoszenia. Jeśli nawet bez obu
    opcjonalnych segmentów tytuł się nie mieści (skrajnie długi tytuł gry),
    to przypadek do ręcznej korekty w poczekalni, nie do automatycznego
    obcinania - stąd błąd zamiast dalszego skracania.

    Args:
        game_title: Tytuł gry.
        platform_generation: Etykieta generacji platformy (platform.generation,
            np. "PS4/PS5", "Xbox 360"), albo zamiennik opisowy, gdy platforma
            jest "other".

    Returns:
        Tytuł gotowy do wysłania w payloadzie OLX - pełny, albo (gdy za
        długi) bez " | Wymiana", albo (gdy nadal za długi) bez
        " | Wymiana" i " | Wysyłka". Tytuł gry i platforma są zawsze w
        całości.

    Raises:
        OlxValidationError: Gdy tytuł przekracza MAX_TITLE_LENGTH znaków
            NAWET bez obu opcjonalnych segmentów - lepiej to wykryć przed
            wysłaniem niż dostać 4xx z OLX (bez środowiska testowego każda
            próba się liczy).
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
        f"Tytuł ogłoszenia ma {len(base)} znaków (limit {MAX_TITLE_LENGTH}) "
        f'nawet bez segmentów " | Wysyłka" i " | Wymiana": {base!r}. Tytuł '
        "gry i platforma nigdy nie są ucinane - skróć ręcznie tytuł gry w "
        "poczekalni i spróbuj ponownie."
    )


def build_description(
    *,
    manufacturer: str,
    console_name: str,
    game_title: str,
    condition: Literal["new", "used"],
) -> str:
    """Buduje opis ogłoszenia wg sprawdzonego szablonu sklepu.

    Args:
        manufacturer: Kod producenta platformy (platform.manufacturer:
            "sony"/"microsoft"/"nintendo"/"other").
        console_name: Nazwa konsoli do opisu (np. "PlayStation 4").
        game_title: Tytuł gry.
        condition: Stan egzemplarza.

    Returns:
        Pełny opis ogłoszenia po polsku.
    """
    manufacturer_pl = _MANUFACTURER_PL.get(manufacturer, manufacturer.capitalize())
    condition_pl = _CONDITION_PL[condition]
    # Złożone z krótkich linii źródłowych (limit 88 znaków), ale KAŻDY akapit
    # to jedna logiczna linia treści ogłoszenia - stąd "\n\n".join, nie
    # wieloliniowy f-string (ten psułby się na długich akapitach poniżej).
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
    """Buduje payload ogłoszenia zgodny z OLX Partner API (POST /adverts).

    OLX pobiera zdjęcia SAM ze wskazanych URL-i - nie przyjmuje binarnego
    uploadu. `image_urls` muszą więc być już publicznie dostępne (R2), co
    zapewnia `zibicom.photos.upload_photo` przed wywołaniem tej funkcji.

    `advertiser_type` i `contact.name` są wymagane przez OLX (bez nich
    create_advert dostaje 400) - konto zibicom jest firmowe, więc pierwsze
    jest stałą wartością "business"; nazwa kontaktowa pochodzi z
    konfiguracji (`Settings.olx_contact_name`), nie jest stała tutaj.

    NIE ma tu "auto_extend_enabled" ANI "ad_delivery", mimo że oba są
    widoczne w odczycie ogłoszenia (GET /adverts/{id}) - POST /adverts
    odrzuca oba (pierwsze 400-ką "Ten formularz nie powinien zawierać
    dodatkowych pól", drugie pustym 400 "Data validation error occurred",
    ustalonym przez porównanie udanego payloadu bez "ad_delivery" z
    odrzuconym, który je miał - wszystkie inne pola były identyczne). Obecność
    pola w odczycie NIE oznacza, że jest akceptowane przy zapisie - to
    najwyraźniej stan wynikowy ustawiany przez OLX po stronie serwera, nie
    pole wejściowe. `resolve_delivery_attribute` zostaje w kodzie (przyda
    się, gdy ustalimy właściwy sposób ustawiania dostawy - prawdopodobnie
    osobne wywołanie API, nie pole payloadu tworzenia), ale NIE jest tu
    wywoływane.

    Args:
        title: Tytuł (zibicom.olx.build_title).
        description: Opis (zibicom.olx.build_description).
        category_id: Id kategorii OLX (z konfiguracji albo /api/olx/categories).
        city_id: Id miasta OLX (z konfiguracji albo /api/olx/cities).
        district_id: Id dzielnicy OLX (z konfiguracji albo
            /api/olx/cities/{city_id}/districts), albo 0, gdy nieustawiona -
            wtedy pomijana w payloadzie. OLX wymaga jej TYLKO dla miast z
            podziałem na dzielnice (np. Kraków) - wysłanie jej dla małej
            miejscowości bez dzielnic psuje publikację, więc NIE jest
            dołączana bezwarunkowo tak jak city_id.
        price_pln: Cena w PLN.
        condition: Stan egzemplarza.
        platform_olx_attribute_value: Wartość atrybutu platformy
            (`PLATFORM_ATTRIBUTE_CODE`, `platform.olx_attribute_value`),
            albo None, gdy nieustawiona w słowniku - wtedy atrybut platformy
            jest pomijany w payloadzie (w odróżnieniu od atrybutu stanu,
            który OLX wymaga zawsze).
        image_urls: Publiczne URL-e zdjęć (R2), maks. MAX_IMAGES.
        contact_name: Nazwa kontaktowa wyświetlana w ogłoszeniu
            (Settings.olx_contact_name).

    Returns:
        Payload gotowy do wysłania w treści POST /adverts.

    Raises:
        OlxValidationError: Gdy zdjęć jest więcej niż MAX_IMAGES.
    """
    if len(image_urls) > MAX_IMAGES:
        raise OlxValidationError(
            f"OLX przyjmuje maksymalnie {MAX_IMAGES} zdjęć, "
            f"otrzymano {len(image_urls)}."
        )

    # "state" mapuje się 1:1 na nasz enum listing_condition ("new"/"used") -
    # to są dokładnie kody, których oczekuje OLX (zweryfikowane empirycznie,
    # patrz komentarz przy CONDITION_ATTRIBUTE_CODE), żaden słownik nie jest
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
    """Publikuje ogłoszenie na OLX (POST /adverts) i loguje wywołanie w olx_operation.

    Przyjmuje już zdobyty `access_token` (zamiast wywoływać
    `get_access_token` samodzielnie) - patrz docstring `get_access_token` o
    tym, dlaczego wywołujący musi zdobyć token PRZED otwarciem większej
    transakcji. Ta funkcja sama NIE commituje - jest częścią transakcji
    publikacji pozycji poczekalni (`zibicom.intake.publish_item`), która ma
    zatwierdzić się w całości na koniec, dopiero po sukcesie OLX.

    Ogłoszenie po publikacji przechodzi moderację OLX (status
    new -> waiting -> active) - status w odpowiedzi NIE oznacza, że
    ogłoszenie jest już widoczne.

    Args:
        session: Sesja bazy danych (ta sama, w której trwa transakcja
            publikacji).
        payload: Payload ogłoszenia (zibicom.olx.build_advert_payload).
        access_token: Ważny access token, zdobyty przez `get_access_token`.
        listing_id: Id oferty, do której przypisać wpis audytowy.

    Returns:
        Odpowiedź OLX (zawiera co najmniej pole "id" utworzonego ogłoszenia
        i jego "status" moderacji).

    Raises:
        OlxApiError: Gdy wywołanie sieciowe albo odpowiedź OLX wskazuje na
            błąd - transakcja publikacji jest wtedy wycofywana przez
            wywołującego, więc w bazie NIE zostaje oferta bez odpowiednika
            na OLX ani (przy błędzie samego wywołania) ogłoszenie na OLX bez
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
            f"Wywołanie OLX create_advert nie powiodło się: {exc}"
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
            f"OLX zwróciło błąd {response.status_code} przy tworzeniu "
            f"ogłoszenia: {error_detail}"
        )
    return data


_ADVERT_FIELDS = ("id", "status", "activated_at", "valid_to")


def _compact_advert(raw: dict[str, Any]) -> dict[str, Any]:
    """Redukuje surowy rekord ogłoszenia OLX do pól istotnych dla klienta.

    Args:
        raw: Surowy rekord ogłoszenia z odpowiedzi OLX.

    Returns:
        Słownik z wyłącznie kluczami `_ADVERT_FIELDS`.
    """
    return {field: raw.get(field) for field in _ADVERT_FIELDS}


async def fetch_advert(session: AsyncSession, advert_id: int) -> dict[str, Any]:
    """Pobiera bieżący stan ogłoszenia OLX (GET /adverts/{id}).

    Status ogłoszenia MOŻE zmienić się po naszej stronie bez naszego udziału
    (moderacja, wygaśnięcie po `valid_to`, zdjęcie przez OLX) - status
    zapisany przy publikacji (`create_advert`) jest więc tylko migawką z
    tamtej chwili. Zweryfikowane empirycznie: zaraz po utworzeniu OLX
    zwrócił status "disabled", a kilka minut później to samo ogłoszenie
    miało już "active" (`activated_at` było ustawione już przy tworzeniu,
    mimo statusu "disabled" - nie jest więc wiarygodnym sygnałem samym w
    sobie). Ta funkcja służy do ODŚWIEŻENIA tego stanu na żądanie - patrz
    `zibicom.intake.sync_advert_status`.

    Args:
        session: Sesja bazy danych.
        advert_id: Id ogłoszenia OLX (listing.olx_advert_id).

    Returns:
        Zwięzły stan ogłoszenia (`_compact_advert`): id, status,
        activated_at, valid_to.

    Raises:
        OlxAuthError: Gdy brak ważnej autoryzacji OLX.
        OlxApiError: Gdy wywołanie OLX się nie powiedzie.
    """
    token = await get_access_token(session)
    settings = get_settings()
    response = await _partner_http_client().get(
        f"{settings.olx_api_base_url}{_ADVERTS_PATH}/{advert_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        body = response.json()
    except ValueError:
        body = None
    data = _unwrap_data(body)
    if response.status_code >= 400 or not isinstance(data, dict):
        raise OlxApiError(
            f"OLX zwróciło nieoczekiwaną odpowiedź przy fetch_advert "
            f"({response.status_code}): {body!r}"
        )
    return _compact_advert(data)
