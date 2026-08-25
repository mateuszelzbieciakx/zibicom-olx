"""Testy integracji OLX.

HTTP jest w pelni zamockowany (zero ruchu sieciowego - OLX nie ma
srodowiska testowego, wiec kazde prawdziwe wywolanie byloby realna
publikacja). `get_access_token`/`exchange_code`/`create_advert` korzystaja z
prawdziwej bazy testowej (docker-compose) - liczy sie realny UPSERT
singletonu, szyfrowanie w kolumnach BYTEA i logowanie do olx_operation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from zibicom import crypto, olx
from zibicom.config import Settings

FAKE_TOKEN_KEY = Fernet.generate_key().decode("ascii")

FAKE_SETTINGS = Settings(
    _env_file=None,  # type: ignore[call-arg]
    olx_client_id="test-client-id",
    olx_client_secret="test-client-secret",
    olx_redirect_uri="https://cdn.example.test/callback",
    olx_auth_base_url="https://auth.example.test",
    olx_api_base_url="https://api.example.test/partner",
    token_encryption_key=FAKE_TOKEN_KEY,
)


@pytest.fixture(autouse=True)
def _fake_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Odcina olx.py/crypto.py od prawdziwych sekretow dewelopera."""
    monkeypatch.setattr(olx, "get_settings", lambda: FAKE_SETTINGS)
    monkeypatch.setattr(crypto, "get_settings", lambda: FAKE_SETTINGS)
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


@pytest.fixture(autouse=True)
def _clear_http_client_cache() -> Iterator[None]:
    olx._http_client.cache_clear()
    olx._partner_http_client.cache_clear()
    yield
    olx._http_client.cache_clear()
    olx._partner_http_client.cache_clear()


@pytest.fixture(autouse=True)
def _clear_category_tree_cache() -> Iterator[None]:
    """Zeruje cache drzewa kategorii (`olx._category_tree_cache`) miedzy testami.

    Bez tego wynik jednego testu (np. zamockowana odpowiedz z falszywymi
    kategoriami) przecieklby jako "prawdziwy" cache do kolejnych testow w tym
    samym procesie pytest.
    """
    olx._category_tree_cache = None
    yield
    olx._category_tree_cache = None


async def _insert_valid_token(session: AsyncSession) -> None:
    """Wstawia wazny (nie wygasly) token OLX.

    Warunek wstepny fetch_categories/search_leaf_categories/fetch_cities,
    ktore wywoluja `get_access_token`.
    """
    await session.execute(
        text(
            "INSERT INTO olx_token "
            "(id, access_token_encrypted, refresh_token_encrypted, "
            " access_expires_at, scope) "
            "VALUES (1, :access, :refresh, :expires_at, 'v2 read write')"
        ),
        {
            "access": crypto.encrypt("AT-valid"),
            "refresh": crypto.encrypt("RT-valid"),
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        },
    )
    await session.commit()


def _response(status_code: int, body: dict | None = None) -> MagicMock:
    """Buduje falszywa odpowiedz httpx.Response o zadanym statusie i ciele.

    `.text` jest ustawiane obok `.json()` - `_error_detail` czyta `.text`
    (nie sparsowany JSON), wiec bez tego mockowe wywolania bledow dostalyby
    MagicMock zamiast str przy zapisie do olx_operation.olx_error.
    """
    response = MagicMock()
    response.status_code = status_code
    if body is None:
        response.json.side_effect = ValueError("brak ciala odpowiedzi")
        response.text = ""
    else:
        response.json.return_value = body
        response.text = json.dumps(body)
    return response


# --------------------------------------------------------------------------
# build_authorize_url / build_title / build_description / build_advert_payload
# (czyste funkcje - zero sieci, zero bazy)
# --------------------------------------------------------------------------


def test_build_authorize_url_zawiera_wymagane_parametry() -> None:
    url = olx.build_authorize_url()

    assert url.startswith("https://auth.example.test/oauth/authorize/?")
    assert "client_id=test-client-id" in url
    assert "response_type=code" in url
    assert "scope=v2+read+write" in url
    assert "redirect_uri=https%3A%2F%2Fcdn.example.test%2Fcallback" in url


def test_build_title_wg_sprawdzonego_szablonu() -> None:
    title = olx.build_title("Bloodborne", "PS4/PS5")

    assert title == "Bloodborne | PS4/PS5 | Sklep | Kraków | Wysyłka | Wymiana"


def test_build_title_za_dlugi_rzuca_blad_walidacji() -> None:
    with pytest.raises(olx.OlxValidationError, match="70"):
        olx.build_title("A" * 60, "PS4/PS5")


def test_build_description_zawiera_wszystkie_sekcje_szablonu() -> None:
    description = olx.build_description(
        manufacturer="sony",
        console_name="PlayStation 4",
        game_title="Bloodborne",
        condition="used",
    )

    assert description.startswith(
        "Sklep ZibiCom zaprasza do zakupu gry na konsole Sony PlayStation 4 "
        "- Bloodborne"
    )
    assert "Gra jest używana." in description
    assert "Ulicy Wlotowej 2a" in description
    assert "PROSIMY O KONTAKT PRZED ZŁOŻENIEM ZAMÓWIENIA" in description
    assert "Nintendo Switch 1 i 2" in description
    assert description.endswith("prosimy o kontakt.")


def test_build_description_stan_nowa_i_inny_producent() -> None:
    description = olx.build_description(
        manufacturer="microsoft",
        console_name="Xbox 360",
        game_title="Halo 3",
        condition="new",
    )

    assert "Microsoft Xbox 360 - Halo 3" in description
    assert "Gra jest nowa." in description


def test_build_advert_payload_podstawowe_pola() -> None:
    payload = olx.build_advert_payload(
        title="Tytul",
        description="Opis",
        category_id=123,
        city_id=456,
        price_pln=Decimal("99.99"),
        condition="used",
        platform_olx_attribute_value="PlayStation 4",
        image_urls=["https://cdn.example.test/1.jpg", "https://cdn.example.test/2.jpg"],
    )

    assert payload["title"] == "Tytul"
    assert payload["category_id"] == 123
    assert payload["location"] == {"city_id": 456}
    assert payload["price"] == {"value": 99.99, "currency": "PLN"}
    assert payload["images"] == [
        {"url": "https://cdn.example.test/1.jpg"},
        {"url": "https://cdn.example.test/2.jpg"},
    ]
    assert {"code": "state", "value": "Używane"} in payload["attributes"]
    assert {"code": "platform", "value": "PlayStation 4"} in payload["attributes"]


def test_build_advert_payload_bez_platformy_pomija_atrybut_platformy() -> None:
    payload = olx.build_advert_payload(
        title="Tytul",
        description="Opis",
        category_id=1,
        city_id=1,
        price_pln=Decimal("10"),
        condition="new",
        platform_olx_attribute_value=None,
        image_urls=[],
    )

    codes = [attribute["code"] for attribute in payload["attributes"]]
    assert "platform" not in codes
    assert payload["images"] == []


def test_build_advert_payload_za_duzo_zdjec_rzuca_blad_walidacji() -> None:
    with pytest.raises(olx.OlxValidationError, match="8"):
        olx.build_advert_payload(
            title="T",
            description="D",
            category_id=1,
            city_id=1,
            price_pln=Decimal("10"),
            condition="used",
            platform_olx_attribute_value=None,
            image_urls=[f"https://cdn.example.test/{i}.jpg" for i in range(9)],
        )


# --------------------------------------------------------------------------
# _http_client / _error_detail (regresja blokady WAF/CloudFront)
# --------------------------------------------------------------------------


def test_http_client_ma_niestandardowy_user_agent() -> None:
    """Sprawdza, ze klient OLX nie uzywa domyslnego User-Agenta httpx.

    Domyslny User-Agent httpx ("python-httpx/...") jest przez WAF/CloudFront
    OLX traktowany jako bot i blokowany 403-ka - regresja, ktora kosztowala
    sporo czasu na diagnoze. Ten test pilnuje, zeby klient zawsze wysylal
    wlasny User-Agent (i sensowne Accept/Content-Type).
    """
    client = olx._http_client()

    assert client.headers["User-Agent"] == "zibicom-olx/0.1"
    assert client.headers["User-Agent"] != httpx.Client().headers["User-Agent"]
    assert client.headers["Accept"] == "application/json"
    assert client.headers["Content-Type"] == "application/json"


def test_partner_http_client_ma_naglowek_version() -> None:
    """Sprawdza, ze klient Partner API zawsze wysyla naglowek Version.

    Partner API (kategorie/miasta/adverts) odrzuca zadania bez naglowka
    Version 400-ka "Missing required 'Version' header!". Klient uzywany do
    tych wywolan musi go wiec zawsze wysylac, obok istniejacych naglowkow
    (User-Agent/Accept/Content-Type) chroniacych przed blokada WAF/CloudFront.
    """
    client = olx._partner_http_client()

    assert client.headers["Version"] == "2.0"
    assert client.headers["User-Agent"] == "zibicom-olx/0.1"
    assert client.headers["Accept"] == "application/json"
    assert client.headers["Content-Type"] == "application/json"


def test_http_client_oauth_bez_naglowka_version() -> None:
    """Sprawdza, ze klient OAuth nie niesie naglowka Version.

    Endpoint OAuth (/api/open/oauth/token) NIE jest czescia Partner API i
    dziala poprawnie bez naglowka Version - dopisanie go tutaj byloby
    niepotrzebne (i ryzykowne, gdyby OLX kiedys zaczal go tam walidowac
    inaczej niz w Partner API), wiec klient OAuth ma go NIE miec.
    """
    client = olx._http_client()

    assert "Version" not in client.headers


def test_error_detail_cialo_puste_opisuje_blokade_waf() -> None:
    response = _response(403, body=None)

    assert olx._error_detail(response) == (
        "pusta odpowiedz — prawdopodobnie blokada WAF/CloudFront"
    )


def test_error_detail_przycina_dlugie_cialo_do_500_znakow() -> None:
    response = MagicMock()
    response.text = "x" * 1000

    assert olx._error_detail(response) == "x" * 500


# --------------------------------------------------------------------------
# get_access_token / exchange_code (baza prawdziwa, HTTP zamockowane)
# --------------------------------------------------------------------------


async def test_get_access_token_bez_autoryzacji_rzuca_auth_error(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(olx.OlxAuthError):
        await olx.get_access_token(db_session)


async def test_get_access_token_zwraca_bez_odswiezania_gdy_wazny(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO olx_token "
            "(id, access_token_encrypted, refresh_token_encrypted, "
            " access_expires_at, scope) "
            "VALUES (1, :access, :refresh, :expires_at, 'v2 read write')"
        ),
        {
            "access": crypto.encrypt("AT-valid"),
            "refresh": crypto.encrypt("RT-valid"),
            "expires_at": datetime.now(UTC) + timedelta(hours=1),
        },
    )
    await db_session.commit()

    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=AssertionError("nie powinno odswiezac"))
    monkeypatch.setattr(olx, "_http_client", lambda: fake_client)

    token = await olx.get_access_token(db_session)

    assert token == "AT-valid"
    fake_client.post.assert_not_called()


async def test_get_access_token_odswieza_gdy_wygasl_i_rotuje_refresh_token(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO olx_token "
            "(id, access_token_encrypted, refresh_token_encrypted, "
            " access_expires_at, scope) "
            "VALUES (1, :access, :refresh, :expires_at, 'v2 read write')"
        ),
        {
            "access": crypto.encrypt("AT-old"),
            "refresh": crypto.encrypt("RT-old"),
            "expires_at": datetime.now(UTC) - timedelta(seconds=5),
        },
    )
    await db_session.commit()

    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_response(
            200,
            {
                "access_token": "AT-new",
                "refresh_token": "RT-new",
                "expires_in": 3600,
                "scope": "v2 read write",
            },
        )
    )
    monkeypatch.setattr(olx, "_http_client", lambda: fake_client)

    token = await olx.get_access_token(db_session)

    assert token == "AT-new"
    _, kwargs = fake_client.post.call_args
    assert kwargs["json"]["grant_type"] == "refresh_token"
    assert kwargs["json"]["refresh_token"] == "RT-old"

    row = (
        await db_session.execute(
            text(
                "SELECT access_token_encrypted, refresh_token_encrypted "
                "FROM olx_token WHERE id = 1"
            )
        )
    ).first()
    assert crypto.decrypt(row[0]) == "AT-new"
    assert crypto.decrypt(row[1]) == "RT-new"

    op = (
        await db_session.execute(
            text(
                "SELECT operation, succeeded, request_payload::text "
                "FROM olx_operation ORDER BY id DESC LIMIT 1"
            )
        )
    ).first()
    assert op[0] == "refresh_token"
    assert op[1] is True
    assert "RT-old" not in op[2]
    assert "test-client-secret" not in op[2]


async def test_get_access_token_odswiezenia_bledu_rzuca_api_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await db_session.execute(
        text(
            "INSERT INTO olx_token "
            "(id, access_token_encrypted, refresh_token_encrypted, "
            " access_expires_at, scope) "
            "VALUES (1, :access, :refresh, :expires_at, 'v2 read write')"
        ),
        {
            "access": crypto.encrypt("AT-old"),
            "refresh": crypto.encrypt("RT-old"),
            "expires_at": datetime.now(UTC) - timedelta(seconds=5),
        },
    )
    await db_session.commit()

    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_response(400, {"error": "invalid_grant"})
    )
    monkeypatch.setattr(olx, "_http_client", lambda: fake_client)

    with pytest.raises(olx.OlxApiError):
        await olx.get_access_token(db_session)


async def test_exchange_code_zapisuje_tokeny_i_loguje_z_redakcja_sekretow(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_response(
            200,
            {
                "access_token": "AT-1",
                "refresh_token": "RT-1",
                "expires_in": 3600,
                "scope": "v2 read write",
            },
        )
    )
    monkeypatch.setattr(olx, "_http_client", lambda: fake_client)

    await olx.exchange_code(db_session, "auth-code-xyz")

    row = (
        await db_session.execute(
            text(
                "SELECT access_token_encrypted, refresh_token_encrypted, scope "
                "FROM olx_token WHERE id = 1"
            )
        )
    ).first()
    assert row is not None
    assert crypto.decrypt(row[0]) == "AT-1"
    assert crypto.decrypt(row[1]) == "RT-1"
    assert row[2] == "v2 read write"

    op = (
        await db_session.execute(
            text(
                "SELECT operation, succeeded, request_payload::text, "
                "response_payload::text FROM olx_operation ORDER BY id DESC LIMIT 1"
            )
        )
    ).first()
    assert op[0] == "oauth_exchange"
    assert op[1] is True
    # Redakcja: ani kod, ani sekret klienta, ani zaden token nie trafiaja do
    # olx_operation w postaci jawnej - zapisanie ich tam zniweczyloby sens
    # szyfrowania w olx_token.
    assert "auth-code-xyz" not in op[2]
    assert "test-client-secret" not in op[2]
    assert "AT-1" not in op[3]
    assert "RT-1" not in op[3]


async def test_exchange_code_bledu_olx_rzuca_api_error_i_loguje(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_response(400, {"error": "invalid_grant"})
    )
    monkeypatch.setattr(olx, "_http_client", lambda: fake_client)

    with pytest.raises(olx.OlxApiError):
        await olx.exchange_code(db_session, "zly-kod")

    op = (
        await db_session.execute(
            text(
                "SELECT succeeded, http_status FROM olx_operation "
                "ORDER BY id DESC LIMIT 1"
            )
        )
    ).first()
    assert op == (False, 400)


# --------------------------------------------------------------------------
# create_advert (baza prawdziwa, HTTP zamockowane)
# --------------------------------------------------------------------------


async def test_create_advert_sukces_loguje_operacje_i_zwraca_body(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # olx_operation.listing_id ma FK do listing - potrzebny prawdziwy wiersz.
    platform_id = (
        await db_session.execute(text("SELECT id FROM platform WHERE code = 'ps4_ps5'"))
    ).scalar_one()
    game_id = (
        await db_session.execute(
            text(
                "INSERT INTO game (title, platform_id) VALUES ('Test Game', :pid) "
                "RETURNING id"
            ),
            {"pid": platform_id},
        )
    ).scalar_one()
    listing_id = (
        await db_session.execute(
            text(
                "INSERT INTO listing (game_id, condition, price_pln, status) "
                "VALUES (:gid, 'used', 10, 'pending') RETURNING id"
            ),
            {"gid": game_id},
        )
    ).scalar_one()
    await db_session.commit()

    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_response(201, {"id": 999, "status": "new"})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    body = await olx.create_advert(
        db_session, {"title": "x"}, access_token="AT-1", listing_id=listing_id
    )

    assert body == {"id": 999, "status": "new"}
    _, kwargs = fake_client.post.call_args
    assert kwargs["headers"]["Authorization"] == "Bearer AT-1"

    op = (
        await db_session.execute(
            text(
                "SELECT listing_id, operation, succeeded, http_status "
                "FROM olx_operation ORDER BY id DESC LIMIT 1"
            )
        )
    ).first()
    assert tuple(op) == (listing_id, "create_advert", True, 201)


async def test_create_advert_blad_http_loguje_i_rzuca_api_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_response(422, {"error": "validation_failed"})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    with pytest.raises(olx.OlxApiError):
        await olx.create_advert(db_session, {"title": "x"}, access_token="AT-1")

    op = (
        await db_session.execute(
            text(
                "SELECT succeeded, http_status FROM olx_operation "
                "ORDER BY id DESC LIMIT 1"
            )
        )
    ).first()
    assert op == (False, 422)


async def test_create_advert_blad_sieciowy_loguje_i_rzuca_api_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()
    fake_client.post = AsyncMock(side_effect=httpx.ConnectError("timeout"))
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    with pytest.raises(olx.OlxApiError):
        await olx.create_advert(db_session, {"title": "x"}, access_token="AT-1")

    op = (
        await db_session.execute(
            text(
                "SELECT succeeded, http_status FROM olx_operation "
                "ORDER BY id DESC LIMIT 1"
            )
        )
    ).first()
    assert op == (False, None)


# --------------------------------------------------------------------------
# _unwrap_data / fetch_categories / search_leaf_categories / fetch_cities
# (rozpakowanie wrappera "data" Partner API + drzewo kategorii)
# --------------------------------------------------------------------------

_RAW_CATEGORY_TREE = [
    {
        "id": 1,
        "name": "Elektronika",
        "parent_id": 0,
        "is_leaf": False,
        "photos_limit": None,
        "created_at": "2020-01-01",
    },
    {
        "id": 2,
        "name": "Gry i Konsole",
        "parent_id": 1,
        "is_leaf": False,
        "photos_limit": None,
    },
    {"id": 3, "name": "Gry", "parent_id": 2, "is_leaf": True, "photos_limit": 8},
    {"id": 4, "name": "Konsole", "parent_id": 2, "is_leaf": True, "photos_limit": 8},
    {"id": 5, "name": "Telefony", "parent_id": 1, "is_leaf": True, "photos_limit": 4},
    {
        "id": 6,
        "name": "Motoryzacja",
        "parent_id": 0,
        "is_leaf": True,
        "photos_limit": 10,
    },
]


def test_unwrap_data_rozpakowuje_klucz_data() -> None:
    assert olx._unwrap_data({"data": [1, 2, 3]}) == [1, 2, 3]
    assert olx._unwrap_data({"data": {"id": 1}}) == {"id": 1}


def test_unwrap_data_bez_klucza_data_zwraca_bez_zmian() -> None:
    assert olx._unwrap_data([1, 2, 3]) == [1, 2, 3]
    assert olx._unwrap_data({"error": "invalid_grant"}) == {"error": "invalid_grant"}
    assert olx._unwrap_data(None) is None


async def test_fetch_categories_rozpakowuje_data_i_zwraca_kategorie_glowne(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(200, {"data": _RAW_CATEGORY_TREE})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    categories = await olx.fetch_categories(db_session)

    assert {c["id"] for c in categories} == {1, 6}
    # Zwiezly ksztalt - bez pol spoza _CATEGORY_FIELDS (np. "created_at").
    assert categories[0].keys() == {
        "id",
        "name",
        "parent_id",
        "is_leaf",
        "photos_limit",
    }


async def test_fetch_categories_zwraca_dzieci_podanego_parent_id(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(200, {"data": _RAW_CATEGORY_TREE})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    children = await olx.fetch_categories(db_session, parent_id=1)

    assert {c["id"] for c in children} == {2, 5}


async def test_fetch_categories_filtruje_po_q_w_obrebie_poziomu(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(200, {"data": _RAW_CATEGORY_TREE})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    matches = await olx.fetch_categories(db_session, parent_id=1, q="tel")

    assert [c["id"] for c in matches] == [5]


async def test_fetch_categories_drzewo_pobierane_tylko_raz_z_cache(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza, ze drzewo kategorii jest pobierane z OLX tylko raz.

    Kolejne wywolania (rozne parent_id) NIE odpytuja OLX ponownie - drzewo
    jest cache'owane w pamieci procesu (`_fetch_category_tree`), co chroni
    przed limitem OLX 4500 zadan/5 min.
    """
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(200, {"data": _RAW_CATEGORY_TREE})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    await olx.fetch_categories(db_session)
    await olx.fetch_categories(db_session, parent_id=1)
    await olx.search_leaf_categories(db_session, "gry")

    assert fake_client.get.call_count == 1


async def test_search_leaf_categories_zwraca_tylko_liscie_pasujace_rekurencyjnie(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(200, {"data": _RAW_CATEGORY_TREE})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    matches = await olx.search_leaf_categories(db_session, "gr")

    # "Gry" (id=3, lisc, dwa poziomy w dol od korzenia) pasuje; "Gry i
    # Konsole" (id=2) NIE, mimo pasujacej nazwy - to nie lisc (nie da sie w
    # niej wystawic ogloszenia).
    assert [c["id"] for c in matches] == [3]


async def test_create_advert_rozpakowuje_wrapper_data(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_client = MagicMock()
    fake_client.post = AsyncMock(
        return_value=_response(201, {"data": {"id": 999, "status": "new"}})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    body = await olx.create_advert(db_session, {"title": "x"}, access_token="AT-1")

    assert body == {"id": 999, "status": "new"}


async def test_fetch_cities_rozpakowuje_wrapper_data(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(200, {"data": [{"id": 1, "name": "Kraków"}]})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    cities = await olx.fetch_cities(db_session)

    assert cities == [{"id": 1, "name": "Kraków"}]
