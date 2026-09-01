"""Testy grupowania zdjec w egzemplarze (dane wejsciowe reczne, bez sieci)."""

from decimal import Decimal

from zibicom.grouping import group_photos, normalize_title
from zibicom.models import PhotoExtraction, PlatformCode


def _photo(**overrides: object) -> PhotoExtraction:
    defaults: dict[str, object] = {
        "title": "God of War",
        "platform": PlatformCode.PS4_PS5,
        "title_confident": True,
        "price_confident": True,
    }
    defaults.update(overrides)
    return PhotoExtraction(**defaults)


def test_dwie_kopie_tego_samego_tytulu_daja_dwie_pozycje() -> None:
    photos = [
        _photo(is_front=True, price_pln=Decimal("120")),
        _photo(is_front=False, title=None, price_pln=None),
        _photo(is_front=True, price_pln=Decimal("140")),
        _photo(is_front=False, title=None, price_pln=None),
    ]

    listings = group_photos(photos)

    assert len(listings) == 2
    assert [listing.price_pln for listing in listings] == [
        Decimal("120"),
        Decimal("140"),
    ]
    assert all(listing.title == "God of War" for listing in listings)


def test_przod_tyl_wnetrze_switcha_to_jedna_pozycja() -> None:
    photos = [
        _photo(platform=PlatformCode.SWITCH1_2, is_front=True),
        _photo(platform=PlatformCode.SWITCH1_2, is_front=False, title=None),
        _photo(platform=PlatformCode.SWITCH1_2, is_front=False, title=None),
    ]

    listings = group_photos(photos)

    assert len(listings) == 1
    assert len(listings[0].photos) == 3


def test_zdjecie_bez_tytulu_dolacza_do_biezacej_grupy() -> None:
    photos = [
        _photo(is_front=True, title="Elden Ring"),
        _photo(is_front=None, title=None),
    ]

    listings = group_photos(photos)

    assert len(listings) == 1
    assert len(listings[0].photos) == 2


def test_brak_ceny_generuje_ostrzezenie() -> None:
    listings = group_photos([_photo(is_front=True, price_pln=None)])

    assert listings[0].warning is not None
    assert "ceny" in listings[0].warning


def test_niepewny_tytul_generuje_ostrzezenie() -> None:
    listings = group_photos(
        [_photo(is_front=True, price_pln=Decimal("100"), title_confident=False)]
    )

    assert listings[0].warning is not None
    assert "tytuł" in listings[0].warning


def test_zdjecie_bez_ceny_nie_obniza_pewnosci_ceny_z_przodu() -> None:
    # Tyl pudelka nie widzi cenowki - jego (z definicji niepewna) flaga nie
    # moze zepsuc pewnosci ceny odczytanej z przodu.
    photos = [
        _photo(is_front=True, price_pln=Decimal("120"), price_confident=True),
        _photo(is_front=False, title=None, price_pln=None, price_confident=False),
    ]

    listings = group_photos(photos)

    assert listings[0].warning is None


def test_niepewna_cena_ze_zrodlowego_zdjecia_generuje_ostrzezenie() -> None:
    photos = [
        _photo(is_front=True, price_pln=Decimal("120"), price_confident=False),
        _photo(is_front=False, title=None, price_pln=None, price_confident=True),
    ]

    listings = group_photos(photos)

    assert listings[0].warning is not None
    assert "cen" in listings[0].warning


def test_zdjecie_bez_tytulu_nie_obniza_pewnosci_tytulu_z_przodu() -> None:
    photos = [
        _photo(
            is_front=True,
            title="God of War",
            title_confident=True,
            price_pln=Decimal("100"),
        ),
        _photo(is_front=False, title=None, title_confident=False),
    ]

    listings = group_photos(photos)

    assert listings[0].warning is None


def test_niepewny_tytul_ze_zrodlowego_zdjecia_generuje_ostrzezenie() -> None:
    photos = [
        _photo(
            is_front=True,
            title="God of War",
            title_confident=False,
            price_pln=Decimal("100"),
        ),
        _photo(is_front=False, title=None, title_confident=True),
    ]

    listings = group_photos(photos)

    assert listings[0].warning is not None
    assert "tytuł" in listings[0].warning


def test_brak_ostrzezenia_gdy_wszystko_pewne() -> None:
    listings = group_photos([_photo(is_front=True, price_pln=Decimal("100"))])

    assert listings[0].warning is None


def test_platforma_wybierana_wiekszoscia_glosow() -> None:
    photos = [
        _photo(is_front=True, platform=PlatformCode.PS4_PS5),
        _photo(is_front=False, title=None, platform=PlatformCode.PS4_PS5),
        _photo(is_front=False, title=None, platform=PlatformCode.PS5),
    ]

    listings = group_photos(photos)

    assert listings[0].platform == PlatformCode.PS4_PS5


def test_inny_tytul_bez_is_front_zaczyna_nowy_egzemplarz() -> None:
    photos = [
        _photo(is_front=None, title="Bloodborne"),
        _photo(is_front=None, title="Sekiro"),
    ]

    listings = group_photos(photos)

    assert len(listings) == 2


def test_normalize_title_usuwa_diakrytyki_interpunkcje_i_wielkosc_liter() -> None:
    assert normalize_title("Wiedźmin 3: Dziki Gon") == "wiedzmin 3 dziki gon"
