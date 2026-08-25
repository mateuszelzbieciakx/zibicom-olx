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

# Kody atrybutow OLX (per kategoria) - NIE sa czescia zweryfikowanej
# dokumentacji dostarczonej przy tym zadaniu. OLX przypisuje atrybuty
# indywidualnie per kategoria, wiec przed pierwsza prawdziwa publikacja
# trzeba je potwierdzic dla wybranej kategorii (olx_category_id). Bledny kod
# konczy sie 4xx z OLX (zalogowanym w olx_operation przez create_advert) -
# NIE tworzy ogloszenia, wiec pomylka tutaj jest bezpieczna do naprawienia.
CONDITION_ATTRIBUTE_CODE = "state"
PLATFORM_ATTRIBUTE_CODE = "platform"

_CONDITION_ATTRIBUTE_VALUE = {"new": "Nowe", "used": "Używane"}
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


@lru_cache
def _http_client() -> httpx.AsyncClient:
    """Buduje (raz na proces) asynchronicznego klienta HTTP do OLX API.

    Tworzenie nowego klienta przy kazdym wywolaniu prowadzi do tego samego
    bledu, co przy kliencie Gemini (zibicom.vision._client) - GC zamyka
    porzucony transport i kolejne wywolania w tym samym procesie dostaja
    "Cannot send a request, as the client has been closed". `lru_cache` bez
    argumentow trzyma jedna, wspoldzielona instancje przez caly czas zycia
    procesu.

    Returns:
        Asynchroniczny klient httpx z rozsadnym timeoutem.
    """
    return httpx.AsyncClient(timeout=30.0)


async def dispose_http_client() -> None:
    """Zamyka wspoldzielonego klienta HTTP (wywolywane przy zamykaniu aplikacji)."""
    if _http_client.cache_info().currsize:
        await _http_client().aclose()
    _http_client.cache_clear()


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
    await _log_operation(
        session,
        listing_id=None,
        operation=operation,
        request_payload=request_log,
        response_payload=_redact(body) if isinstance(body, dict) else None,
        http_status=response.status_code,
        succeeded=succeeded,
        olx_error=None if succeeded else str(body),
    )

    if not succeeded:
        raise OlxApiError(
            f"OLX zwrocilo blad {response.status_code} przy {operation}: {body}"
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


def _parse_list_response(
    response: httpx.Response, *, operation: str
) -> list[dict[str, Any]]:
    """Parsuje odpowiedz JSON, ktora ma byc tablica obiektow.

    Args:
        response: Surowa odpowiedz httpx.
        operation: Nazwa operacji, do czytelnego komunikatu bledu.

    Returns:
        Sparsowana lista obiektow.

    Raises:
        OlxApiError: Gdy status jest bledem albo cialo nie jest tablica.
    """
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400 or not isinstance(body, list):
        raise OlxApiError(
            f"OLX zwrocilo nieoczekiwana odpowiedz przy {operation} "
            f"({response.status_code}): {body!r}"
        )
    return body


async def fetch_categories(
    session: AsyncSession, q: str | None = None
) -> list[dict[str, Any]]:
    """Wyszukuje kategorie OLX po nazwie - pomocnicze, do ustalenia category_id.

    Args:
        session: Sesja bazy danych.
        q: Fragment nazwy do wyszukania (case-insensitive), albo None dla
            pelnej listy.

    Returns:
        Kategorie pasujace do `q` (albo wszystkie, gdy `q` jest puste).

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX.
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie.
    """
    token = await get_access_token(session)
    settings = get_settings()
    response = await _http_client().get(
        f"{settings.olx_api_base_url}{_CATEGORIES_PATH}",
        headers={"Authorization": f"Bearer {token}"},
    )
    categories = _parse_list_response(response, operation="fetch_categories")
    if not q:
        return categories
    needle = q.strip().lower()
    return [c for c in categories if needle in str(c.get("name", "")).lower()]


async def fetch_cities(
    session: AsyncSession, q: str | None = None
) -> list[dict[str, Any]]:
    """Wyszukuje miasta OLX po nazwie - pomocnicze, do ustalenia city_id.

    Args:
        session: Sesja bazy danych.
        q: Fragment nazwy do wyszukania (case-insensitive), albo None dla
            pelnej listy.

    Returns:
        Miasta pasujace do `q` (albo wszystkie, gdy `q` jest puste).

    Raises:
        OlxAuthError: Gdy brak waznej autoryzacji OLX.
        OlxApiError: Gdy wywolanie OLX sie nie powiedzie.
    """
    token = await get_access_token(session)
    settings = get_settings()
    response = await _http_client().get(
        f"{settings.olx_api_base_url}{_CITIES_PATH}",
        headers={"Authorization": f"Bearer {token}"},
    )
    cities = _parse_list_response(response, operation="fetch_cities")
    if not q:
        return cities
    needle = q.strip().lower()
    return [c for c in cities if needle in str(c.get("name", "")).lower()]


def build_title(game_title: str, platform_generation: str) -> str:
    """Buduje tytul ogloszenia wg sprawdzonego szablonu sklepu.

    Args:
        game_title: Tytul gry.
        platform_generation: Etykieta generacji platformy (platform.generation,
            np. "PS4/PS5", "Xbox 360"), albo zamiennik opisowy, gdy platforma
            jest "other".

    Returns:
        Tytul gotowy do wyslania w payloadzie OLX.

    Raises:
        OlxValidationError: Gdy zlozony tytul przekracza MAX_TITLE_LENGTH
            znakow - lepiej to wykryc przed wyslaniem niz dostac 4xx z OLX
            (bez srodowiska testowego kazda proba sie liczy).
    """
    title = f"{game_title} | {platform_generation} | Sklep | Kraków | Wysyłka | Wymiana"
    if len(title) > MAX_TITLE_LENGTH:
        raise OlxValidationError(
            f"Tytul ogloszenia ma {len(title)} znakow (limit {MAX_TITLE_LENGTH}): "
            f"{title!r}. Skroc tytul gry i sprobuj ponownie."
        )
    return title


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
    price_pln: Decimal,
    condition: Literal["new", "used"],
    platform_olx_attribute_value: str | None,
    image_urls: Sequence[str],
) -> dict[str, Any]:
    """Buduje payload ogloszenia zgodny z OLX Partner API (POST /adverts).

    OLX pobiera zdjecia SAM ze wskazanych URL-i - nie przyjmuje binarnego
    uploadu. `image_urls` musza wiec byc juz publicznie dostepne (R2), co
    zapewnia `zibicom.photos.upload_photo` przed wywolaniem tej funkcji.

    Args:
        title: Tytul (zibicom.olx.build_title).
        description: Opis (zibicom.olx.build_description).
        category_id: Id kategorii OLX (z konfiguracji albo /api/olx/categories).
        city_id: Id miasta OLX (z konfiguracji albo /api/olx/cities).
        price_pln: Cena w PLN.
        condition: Stan egzemplarza.
        platform_olx_attribute_value: Wartosc atrybutu platformy
            (platform.olx_attribute_value), albo None, gdy nieustawiona w
            slowniku - wtedy atrybut platformy jest pomijany w payloadzie.
        image_urls: Publiczne URL-e zdjec (R2), maks. MAX_IMAGES.

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

    attributes = [
        {
            "code": CONDITION_ATTRIBUTE_CODE,
            "value": _CONDITION_ATTRIBUTE_VALUE[condition],
        }
    ]
    if platform_olx_attribute_value:
        attributes.append(
            {"code": PLATFORM_ATTRIBUTE_CODE, "value": platform_olx_attribute_value}
        )

    return {
        "title": title,
        "description": description,
        "category_id": category_id,
        "location": {"city_id": city_id},
        "price": {"value": float(price_pln), "currency": "PLN"},
        "images": [{"url": url} for url in image_urls],
        "attributes": attributes,
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
        response = await _http_client().post(
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

    succeeded = response.status_code < 400 and isinstance(body, dict)
    await _log_operation(
        session,
        listing_id=listing_id,
        operation="create_advert",
        request_payload=request_log,
        response_payload=body if isinstance(body, dict) else None,
        http_status=response.status_code,
        succeeded=succeeded,
        olx_error=None if succeeded else str(body),
    )

    if not succeeded:
        raise OlxApiError(
            f"OLX zwrocilo blad {response.status_code} przy tworzeniu "
            f"ogloszenia: {body}"
        )
    return body
