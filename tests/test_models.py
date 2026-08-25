"""Testy walidacji PhotoExtraction."""

from decimal import Decimal

import pytest

from zibicom.models import PhotoExtraction, PlatformCode


def _extraction(**overrides: object) -> PhotoExtraction:
    defaults: dict[str, object] = {
        "title": "Tytul",
        "platform": PlatformCode.PS4_PS5,
        "title_confident": True,
        "price_confident": True,
    }
    defaults.update(overrides)
    return PhotoExtraction(**defaults)


@pytest.mark.parametrize(
    "price", [Decimal("0"), Decimal("-10"), Decimal("2000.01"), Decimal("50000")]
)
def test_cena_spoza_zakresu_jest_odrzucana(price: Decimal) -> None:
    assert _extraction(price_pln=price).price_pln is None


@pytest.mark.parametrize("price", [Decimal("1"), Decimal("99.99"), Decimal("2000")])
def test_cena_w_zakresie_jest_zachowywana(price: Decimal) -> None:
    assert _extraction(price_pln=price).price_pln == price
