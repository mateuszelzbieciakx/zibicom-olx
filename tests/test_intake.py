"""Testy warstwy poczekalni - R2 i Gemini zamockowane, baza jest prawdziwa.

Enumy, CHECK-i i kaskady FK sa czescia kontraktu 0003_intake.sql, wiec te
testy celowo NIE mockuja bazy danych (patrz `db_session` w conftest.py).
"""

from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from zibicom import intake
from zibicom.models import PhotoExtraction, PlatformCode


async def _create_item(
    db_session: AsyncSession,
    *,
    title: str | None = "Elden Ring",
    price_pln: Decimal | None = Decimal("150"),
) -> tuple[int, int]:
    """Tworzy partie i jedna pozycje bezposrednio przez SQL (bez AI/R2)."""
    batch_id = (
        await db_session.execute(
            text("INSERT INTO intake_batch DEFAULT VALUES RETURNING id")
        )
    ).scalar_one()
    item_id = (
        await db_session.execute(
            text(
                "INSERT INTO intake_item (batch_id, position, title, price_pln) "
                "VALUES (:batch_id, 1, :title, :price_pln) RETURNING id"
            ),
            {"batch_id": batch_id, "title": title, "price_pln": price_pln},
        )
    ).scalar_one()
    await db_session.commit()
    return batch_id, item_id


# --------------------------------------------------------------------------
# create_batch
# --------------------------------------------------------------------------


async def test_create_batch_zapisuje_zdjecia_w_kolejnosci_wgrania(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(intake.photos, "normalize_photo", lambda raw: raw)
    urls = iter(["https://cdn.example.test/1.jpg", "https://cdn.example.test/2.jpg"])
    monkeypatch.setattr(intake.photos, "upload_photo", lambda _: next(urls))

    batch_id = await intake.create_batch(
        db_session, [("przod.jpg", b"a"), ("tyl.jpg", b"b")]
    )

    rows = (
        await db_session.execute(
            text(
                "SELECT position, original_filename, public_url FROM intake_photo "
                "WHERE batch_id = :batch_id ORDER BY position"
            ),
            {"batch_id": batch_id},
        )
    ).all()
    assert [tuple(row) for row in rows] == [
        (1, "przod.jpg", "https://cdn.example.test/1.jpg"),
        (2, "tyl.jpg", "https://cdn.example.test/2.jpg"),
    ]


async def test_create_batch_bez_plikow_rzuca_blad_walidacji(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeValidationError):
        await intake.create_batch(db_session, [])


async def test_create_batch_niepoprawnego_zdjecia_rzuca_blad_walidacji(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(intake.IntakeValidationError, match="obraz"):
        await intake.create_batch(db_session, [("plik.txt", b"to nie jest zdjecie")])


# --------------------------------------------------------------------------
# extract_batch
# --------------------------------------------------------------------------


async def test_extract_batch_grupuje_zdjecia_w_pozycje(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(intake.photos, "normalize_photo", lambda raw: raw)
    monkeypatch.setattr(
        intake.photos, "upload_photo", lambda _: "https://cdn.example.test/x.jpg"
    )
    batch_id = await intake.create_batch(
        db_session, [("przod.jpg", b"a"), ("tyl.jpg", b"b")]
    )

    monkeypatch.setattr(intake.photos, "download_photo", lambda url: b"stub")
    extractions = iter(
        [
            PhotoExtraction(
                title="Bloodborne",
                platform=PlatformCode.PS4_PS5,
                price_pln=Decimal("120"),
                condition="used",
                is_front=True,
                title_confident=True,
                price_confident=True,
            ),
            PhotoExtraction(
                title=None,
                platform=PlatformCode.PS4_PS5,
                price_pln=None,
                condition=None,
                is_front=False,
                title_confident=True,
                price_confident=True,
            ),
        ]
    )
    monkeypatch.setattr(intake.vision, "recognize_photo", lambda raw: next(extractions))

    created = await intake.extract_batch(db_session, batch_id)

    assert created == 1
    items = await intake.list_items(db_session, batch_id)
    assert len(items) == 1
    item = items[0]
    assert item.title == "Bloodborne"
    assert item.price_pln == Decimal("120")
    assert item.platform_code == "ps4_ps5"
    assert item.condition == "used"
    assert len(item.photo_urls) == 2

    batch_status = (
        await db_session.execute(
            text("SELECT status::TEXT FROM intake_batch WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
    ).scalar_one()
    assert batch_status == "review"


async def test_extract_batch_nieistniejacej_partii_rzuca_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeNotFoundError):
        await intake.extract_batch(db_session, 999_999)


def _extraction(
    title: str | None,
    is_front: bool | None,
    *,
    price_pln: Decimal | None = Decimal("100"),
) -> PhotoExtraction:
    """Buduje wynik rozpoznania do testow ekstrakcji przyrostowej/wznawialnej."""
    return PhotoExtraction(
        title=title,
        platform=PlatformCode.PS4_PS5,
        price_pln=price_pln,
        condition="used",
        is_front=is_front,
        title_confident=True,
        price_confident=True,
    )


async def test_extract_batch_domyka_grupy_przyrostowo_przez_cala_partie(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza domykanie dwoch grup w jednej partii.

    Pierwsza domknieta w trakcie petli (przez kolejny 'przod'), druga
    dopiero na koncu partii (`IncrementalGrouper.close()`) - obie musza
    trafic do intake_item z poprawnymi pozycjami. Wznawialnosc przebiegu
    przerwanego W TRAKCIE (grupa juz zapisana, kolejna jeszcze nie) jest
    pokryta osobnym testem (`test_extract_batch_wznowienie_nie_duplikuje_pozycji`).
    """
    monkeypatch.setattr(intake.photos, "normalize_photo", lambda raw: raw)
    monkeypatch.setattr(
        intake.photos, "upload_photo", lambda _: "https://cdn.example.test/x.jpg"
    )
    batch_id = await intake.create_batch(
        db_session, [("1.jpg", b"a"), ("2.jpg", b"b"), ("3.jpg", b"c")]
    )
    monkeypatch.setattr(intake.photos, "download_photo", lambda url: b"stub")

    extractions = [
        _extraction("Bloodborne", True),
        _extraction(None, False),
        _extraction("Sekiro", True),
    ]
    monkeypatch.setattr(
        intake.vision, "recognize_photo", lambda raw: extractions.pop(0)
    )

    await intake.extract_batch(db_session, batch_id)

    items = await intake.list_items(db_session, batch_id)
    assert len(items) == 2
    assert [item.title for item in items] == ["Bloodborne", "Sekiro"]
    assert [item.position for item in items] == [1, 2]


async def test_extract_batch_wznowienie_nie_duplikuje_pozycji(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regresja WZNAWIALNOSCI.

    Przerwana partia wznowiona na tej samej partii MUSI pominac zdjecia z
    wypelnionym ai_raw oraz juz zapisane egzemplarze - inaczej powstaja
    duplikaty pozycji (podwojne ogloszenia na OLX).
    """
    monkeypatch.setattr(intake.photos, "normalize_photo", lambda raw: raw)
    monkeypatch.setattr(
        intake.photos, "upload_photo", lambda _: "https://cdn.example.test/x.jpg"
    )
    batch_id = await intake.create_batch(
        db_session,
        [("1.jpg", b"a"), ("2.jpg", b"b"), ("3.jpg", b"c"), ("4.jpg", b"d")],
    )
    monkeypatch.setattr(intake.photos, "download_photo", lambda url: b"stub")

    # Zdjecia 1-3 (domykajace pierwsza grupe: 1=przod, 2=tyl, 3=przod drugiej
    # gry) rozpoznaja sie poprawnie; 4. zdjecie symuluje przerwanie procesu
    # (wyczerpany limit Gemini) - PO tym, jak pierwsza grupa zdazyla sie juz
    # zapisac (domknieta przez zdjecie #3), ale PRZED domknieciem drugiej.
    first_run = iter(
        [
            _extraction("Bloodborne", True),
            _extraction(None, False),
            _extraction("Sekiro", True),
        ]
    )

    def _recognize_first_run(raw: bytes) -> PhotoExtraction:
        try:
            return next(first_run)
        except StopIteration:
            raise intake.vision.GeminiQuotaExceededError("limit wyczerpany") from None

    monkeypatch.setattr(intake.vision, "recognize_photo", _recognize_first_run)

    with pytest.raises(intake.IntakeError):
        await intake.extract_batch(db_session, batch_id)

    items_after_crash = await intake.list_items(db_session, batch_id)
    assert len(items_after_crash) == 1
    assert items_after_crash[0].title == "Bloodborne"

    status_after_crash = (
        await db_session.execute(
            text("SELECT status::TEXT FROM intake_batch WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
    ).scalar_one()
    assert status_after_crash == "failed"

    # Wznowienie: zdjecia 1-3 maja juz wypelniony ai_raw - recognize_photo NIE
    # powinno byc dla nich wywolane drugi raz, tylko dla zdjecia 4.
    recognize_calls: list[bytes] = []

    def _recognize_second_run(raw: bytes) -> PhotoExtraction:
        recognize_calls.append(raw)
        return _extraction(None, False)

    monkeypatch.setattr(intake.vision, "recognize_photo", _recognize_second_run)

    created = await intake.extract_batch(db_session, batch_id)

    assert created == 1  # tylko nowa pozycja domknieta w TYM przebiegu
    assert len(recognize_calls) == 1  # tylko zdjecie #4

    items_after_resume = await intake.list_items(db_session, batch_id)
    assert len(items_after_resume) == 2
    assert [item.title for item in items_after_resume] == ["Bloodborne", "Sekiro"]
    assert [item.position for item in items_after_resume] == [1, 2]
    # Pozycja z pierwszego przebiegu to DOKLADNIE ten sam wiersz (to samo id) -
    # nie zostala utworzona drugi raz.
    assert items_after_resume[0].id == items_after_crash[0].id

    status_after_resume = (
        await db_session.execute(
            text("SELECT status::TEXT FROM intake_batch WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
    ).scalar_one()
    assert status_after_resume == "review"


async def test_extract_batch_blad_pojedynczego_zdjecia_nie_przerywa_partii(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza, ze blad JEDNEGO zdjecia nie przerywa calej partii.

    Blad pobrania/rozpoznania jednego zdjecia (np. R2 niedostepne) nie
    przerywa calej partii - w odroznieniu od wyczerpania limitu Gemini,
    ktore jest bledem systemowym.
    """
    monkeypatch.setattr(intake.photos, "normalize_photo", lambda raw: raw)
    monkeypatch.setattr(
        intake.photos, "upload_photo", lambda _: "https://cdn.example.test/x.jpg"
    )
    batch_id = await intake.create_batch(
        db_session, [("przod.jpg", b"a"), ("tyl.jpg", b"b")]
    )

    def _download(url: str) -> bytes:
        raise ValueError("R2 niedostepne")

    monkeypatch.setattr(intake.photos, "download_photo", _download)
    recognize_photo = Mock(side_effect=AssertionError("nie powinno byc wywolane"))
    monkeypatch.setattr(intake.vision, "recognize_photo", recognize_photo)

    created = await intake.extract_batch(db_session, batch_id)

    assert created == 1
    recognize_photo.assert_not_called()

    items = await intake.list_items(db_session, batch_id)
    assert len(items) == 1
    assert items[0].title is None
    assert "brak tytulu" in (items[0].ai_warning or "")

    status = (
        await db_session.execute(
            text("SELECT status::TEXT FROM intake_batch WHERE id = :batch_id"),
            {"batch_id": batch_id},
        )
    ).scalar_one()
    assert status == "review"

    ai_raw_notes = (
        await db_session.execute(
            text(
                "SELECT ai_raw->>'note' FROM intake_photo "
                "WHERE batch_id = :batch_id ORDER BY position"
            ),
            {"batch_id": batch_id},
        )
    ).all()
    assert all("Blad rozpoznania" in row[0] for row in ai_raw_notes)


# --------------------------------------------------------------------------
# list_items
# --------------------------------------------------------------------------


async def test_list_items_nieistniejacej_partii_rzuca_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeNotFoundError):
        await intake.list_items(db_session, 999_999)


async def test_list_items_after_zwraca_tylko_pozycje_nowsze_niz_id(
    db_session: AsyncSession,
) -> None:
    batch_id, item_1 = await _create_item(db_session, title="Pierwsza")
    item_2 = await _add_item(db_session, batch_id, 2, status="pending", title="Druga")

    all_items = await intake.list_items_after(db_session, batch_id, 0)
    assert [item.id for item in all_items] == [item_1, item_2]

    newer_only = await intake.list_items_after(db_session, batch_id, item_1)
    assert [item.id for item in newer_only] == [item_2]

    none_newer = await intake.list_items_after(db_session, batch_id, item_2)
    assert none_newer == []


async def test_list_items_after_nieistniejacej_partii_rzuca_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeNotFoundError):
        await intake.list_items_after(db_session, 999_999, 0)


# --------------------------------------------------------------------------
# update_item
# --------------------------------------------------------------------------


async def test_update_item_koryguje_pola(db_session: AsyncSession) -> None:
    _, item_id = await _create_item(db_session, title=None, price_pln=None)

    updated = await intake.update_item(
        db_session,
        item_id,
        intake.IntakeItemUpdate(title="Nowy tytul", price_pln=Decimal("99.99")),
    )

    assert updated.title == "Nowy tytul"
    assert updated.price_pln == Decimal("99.99")


async def test_update_item_nieistniejaca_platforma_rzuca_blad(
    db_session: AsyncSession,
) -> None:
    _, item_id = await _create_item(db_session)

    with pytest.raises(intake.IntakeValidationError):
        await intake.update_item(
            db_session, item_id, intake.IntakeItemUpdate(platform_id=999_999)
        )


async def test_update_item_bez_pol_rzuca_blad(db_session: AsyncSession) -> None:
    _, item_id = await _create_item(db_session)

    with pytest.raises(intake.IntakeValidationError):
        await intake.update_item(db_session, item_id, intake.IntakeItemUpdate())


async def test_update_item_nieistniejacej_pozycji_rzuca_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeNotFoundError):
        await intake.update_item(
            db_session, 999_999, intake.IntakeItemUpdate(title="X")
        )


# --------------------------------------------------------------------------
# approve_item
# --------------------------------------------------------------------------


async def test_approve_item_bez_tytulu_i_ceny_rzuca_czytelny_blad(
    db_session: AsyncSession,
) -> None:
    _, item_id = await _create_item(db_session, title=None, price_pln=None)

    with pytest.raises(intake.IntakeValidationError, match="tytulu"):
        await intake.approve_item(db_session, item_id)


async def test_approve_item_z_tytulem_i_cena_ustawia_status_approved(
    db_session: AsyncSession,
) -> None:
    _, item_id = await _create_item(db_session)

    approved = await intake.approve_item(db_session, item_id)

    assert approved.status == "approved"


async def test_approve_item_juz_zatwierdzonej_rzuca_blad(
    db_session: AsyncSession,
) -> None:
    _, item_id = await _create_item(db_session)
    await intake.approve_item(db_session, item_id)

    with pytest.raises(intake.IntakeValidationError):
        await intake.approve_item(db_session, item_id)


# --------------------------------------------------------------------------
# reject_item
# --------------------------------------------------------------------------


async def test_reject_item_ustawia_status_rejected(db_session: AsyncSession) -> None:
    _, item_id = await _create_item(db_session)

    rejected = await intake.reject_item(db_session, item_id)

    assert rejected.status == "rejected"


async def test_reject_item_juz_odrzuconej_rzuca_blad(db_session: AsyncSession) -> None:
    _, item_id = await _create_item(db_session)
    await intake.reject_item(db_session, item_id)

    with pytest.raises(intake.IntakeValidationError):
        await intake.reject_item(db_session, item_id)


# --------------------------------------------------------------------------
# list_platforms
# --------------------------------------------------------------------------


async def test_list_platforms_zwraca_slownik(db_session: AsyncSession) -> None:
    platforms = await intake.list_platforms(db_session)

    codes = {platform.code for platform in platforms}
    assert "ps4_ps5" in codes
    assert "other" in codes


# --------------------------------------------------------------------------
# _map_olx_status
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_status", "expected"),
    [
        ("active", "active"),
        ("new", "pending"),
        ("waiting", "pending"),
        ("moderated", "pending"),
        ("removed", "removed"),
        ("outdated", "removed"),
        ("disabled", "pending"),
    ],
)
def test_map_olx_status_znane_wartosci(raw_status: str, expected: str) -> None:
    assert intake._map_olx_status(raw_status) == expected


def test_map_olx_status_nieznana_wartosc_mapuje_na_pending_z_ostrzezeniem(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        result = intake._map_olx_status("some_new_olx_status")

    assert result == "pending"
    assert "some_new_olx_status" in caplog.text


def test_map_olx_status_brak_wartosci_mapuje_na_pending() -> None:
    assert intake._map_olx_status(None) == "pending"


# --------------------------------------------------------------------------
# publish_item
# --------------------------------------------------------------------------


async def _create_pending_item(
    db_session: AsyncSession,
    *,
    title: str | None = "Bloodborne",
    price_pln: Decimal = Decimal("150"),
    condition: str = "used",
    platform_code: str = "ps4_ps5",
    photo_urls: list[str] | None = None,
) -> tuple[int, int]:
    """Tworzy partie i pozycje ze zdjeciami, ze statusem 'pending'."""
    platform_id = (
        await db_session.execute(
            text("SELECT id FROM platform WHERE code = :code"),
            {"code": platform_code},
        )
    ).scalar_one()

    batch_id = (
        await db_session.execute(
            text("INSERT INTO intake_batch DEFAULT VALUES RETURNING id")
        )
    ).scalar_one()
    item_id = (
        await db_session.execute(
            text(
                "INSERT INTO intake_item "
                "(batch_id, position, title, platform_id, price_pln, condition) "
                "VALUES (:batch_id, 1, :title, :platform_id, :price_pln, "
                " CAST(:condition AS listing_condition)) RETURNING id"
            ),
            {
                "batch_id": batch_id,
                "title": title,
                "platform_id": platform_id,
                "price_pln": price_pln,
                "condition": condition,
            },
        )
    ).scalar_one()

    for position, url in enumerate(
        photo_urls or ["https://cdn.example.test/1.jpg"], start=1
    ):
        await db_session.execute(
            text(
                "INSERT INTO intake_photo (batch_id, item_id, position, public_url) "
                "VALUES (:batch_id, :item_id, :position, :url)"
            ),
            {
                "batch_id": batch_id,
                "item_id": item_id,
                "position": position,
                "url": url,
            },
        )
    await db_session.commit()
    return batch_id, item_id


async def _create_approved_item(
    db_session: AsyncSession,
    *,
    title: str = "Bloodborne",
    price_pln: Decimal = Decimal("150"),
    condition: str = "used",
    platform_code: str = "ps4_ps5",
    photo_urls: list[str] | None = None,
) -> tuple[int, int]:
    """Tworzy partie i JUZ zatwierdzona pozycje ze zdjeciami - do testow publish."""
    batch_id, item_id = await _create_pending_item(
        db_session,
        title=title,
        price_pln=price_pln,
        condition=condition,
        platform_code=platform_code,
        photo_urls=photo_urls,
    )
    approved = await intake.approve_item(db_session, item_id)
    assert approved.status == "approved"
    return batch_id, item_id


async def test_publish_item_promuje_do_tabel_produkcyjnych(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, item_id = await _create_approved_item(db_session)

    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    monkeypatch.setattr(
        intake.olx,
        "create_advert",
        AsyncMock(return_value={"id": 12345, "status": "new"}),
    )

    published = await intake.publish_item(db_session, item_id)

    assert published.status == "published"
    assert published.listing_id is not None

    listing = (
        await db_session.execute(
            text(
                "SELECT game_id, condition::TEXT, price_pln, status, "
                "olx_advert_id, olx_status FROM listing WHERE id = :id"
            ),
            {"id": published.listing_id},
        )
    ).first()
    mapping = listing._mapping
    assert mapping["condition"] == "used"
    assert mapping["price_pln"] == Decimal("150")
    assert mapping["status"] == "pending"  # moderacja OLX - NIE zakladamy 'active'
    assert mapping["olx_advert_id"] == 12345
    assert mapping["olx_status"] == "new"

    game_title = (
        await db_session.execute(
            text("SELECT title FROM game WHERE id = :id"), {"id": mapping["game_id"]}
        )
    ).scalar_one()
    assert game_title == "Bloodborne"

    photo_rows = (
        await db_session.execute(
            text(
                "SELECT public_url, is_primary FROM listing_photo "
                "WHERE listing_id = :id ORDER BY position"
            ),
            {"id": published.listing_id},
        )
    ).all()
    assert [tuple(row) for row in photo_rows] == [
        ("https://cdn.example.test/1.jpg", True)
    ]


async def test_publish_item_status_active_mapuje_i_ustawia_posted_at(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regresja: FIFO (listing_fifo_idx) szuka WYLACZNIE status='active'.

    Gdy OLX zwroci przy tworzeniu ogloszenie juz aktywne (bez moderacji dla
    zaufanych kont), listing.status ma byc 'active' - nie na sztywno
    'pending' jak poprzednio - i posted_at ma byc ustawione.
    """
    _, item_id = await _create_approved_item(db_session)

    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    monkeypatch.setattr(
        intake.olx,
        "create_advert",
        AsyncMock(return_value={"id": 12345, "status": "active"}),
    )

    published = await intake.publish_item(db_session, item_id)

    listing = (
        await db_session.execute(
            text(
                "SELECT status::TEXT, olx_status, posted_at FROM listing WHERE id = :id"
            ),
            {"id": published.listing_id},
        )
    ).first()
    status, olx_status, posted_at = listing
    assert status == "active"
    assert olx_status == "active"
    assert posted_at is not None


async def test_publish_item_wysyla_category_id_z_platformy(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza, ze category_id w payloadzie OLX pochodzi z platformy.

    Wartosc bierzemy z platform.olx_category_id (per producent, migracja
    0005) - NIE z globalnej konfiguracji.
    """
    _, item_id = await _create_approved_item(db_session, platform_code="ps4_ps5")

    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    create_advert = AsyncMock(return_value={"id": 12345, "status": "new"})
    monkeypatch.setattr(intake.olx, "create_advert", create_advert)

    await intake.publish_item(db_session, item_id)

    payload = create_advert.call_args.args[1]
    assert payload["category_id"] == 2272  # sony, patrz 0005_olx_category_mapping.sql


async def test_publish_item_nie_dolacza_ad_delivery_ani_auto_extend(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regresja pustego 400 "Data validation error occurred" przy publikacji.

    Porownanie udanego i odrzuconego payloadu wykazalo, ze "ad_delivery" -
    tak samo jak "auto_extend_enabled" - jest widoczne w odczycie
    ogloszenia, ale odrzucane przy tworzeniu. `resolve_delivery_attribute`
    NIE jest juz wywolywane przy budowaniu payloadu (mockujemy je tutaj
    tylko po to, zeby test od razu wybuchl, gdyby ktos przypadkiem
    przywrocil to polaczenie) - `create_advert` w ogole go nie widzi.
    """
    _, item_id = await _create_approved_item(db_session, platform_code="xbox360")

    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    resolve_delivery_attribute = AsyncMock(
        return_value="ef5414d2-1fa4-4344-bf09-d1528cfb58e1"
    )
    monkeypatch.setattr(
        intake.olx, "resolve_delivery_attribute", resolve_delivery_attribute
    )
    create_advert = AsyncMock(return_value={"id": 12345, "status": "new"})
    monkeypatch.setattr(intake.olx, "create_advert", create_advert)

    await intake.publish_item(db_session, item_id)

    payload = create_advert.call_args.args[1]
    assert "ad_delivery" not in payload
    assert "auto_extend_enabled" not in payload
    resolve_delivery_attribute.assert_not_called()


async def test_publish_item_platforma_bez_kategorii_olx_rzuca_blad(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza, ze publikacja platformy bez kategorii OLX odmawia czytelnym bledem.

    "other" nie ma ustalonej platform.olx_category_id (migracja 0005) -
    publikacja ma odmowic PRZED jakimkolwiek wywolaniem OLX, zamiast wyslac
    ogloszenie z brakujaca/bledna kategoria.
    """
    _, item_id = await _create_approved_item(db_session, platform_code="other")

    create_advert = AsyncMock()
    monkeypatch.setattr(intake.olx, "create_advert", create_advert)

    with pytest.raises(intake.IntakeValidationError, match="kategorii OLX"):
        await intake.publish_item(db_session, item_id)

    create_advert.assert_not_called()


async def test_publish_item_druga_kopia_dolacza_do_istniejacej_gry(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dwie kopie (tytul, platforma) dziela jedna `game` (bez duplikatow)."""
    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    advert_ids = iter([111, 222])
    monkeypatch.setattr(
        intake.olx,
        "create_advert",
        AsyncMock(
            side_effect=lambda *a, **k: {"id": next(advert_ids), "status": "new"}
        ),
    )

    _, item_id_1 = await _create_approved_item(db_session)
    _, item_id_2 = await _create_approved_item(db_session)
    published_1 = await intake.publish_item(db_session, item_id_1)
    published_2 = await intake.publish_item(db_session, item_id_2)

    game_count = (
        await db_session.execute(
            text("SELECT COUNT(*) FROM game WHERE lower(title) = 'bloodborne'")
        )
    ).scalar_one()
    assert game_count == 1

    game_ids = (
        await db_session.execute(
            text("SELECT DISTINCT game_id FROM listing WHERE id = ANY(:ids)"),
            {"ids": [published_1.listing_id, published_2.listing_id]},
        )
    ).all()
    assert len(game_ids) == 1


async def test_publish_item_nieistniejacej_pozycji_rzuca_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeNotFoundError):
        await intake.publish_item(db_session, 999_999)


async def test_publish_item_niezatwierdzonej_pozycji_rzuca_blad(
    db_session: AsyncSession,
) -> None:
    _, item_id = await _create_item(db_session)

    with pytest.raises(intake.IntakeValidationError, match="approved"):
        await intake.publish_item(db_session, item_id)


async def test_publish_item_blad_olx_wycofuje_cala_transakcje(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Krytyczny test: OLX odrzuca ogloszenie -> NIC nie zostaje w bazie.

    Odwrotna asymetria (ogloszenie na OLX bez rekordu w bazie) jest
    niemozliwa do przetestowania bez prawdziwego OLX, ale kolejnosc
    operacji w `publish_item` (najpierw baza, potem OLX, commit dopiero po
    sukcesie) gwarantuje, ze rowniez ten przypadek nie wystapi.
    """
    _, item_id = await _create_approved_item(db_session)

    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    monkeypatch.setattr(
        intake.olx,
        "create_advert",
        AsyncMock(side_effect=intake.olx.OlxApiError("OLX zwrocilo 500")),
    )

    with pytest.raises(intake.olx.OlxApiError):
        await intake.publish_item(db_session, item_id)

    game_count = (
        await db_session.execute(text("SELECT COUNT(*) FROM game"))
    ).scalar_one()
    assert game_count == 0
    listing_count = (
        await db_session.execute(text("SELECT COUNT(*) FROM listing"))
    ).scalar_one()
    assert listing_count == 0

    item_status = (
        await db_session.execute(
            text("SELECT status::TEXT FROM intake_item WHERE id = :id"),
            {"id": item_id},
        )
    ).scalar_one()
    assert item_status == "approved"


# --------------------------------------------------------------------------
# publish_batch
# --------------------------------------------------------------------------


async def _add_item(
    db_session: AsyncSession,
    batch_id: int,
    position: int,
    *,
    status: str = "approved",
    title: str = "Bloodborne",
    price_pln: Decimal = Decimal("150"),
    condition: str = "used",
    platform_code: str = "ps4_ps5",
) -> int:
    """Dodaje pozycje o zadanym statusie do istniejacej partii.

    Do testow `publish_batch`, ktore potrzebuja wielu pozycji w JEDNEJ
    partii - w odroznieniu od `_create_approved_item` (nowa partia za
    kazdym razem).
    """
    platform_id = (
        await db_session.execute(
            text("SELECT id FROM platform WHERE code = :code"),
            {"code": platform_code},
        )
    ).scalar_one()
    item_id = (
        await db_session.execute(
            text(
                "INSERT INTO intake_item "
                "(batch_id, position, title, platform_id, price_pln, condition, "
                " status) "
                "VALUES (:batch_id, :position, :title, :platform_id, :price_pln, "
                " CAST(:condition AS listing_condition), "
                " CAST(:status AS intake_item_status)) "
                "RETURNING id"
            ),
            {
                "batch_id": batch_id,
                "position": position,
                "title": title,
                "platform_id": platform_id,
                "price_pln": price_pln,
                "condition": condition,
                "status": status,
            },
        )
    ).scalar_one()
    await db_session.commit()
    return item_id


async def test_publish_batch_publikuje_sekwencyjnie_z_pauza_miedzy_probami(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_id, item_1 = await _create_approved_item(db_session)
    item_2 = await _add_item(db_session, batch_id, 2, title="Dark Souls")
    item_3 = await _add_item(db_session, batch_id, 3, title="Sekiro")

    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    advert_ids = iter([111, 222, 333])
    monkeypatch.setattr(
        intake.olx,
        "create_advert",
        AsyncMock(
            side_effect=lambda *a, **k: {"id": next(advert_ids), "status": "new"}
        ),
    )
    sleep = AsyncMock()
    monkeypatch.setattr(intake.asyncio, "sleep", sleep)

    result = await intake.publish_batch(db_session, batch_id)

    assert result.published == 3
    assert result.failed == 0
    assert result.skipped == 0
    assert result.aborted is False
    assert result.errors == []

    # Pauza MIEDZY probami - trzy pozycje, dwie pauzy, zadnej przed pierwsza.
    assert sleep.await_count == 2
    sleep.assert_awaited_with(0.3)

    for item_id in (item_1, item_2, item_3):
        status = (
            await db_session.execute(
                text("SELECT status::TEXT FROM intake_item WHERE id = :id"),
                {"id": item_id},
            )
        ).scalar_one()
        assert status == "published"


async def test_publish_batch_pomija_pozycje_bez_statusu_approved(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_id, item_approved = await _create_approved_item(db_session)
    item_pending = await _add_item(db_session, batch_id, 2, status="pending")

    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    monkeypatch.setattr(
        intake.olx,
        "create_advert",
        AsyncMock(return_value={"id": 111, "status": "new"}),
    )
    monkeypatch.setattr(intake.asyncio, "sleep", AsyncMock())

    result = await intake.publish_batch(db_session, batch_id)

    assert result.published == 1
    assert result.skipped == 1
    assert result.failed == 0

    pending_status = (
        await db_session.execute(
            text("SELECT status::TEXT FROM intake_item WHERE id = :id"),
            {"id": item_pending},
        )
    ).scalar_one()
    assert pending_status == "pending"


async def test_publish_batch_kontynuuje_po_pojedynczym_bledzie(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pojedynczy blad (nie 3 pod rzad) nie przerywa przebiegu."""
    batch_id, item_1 = await _create_approved_item(db_session)
    item_2 = await _add_item(db_session, batch_id, 2, title="Dark Souls")
    item_3 = await _add_item(db_session, batch_id, 3, title="Sekiro")
    item_4 = await _add_item(db_session, batch_id, 4, title="Nioh")

    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    monkeypatch.setattr(
        intake.olx,
        "create_advert",
        AsyncMock(
            side_effect=[
                intake.olx.OlxApiError("blad 1"),
                {"id": 111, "status": "new"},
                intake.olx.OlxApiError("blad 2"),
                intake.olx.OlxApiError("blad 3"),
            ]
        ),
    )
    monkeypatch.setattr(intake.asyncio, "sleep", AsyncMock())

    result = await intake.publish_batch(db_session, batch_id)

    assert result.published == 1
    assert result.failed == 3
    assert result.skipped == 0
    assert result.aborted is False
    assert [item_id for item_id, _ in result.errors] == [item_1, item_3, item_4]

    status_2 = (
        await db_session.execute(
            text("SELECT status::TEXT FROM intake_item WHERE id = :id"),
            {"id": item_2},
        )
    ).scalar_one()
    assert status_2 == "published"


async def test_publish_batch_circuit_breaker_przerywa_po_trzech_bledach_pod_rzad(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    batch_id, item_1 = await _create_approved_item(db_session)
    item_2 = await _add_item(db_session, batch_id, 2, title="Dark Souls")
    item_3 = await _add_item(db_session, batch_id, 3, title="Sekiro")
    item_4 = await _add_item(db_session, batch_id, 4, title="Nioh")

    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    monkeypatch.setattr(
        intake.olx,
        "create_advert",
        AsyncMock(side_effect=intake.olx.OlxApiError("OLX zwrocilo 500")),
    )
    sleep = AsyncMock()
    monkeypatch.setattr(intake.asyncio, "sleep", sleep)

    result = await intake.publish_batch(db_session, batch_id)

    assert result.published == 0
    assert result.failed == 3
    assert result.aborted is True
    assert [item_id for item_id, _ in result.errors] == [item_1, item_2, item_3]
    # Pauzy tylko miedzy proba 1-2 i 2-3 - przebieg konczy sie zaraz po
    # trzecim bledzie, bez proby (a wiec i pauzy) dla czwartej pozycji.
    assert sleep.await_count == 2

    # Czwarta pozycja nigdy nie zostala tknieta - nadal 'approved'.
    status_4 = (
        await db_session.execute(
            text("SELECT status::TEXT FROM intake_item WHERE id = :id"),
            {"id": item_4},
        )
    ).scalar_one()
    assert status_4 == "approved"


async def test_publish_batch_nieistniejacej_partii_rzuca_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeNotFoundError):
        await intake.publish_batch(db_session, 999_999)


# --------------------------------------------------------------------------
# preview_publish_item
# --------------------------------------------------------------------------


async def test_preview_publish_item_dziala_dla_pozycji_pending(
    db_session: AsyncSession,
) -> None:
    """Sprawdza dostepnosc podgladu dla pozycji w statusie innym niz 'approved'.

    W odroznieniu od `publish_item`, `preview_publish_item` NIE wymaga
    statusu 'approved' - ma umozliwiac diagnoze problemu PRZED
    zatwierdzeniem. Zadnego mocka OLX nie potrzeba - podglad nie wykonuje
    zadnego wywolania OLX (patrz test ponizej o braku ad_delivery).
    """
    _, item_id = await _create_pending_item(db_session, platform_code="xbox360")

    payload = await intake.preview_publish_item(db_session, item_id)

    assert payload["category_id"] == 2273  # microsoft, patrz 0005
    assert {"code": "state", "value": "used"} in payload["attributes"]


async def test_preview_publish_item_bez_ad_delivery_i_auto_extend(
    db_session: AsyncSession,
) -> None:
    """Sprawdza brak pol odrzucanych przez POST /adverts w podgladzie.

    Podglad ma pokazywac dokladnie to, co poszloby do OLX (patrz
    olx.build_advert_payload).
    """
    _, item_id = await _create_pending_item(db_session, platform_code="xbox360")

    payload = await intake.preview_publish_item(db_session, item_id)

    assert "ad_delivery" not in payload
    assert "auto_extend_enabled" not in payload


async def test_preview_publish_item_nie_publikuje_ani_nie_zapisuje_niczego(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza, ze podglad NIE wywoluje create_advert i NIE tworzy rekordow.

    To sedno trybu podgladu - diagnoza bez zuzywania proby na prawdziwej
    publikacji.
    """
    _, item_id = await _create_pending_item(db_session)

    create_advert = AsyncMock()
    monkeypatch.setattr(intake.olx, "create_advert", create_advert)

    await intake.preview_publish_item(db_session, item_id)

    create_advert.assert_not_called()
    game_count = (
        await db_session.execute(text("SELECT COUNT(*) FROM game"))
    ).scalar_one()
    assert game_count == 0
    listing_count = (
        await db_session.execute(text("SELECT COUNT(*) FROM listing"))
    ).scalar_one()
    assert listing_count == 0
    item_status = (
        await db_session.execute(
            text("SELECT status::TEXT FROM intake_item WHERE id = :id"),
            {"id": item_id},
        )
    ).scalar_one()
    assert item_status == "pending"


async def test_preview_publish_item_uzywa_tych_samych_funkcji_co_publish(
    db_session: AsyncSession,
) -> None:
    """Sprawdza, ze podglad odzwierciedla dokladnie to, co wyslalby publish_item.

    Zbudowany tytul (build_title, z przycinaniem segmentow) musi byc
    identyczny w obu sciezkach - to gwarantuje, ze podglad jest wiarygodna
    diagnostyka.
    """
    _, item_id = await _create_pending_item(
        db_session, title="Medal of Honor Airborne", platform_code="xbox360"
    )

    payload = await intake.preview_publish_item(db_session, item_id)

    assert payload["title"] == (
        "Medal of Honor Airborne | Xbox 360 | Sklep | Kraków | Wysyłka"
    )


async def test_preview_publish_item_platforma_bez_kategorii_rzuca_blad(
    db_session: AsyncSession,
) -> None:
    """Sprawdza, ze podglad odmawia dla platformy bez ustalonej kategorii.

    "other" nie ma ustalonej kategorii - podglad tez ma odmowic czytelnym
    bledem, tak samo jak publish_item.
    """
    _, item_id = await _create_pending_item(db_session, platform_code="other")

    with pytest.raises(intake.IntakeValidationError, match="kategorii OLX"):
        await intake.preview_publish_item(db_session, item_id)


async def test_preview_publish_item_nieistniejacej_pozycji_rzuca_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeNotFoundError):
        await intake.preview_publish_item(db_session, 999_999)


async def test_preview_publish_item_bez_tytulu_rzuca_blad(
    db_session: AsyncSession,
) -> None:
    _, item_id = await _create_pending_item(db_session, title=None)

    with pytest.raises(intake.IntakeValidationError, match="tytulu"):
        await intake.preview_publish_item(db_session, item_id)


# --------------------------------------------------------------------------
# sync_advert_status
# --------------------------------------------------------------------------


async def _publish_with_status(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, *, olx_status: str
) -> int:
    """Publikuje pozycje (create_advert zamockowane na `olx_status`).

    Returns:
        listing_id opublikowanej oferty.
    """
    _, item_id = await _create_approved_item(db_session)
    monkeypatch.setattr(intake.olx, "get_access_token", AsyncMock(return_value="AT-1"))
    monkeypatch.setattr(
        intake.olx,
        "create_advert",
        AsyncMock(return_value={"id": 987654, "status": olx_status}),
    )
    published = await intake.publish_item(db_session, item_id)
    assert published.listing_id is not None
    return published.listing_id


async def test_sync_advert_status_aktualizuje_status_i_ustawia_posted_at(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sprawdza, ze sync przenosi ogloszenie z 'pending' do 'active'.

    Odtwarza dokladnie zglosszony przypadek: OLX zwrocil przy tworzeniu
    status "new" (u nas 'pending'), a przy odczycie juz "active".
    """
    listing_id = await _publish_with_status(db_session, monkeypatch, olx_status="new")

    monkeypatch.setattr(
        intake.olx,
        "fetch_advert",
        AsyncMock(
            return_value={
                "id": 987654,
                "status": "active",
                "activated_at": "2026-08-26 08:37:06",
                "valid_to": "2026-09-25 08:34:36",
            }
        ),
    )

    result = await intake.sync_advert_status(db_session, listing_id)

    assert result.status == "active"
    assert result.olx_status == "active"
    assert result.posted_at is not None

    row = (
        await db_session.execute(
            text(
                "SELECT status::TEXT, olx_status, posted_at FROM listing WHERE id = :id"
            ),
            {"id": listing_id},
        )
    ).first()
    assert tuple(row[:2]) == ("active", "active")
    assert row[2] is not None


async def test_sync_advert_status_nie_nadpisuje_posted_at_gdy_juz_aktywna(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kolejna synchronizacja juz aktywnej oferty NIE przesuwa posted_at.

    To ten sam, pierwszy moment aktywacji - nie "teraz".
    """
    listing_id = await _publish_with_status(db_session, monkeypatch, olx_status="new")
    monkeypatch.setattr(
        intake.olx,
        "fetch_advert",
        AsyncMock(return_value={"id": 987654, "status": "active"}),
    )
    first = await intake.sync_advert_status(db_session, listing_id)

    second = await intake.sync_advert_status(db_session, listing_id)

    assert second.posted_at == first.posted_at


async def test_sync_advert_status_mapuje_removed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing_id = await _publish_with_status(
        db_session, monkeypatch, olx_status="active"
    )
    monkeypatch.setattr(
        intake.olx,
        "fetch_advert",
        AsyncMock(return_value={"id": 987654, "status": "outdated"}),
    )

    result = await intake.sync_advert_status(db_session, listing_id)

    assert result.status == "removed"
    assert result.olx_status == "outdated"


async def test_sync_advert_status_nieistniejacej_oferty_rzuca_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeNotFoundError):
        await intake.sync_advert_status(db_session, 999_999)


async def test_sync_advert_status_bez_olx_advert_id_rzuca_blad(
    db_session: AsyncSession,
) -> None:
    """Sprawdza wynik dla oferty istniejacej, ale nigdy nie opublikowanej.

    Bez olx_advert_id nie ma czego synchronizowac.
    """
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
                "VALUES (:gid, 'used', 10, 'draft') RETURNING id"
            ),
            {"gid": game_id},
        )
    ).scalar_one()
    await db_session.commit()

    with pytest.raises(intake.IntakeValidationError, match="olx_advert_id"):
        await intake.sync_advert_status(db_session, listing_id)
