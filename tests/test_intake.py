"""Testy warstwy poczekalni - R2 i Gemini zamockowane, baza jest prawdziwa.

Enumy, CHECK-i i kaskady FK sa czescia kontraktu 0003_intake.sql, wiec te
testy celowo NIE mockuja bazy danych (patrz `db_session` w conftest.py).
"""

from decimal import Decimal
from unittest.mock import AsyncMock

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


# --------------------------------------------------------------------------
# list_items
# --------------------------------------------------------------------------


async def test_list_items_nieistniejacej_partii_rzuca_not_found(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(intake.IntakeNotFoundError):
        await intake.list_items(db_session, 999_999)


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
# publish_item
# --------------------------------------------------------------------------


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
