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


@pytest.fixture(autouse=True)
def _clear_city_list_cache() -> Iterator[None]:
    """Zeruje cache listy miast (`olx._city_list_cache`) miedzy testami.

    Bez tego wynik jednego testu (np. zamockowana strona z falszywymi
    miastami) przecieklby jako "prawdziwy" cache do kolejnych testow w tym
    samym procesie pytest.
    """
    olx._city_list_cache = None
    yield
    olx._city_list_cache = None


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


def test_build_title_za_dlugi_usuwa_wymiane_tytul_gry_nietkniety() -> None:
    """Regresja realnego przypadku publikacji odrzucanej za dlugi tytul.

    Dluzszy tytul gry na Xbox 360 przekraczal limit 70 znakow. Poprzednia
    wersja ucinala TYTUL GRY w polowie slowa ("Medal of Honor Airborne" ->
    "Medal of Honor") - teraz zamiast tego usuwany jest opcjonalny segment
    " | Wymiana", a tytul gry i platforma zostaja w calosci.
    """
    title = olx.build_title("Medal of Honor Airborne", "Xbox 360")

    assert title == "Medal of Honor Airborne | Xbox 360 | Sklep | Kraków | Wysyłka"
    assert len(title) <= olx.MAX_TITLE_LENGTH
    assert "Wymiana" not in title


def test_build_title_bardzo_dlugi_usuwa_takze_wysylke() -> None:
    """Sprawdza usuwanie takze " | Wysyłka", gdy sama "Wymiana" nie wystarcza.

    W tej kolejnosci (Wymiana pierwsza, Wysyłka druga), tytul gry i
    platforma nietkniete.
    """
    title = olx.build_title("Tom Clancys Rainbow Six Vegas 2 Complete", "Xbox 360")

    assert title == (
        "Tom Clancys Rainbow Six Vegas 2 Complete | Xbox 360 | Sklep | Kraków"
    )
    assert len(title) <= olx.MAX_TITLE_LENGTH
    assert "Wymiana" not in title
    assert "Wysyłka" not in title


def test_build_title_zbyt_dlugi_nawet_bez_obu_segmentow_rzuca_blad() -> None:
    """Sprawdza zabezpieczenie, gdy tytul nie miesci sie nawet bez obu segmentow.

    Tytul gry i platforma NIGDY nie sa ucinane, wiec jedyna droga jest blad
    (do recznej korekty w poczekalni).
    """
    with pytest.raises(olx.OlxValidationError, match="70"):
        olx.build_title(
            "Tom Clancys Rainbow Six Vegas 2 Complete Edition Remastered",
            "Xbox 360",
        )


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
        district_id=0,
        price_pln=Decimal("99.99"),
        condition="used",
        platform_olx_attribute_value="xbox360",
        image_urls=["https://cdn.example.test/1.jpg", "https://cdn.example.test/2.jpg"],
        contact_name="ZibiCom",
    )

    assert payload["title"] == "Tytul"
    assert payload["category_id"] == 123
    # district_id=0 ("nieustawiona") - pomijana, nie wysylana jako 0.
    assert payload["location"] == {"city_id": 456}
    assert payload["price"] == {"value": 99.99, "currency": "PLN"}
    assert payload["images"] == [
        {"url": "https://cdn.example.test/1.jpg"},
        {"url": "https://cdn.example.test/2.jpg"},
    ]
    # "state" wysyla nasz enum WPROST (bez slownika) - to sa dokladnie kody
    # oczekiwane przez OLX (zweryfikowane empirycznie, patrz olx.py).
    assert {"code": "state", "value": "used"} in payload["attributes"]
    assert {"code": "type", "value": "xbox360"} in payload["attributes"]
    # Wymagane przez OLX (bez nich create_advert dostawal 400) - konto
    # zibicom jest firmowe, nazwa kontaktowa z konfiguracji.
    assert payload["advertiser_type"] == "business"
    assert payload["contact"] == {"name": "ZibiCom"}
    # Oba pola widoczne w odczycie ogloszenia (GET /adverts/{id}), ale
    # odrzucane przy tworzeniu (POST /adverts) - patrz docstring
    # build_advert_payload. Nigdy nie ma ich w budowanym payloadzie.
    assert "ad_delivery" not in payload
    assert "auto_extend_enabled" not in payload


def test_build_advert_payload_bez_platformy_pomija_atrybut_platformy() -> None:
    payload = olx.build_advert_payload(
        title="Tytul",
        description="Opis",
        category_id=1,
        city_id=1,
        district_id=0,
        price_pln=Decimal("10"),
        condition="new",
        platform_olx_attribute_value=None,
        image_urls=[],
        contact_name="ZibiCom",
    )

    codes = [attribute["code"] for attribute in payload["attributes"]]
    assert "type" not in codes
    assert payload["images"] == []


def test_build_advert_payload_district_id_dolaczany_gdy_dodatni() -> None:
    """Regresja realnego bledu publikacji odrzucanej bez district_id.

    OLX odrzuca publikacje w miastach z podzialem na dzielnice (np. Krakow)
    bez district_id - ale wyslanie go dla malej miejscowosci bez dzielnic
    tez psuje publikacje, wiec pole ma sie pojawiac WYLACZNIE gdy jest
    faktycznie ustawione (>0).
    """
    payload = olx.build_advert_payload(
        title="Tytul",
        description="Opis",
        category_id=1,
        city_id=8959,
        district_id=271,
        price_pln=Decimal("10"),
        condition="used",
        platform_olx_attribute_value=None,
        image_urls=[],
        contact_name="ZibiCom",
    )

    assert payload["location"] == {"city_id": 8959, "district_id": 271}


def test_build_advert_payload_za_duzo_zdjec_rzuca_blad_walidacji() -> None:
    with pytest.raises(olx.OlxValidationError, match="8"):
        olx.build_advert_payload(
            title="T",
            description="D",
            category_id=1,
            city_id=1,
            district_id=0,
            price_pln=Decimal("10"),
            condition="used",
            platform_olx_attribute_value=None,
            image_urls=[f"https://cdn.example.test/{i}.jpg" for i in range(9)],
            contact_name="ZibiCom",
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
        "pusta odpowiedź — prawdopodobnie blokada WAF/CloudFront"
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


async def test_fetch_cities_rozpakowuje_wrapper_data_i_zwiezly_ksztalt(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(
            200,
            {
                "data": [
                    {
                        "id": 8959,
                        "region_id": 4,
                        "name": "Kraków",
                        "county": "Kraków",
                        "municipality": "Kraków",
                        "latitude": 50.07567,
                        "longitude": 19.93084,
                    }
                ]
            },
        )
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    cities = await olx.fetch_cities(db_session)

    # Zwiezly ksztalt - bez pol spoza id/name/county/region_id (np.
    # "municipality"/"latitude"/"longitude").
    assert cities == [
        {"id": 8959, "name": "Kraków", "county": "Kraków", "region_id": 4}
    ]


async def test_fetch_cities_stronicuje_dopoki_strona_krotsza_niz_limit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza, ze `_fetch_city_list` pobiera WSZYSTKIE strony.

    OLX nie zwraca calego zbioru w jednej odpowiedzi i nie daje zadnych
    metadanych stronicowania - jedyny sygnal konca danych to strona krotsza
    niz zadany `limit`. Ustawiamy limit na 2, zeby przetestowac to bez
    konstruowania tysiecy rekordow.
    """
    monkeypatch.setattr(olx, "_CITY_PAGE_LIMIT", 2)
    await _insert_valid_token(db_session)

    pages = {
        0: [{"id": 1, "name": "A"}, {"id": 2, "name": "B"}],
        2: [{"id": 3, "name": "C"}, {"id": 4, "name": "D"}],
        4: [{"id": 5, "name": "E"}],  # krotsza niz limit -> koniec danych
    }

    def _get(_url: str, *, headers: dict, params: dict) -> MagicMock:
        return _response(200, {"data": pages[params["offset"]]})

    fake_client = MagicMock()
    fake_client.get = AsyncMock(side_effect=_get)
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    cities = await olx.fetch_cities(db_session)

    assert [c["id"] for c in cities] == [1, 2, 3, 4, 5]
    assert fake_client.get.call_count == 3


async def test_fetch_cities_zabezpieczenie_przed_nieskonczona_petla(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza limit bezpieczenstwa petli stronicowania.

    Gdyby OLX NIGDY nie zwrocil strony krotszej niz limit (np. regresja
    API), petla ma sie zatrzymac po `_MAX_CITY_PAGES`, zamiast pobierac
    strony bez konca.
    """
    monkeypatch.setattr(olx, "_CITY_PAGE_LIMIT", 1)
    monkeypatch.setattr(olx, "_MAX_CITY_PAGES", 3)
    await _insert_valid_token(db_session)

    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(200, {"data": [{"id": 1, "name": "X"}]})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    cities = await olx.fetch_cities(db_session)

    assert len(cities) == 3
    assert fake_client.get.call_count == 3


async def test_fetch_cities_cache_pobierane_tylko_raz(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(200, {"data": [{"id": 1, "name": "Kraków"}]})
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    await olx.fetch_cities(db_session)
    await olx.fetch_cities(db_session, q="krak")

    assert fake_client.get.call_count == 1


async def test_fetch_cities_filtruje_bez_wielkosci_liter_i_diakrytykow(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(
            200, {"data": [{"id": 1, "name": "Kraków"}, {"id": 2, "name": "Wrocław"}]}
        )
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    for needle in ["krakow", "KRAKOW", "krak", "Kraków"]:
        cities = await olx.fetch_cities(db_session, q=needle)
        assert [c["id"] for c in cities] == [1], needle


async def test_fetch_cities_sortuje_prefiks_przed_dopasowaniem_w_srodku(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(
            200,
            {
                "data": [
                    {"id": 1, "name": "Nowy Kraków"},  # "krak" w srodku
                    {"id": 2, "name": "Kraków"},  # zaczyna sie od "krak"
                ]
            },
        )
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    cities = await olx.fetch_cities(db_session, q="krak")

    assert [c["id"] for c in cities] == [2, 1]


# --------------------------------------------------------------------------
# fetch_districts
# --------------------------------------------------------------------------


async def test_fetch_districts_rozpakowuje_data_zwiezly_ksztalt_i_buduje_url(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(
            200,
            {
                "data": [
                    {
                        "id": 271,
                        "city_id": 8959,
                        "name": "Bieżanów-Prokocim",
                        "latitude": 50.01713,
                        "longitude": 20.01871,
                    }
                ]
            },
        )
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    districts = await olx.fetch_districts(db_session, 8959)

    # Zwiezly ksztalt - bez pol spoza id/name (np. "city_id"/"latitude").
    assert districts == [{"id": 271, "name": "Bieżanów-Prokocim"}]
    args, _ = fake_client.get.call_args
    assert args[0].endswith("/cities/8959/districts")


async def test_fetch_districts_miasto_bez_dzielnic_zwraca_puste(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza wynik dla miasta bez podzialu na dzielnice.

    Mala miejscowosc bez podzialu na dzielnice - OLX zwraca pusta liste
    (zweryfikowane empirycznie), nie blad.
    """
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(return_value=_response(200, {"data": []}))
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    districts = await olx.fetch_districts(db_session, 627)

    assert districts == []


# --------------------------------------------------------------------------
# fetch_category_attributes / _compact_attribute
# --------------------------------------------------------------------------


def test_compact_attribute_wymagalnosc_pod_validation_wartosci_na_gorze() -> None:
    """Sprawdza ksztalt zweryfikowany empirycznie na prawdziwym OLX.

    "required" jest pod "validation", ale "values" jest polem NAJWYZSZEGO
    poziomu (nie "validation.values") -
    kazda wartosc to {"code", "label"}, gdzie "code" idzie do payloadu.
    """
    raw = {
        "code": "state",
        "label": "Stan",
        "validation": {"required": True},
        "values": [
            {"code": "used", "label": "Używane"},
            {"code": "new", "label": "Nowe"},
        ],
    }

    assert olx._compact_attribute(raw) == {
        "code": "state",
        "label": "Stan",
        "required": True,
        "values": [
            {"code": "used", "label": "Używane"},
            {"code": "new", "label": "Nowe"},
        ],
    }


def test_compact_attribute_ksztalt_plaski_bez_validation() -> None:
    raw = {"code": "cena", "name": "Cena", "required": False, "values": []}

    assert olx._compact_attribute(raw) == {
        "code": "cena",
        "label": "Cena",
        "required": False,
        "values": [],
    }


def test_compact_attribute_brak_dozwolonych_wartosci_daje_pusta_liste() -> None:
    raw = {"code": "opis", "label": "Opis"}

    assert olx._compact_attribute(raw) == {
        "code": "opis",
        "label": "Opis",
        "required": False,
        "values": [],
    }


async def test_fetch_category_attributes_rozpakowuje_data_i_buduje_url(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(
            200,
            {
                "data": [
                    {
                        "code": "state",
                        "label": "Stan",
                        "validation": {"required": True},
                        "values": [
                            {"code": "used", "label": "Używane"},
                            {"code": "new", "label": "Nowe"},
                        ],
                    }
                ]
            },
        )
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    attributes = await olx.fetch_category_attributes(db_session, 2273)

    assert attributes == [
        {
            "code": "state",
            "label": "Stan",
            "required": True,
            "values": [
                {"code": "used", "label": "Używane"},
                {"code": "new", "label": "Nowe"},
            ],
        }
    ]
    args, _ = fake_client.get.call_args
    assert args[0].endswith("/categories/2273/attributes")


# --------------------------------------------------------------------------
# resolve_delivery_attribute
# --------------------------------------------------------------------------

# Ksztalt zweryfikowany empirycznie dla kategorii 2273 (Xbox) - etykiety
# InPost maja realny znak "®" po "Paczkomat" ("Paczkomat® 24/7 S/M/L"), stad
# dopasowanie po fragmencie tekstu w resolve_delivery_attribute, nie po
# rownosci calego stringa.
_REAL_DELIVERY_ATTRIBUTE = {
    "code": "delivery",
    "label": "Dostawa",
    "validation": {"required": False},
    "values": [
        {
            "code": "ef5414d2-1fa4-4344-bf09-d1528cfb58e1",
            "label": "InPost Paczkomat® 24/7 S",
        },
        {
            "code": "0509518f-6d74-45bd-aba7-ce658b784b8d",
            "label": "InPost Paczkomat® 24/7 M",
        },
        {
            "code": "85076df7-ad79-4e99-a52d-c1c2d4b67b2e",
            "label": "InPost Paczkomat® 24/7 L",
        },
    ],
}


async def test_resolve_delivery_attribute_dopasowuje_po_fragmencie_etykiety(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(
            200,
            {
                "data": [
                    _REAL_DELIVERY_ATTRIBUTE,
                    {"code": "state", "label": "Stan", "values": []},
                ]
            },
        )
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    code = await olx.resolve_delivery_attribute(db_session, 2273)

    assert code == "ef5414d2-1fa4-4344-bf09-d1528cfb58e1"


async def test_resolve_delivery_attribute_brak_atrybutu_delivery_zwraca_none(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(
            200, {"data": [{"code": "state", "label": "Stan", "values": []}]}
        )
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    code = await olx.resolve_delivery_attribute(db_session, 2274)

    assert code is None


async def test_resolve_delivery_attribute_brak_pasujacej_wartosci_zwraca_none(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza wynik, gdy atrybut "delivery" istnieje bez pasujacej wartosci.

    Zadna z jego wartosci nie pasuje do "InPost Paczkomat" + rozmiar "S"
    (np. tylko kurierzy, bez paczkomatow) - to prawidlowy wynik None, nie
    blad.
    """
    await _insert_valid_token(db_session)
    fake_client = MagicMock()
    fake_client.get = AsyncMock(
        return_value=_response(
            200,
            {
                "data": [
                    {
                        "code": "delivery",
                        "label": "Dostawa",
                        "values": [
                            {"code": "abc-123", "label": "DPD Kurier M"},
                        ],
                    }
                ]
            },
        )
    )
    monkeypatch.setattr(olx, "_partner_http_client", lambda: fake_client)

    code = await olx.resolve_delivery_attribute(db_session, 2272)

    assert code is None
