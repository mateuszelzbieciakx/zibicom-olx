"""Rozpoznawanie egzemplarzy gier na zdjeciach przez Gemini.

Z produkcyjnego doswiadczenia z poprzedniej wersji narzedzia: model
najczesciej mylil sie w CENIE, czasem w TYTULE. Dlatego prompt wymusza
przyznanie sie do niepewnosci (null + odpowiednia flaga *_confident)
zamiast zgadywania - niepewne pozycje sa potem podswietlane w widoku
zatwierdzania ofert.
"""

from __future__ import annotations

import logging

from google import genai
from google.genai import types

from zibicom.config import get_settings
from zibicom.models import PhotoExtraction, PlatformCode

logger = logging.getLogger(__name__)

PROMPT = """\
Jestes ekspertem od gier wideo. Na zdjeciu jest jeden fizyczny egzemplarz
gry (pudelko/karton) ze sklepu z uzywanymi grami. Wypelnij podany schemat
JSON. Kazde pole wypelniaj OSTROZNIE - brak informacji to zawsze null,
NIGDY zgadywanie. Lepiej przyznac sie do niepewnosci (ustawiajac
odpowiednia flage *_confident na false albo zwracajac null), niz podac
bledna wartosc.

CENA (price_pln, price_confident):
- Odczytaj WYLACZNIE liczbe z naklejonej na pudelko cenowki.
- NIGDY nie zgaduj ceny na podstawie tytulu gry, jej popularnosci ani
  wartosci rynkowej - to najczestszy blad z poprzednich uruchomien.
- Jesli cenowka jest zaslonieta, rozmyta, ucieta lub w ogole niewidoczna,
  zwroc price_pln=null i price_confident=false. Lepszy brak ceny niz cena
  zmyslona.

STAN (condition):
- 'new' TYLKO wtedy, gdy pudelko jest w oryginalnej folii LUB cenowka jest
  POMARANCZOWA.
- Biala cenowka oznacza 'used'.
- W razie jakichkolwiek watpliwosci wybierz 'used' - nowe egzemplarze sa
  rzadkie, a opisanie uzywanej gry jako nowej to podstawa do reklamacji.

TYTUL (title, title_confident):
- Przepisz dokladnie tak, jak jest na okladce.
- BEZ nazwy platformy (np. "PS4", "Xbox") i bez dopiskow typu "PL",
  "Edycja Gry Roku", "Edycja Specjalna" - to nie jest czesc tytulu.
- Przy podobnych wydaniach tej samej serii (np. kolejne roczniki gry
  sportowej, remastery), gdzie latwo pomylic konkretna edycje, ustaw
  title_confident=false, nawet jesli jakis tytul podajesz.

PLATFORMA (platform, platform_other):
- Rozpoznaj po logo producenta i oprawie graficznej pudelka.
- Gry na PC nie wystepuja w tym sklepie - nie uzywaj tej opcji.
- Uwaga na kompatybilnosc wsteczna: plyta z napisem "PS4", ktora dziala
  tez na PS5, to platform=ps4_ps5. Gra WYLACZNIE na PS5 (np. z napisem
  "PS5" lub "wylacznie na PS5") to platform=ps5. Analogicznie
  platform=xboxone_sx (plyta Xbox One dzialajaca tez na Series X/S) vs
  platform=xboxsx (gra wylacznie na Series X/S), oraz platform=switch1_2
  (dziala na obu) vs platform=switch2 (wylacznie Switch 2).
- Jesli platformy nie da sie ustalic albo nie pasuje do zadnego z kodow,
  ustaw platform=other i krotko opisz co widac w platform_other.

IS_FRONT:
- true TYLKO dla glownej okladki: duza grafika i tytul gry.
- Tyl pudelka (opis, logo PEGI, kod kreskowy) i wnetrze (np. plyta,
  instrukcja) to false.
- Gdy nie jestes pewien, ktora to strona - zwroc null, nie zgaduj.

NOTE: krotka uwaga po polsku, jesli cos jest niejasne (np. "cenowka
czesciowo zaslonieta palcem", "mozliwy remaster, nie jestem pewien roku").
Pozostaw null, jesli wszystko jest jasne.
"""


def _client() -> genai.Client:
    """Buduje klienta Gemini na podstawie konfiguracji aplikacji.

    Returns:
        Klient google-genai uwierzytelniony kluczem API z ustawien.
    """
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key.get_secret_value())


def _failure_result(note: str) -> PhotoExtraction:
    """Buduje wynik "nieudanego rozpoznania" o zerowej pewnosci.

    Jedno wadliwe zdjecie (blad sieci, limit API, niepoprawna odpowiedz)
    nie moze wywrocic analizy calej partii - dlatego ta funkcja zwraca
    bezpieczny obiekt zamiast pozwalac wyjatkowi propagowac sie wyzej.

    Args:
        note: Krotki, czytelny opis tego, co poszlo nie tak.

    Returns:
        PhotoExtraction z samymi wartosciami null/false i podana notatka.
    """
    return PhotoExtraction(
        title=None,
        platform=PlatformCode.OTHER,
        platform_other=None,
        price_pln=None,
        condition=None,
        is_front=None,
        title_confident=False,
        price_confident=False,
        note=note,
    )


def recognize_photo(image_bytes: bytes) -> PhotoExtraction:
    """Rozpoznaje jedno znormalizowane zdjecie JPEG przez Gemini.

    Args:
        image_bytes: Bajty znormalizowanego zdjecia JPEG (patrz
            `zibicom.photos.normalize_photo`).

    Returns:
        Wynik rozpoznania. Gdy wywolanie API sie nie powiedzie albo zwroci
        odpowiedz niezgodna ze schematem, zwracany jest bezpieczny wynik
        z flagami pewnosci ustawionymi na false i notatka opisujaca blad -
        funkcja nigdy nie rzuca wyjatkiem.
    """
    settings = get_settings()
    try:
        response = _client().models.generate_content(
            model=settings.gemini_model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                PROMPT,
            ],
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=PhotoExtraction,
            ),
        )
    except Exception:
        logger.exception("Wywolanie Gemini nie powiodlo sie.")
        return _failure_result(
            "Blad wywolania Gemini - zdjecie wymaga recznej weryfikacji."
        )

    parsed = response.parsed
    if not isinstance(parsed, PhotoExtraction):
        logger.error(
            "Gemini zwrocilo odpowiedz niezgodna ze schematem: %r", response.text
        )
        return _failure_result(
            "Nieprawidlowa odpowiedz Gemini - zdjecie wymaga recznej weryfikacji."
        )

    return parsed
