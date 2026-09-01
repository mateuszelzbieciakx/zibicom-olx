"""Rozpoznawanie egzemplarzy gier na zdjęciach przez Gemini.

Z produkcyjnego doświadczenia z poprzedniej wersji narzędzia: model
najczęściej mylił się w CENIE, czasem w TYTULE. Dlatego prompt wymusza
przyznanie się do niepewności (null + odpowiednia flaga *_confident)
zamiast zgadywania - niepewne pozycje są potem podświetlane w widoku
zatwierdzania ofert.
"""

from __future__ import annotations

import io
import logging
import time
from functools import lru_cache

from google import genai
from google.genai import errors, types
from PIL import Image

from zibicom.config import get_settings
from zibicom.models import PhotoExtraction, PlatformCode

logger = logging.getLogger(__name__)

RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10.0
GEMINI_FREE_TIER_DAILY_LIMIT = 20

PROMPT = """\
Jesteś ekspertem od gier wideo. Na zdjęciu jest jeden fizyczny egzemplarz
gry (pudełko/karton) ze sklepu z używanymi grami. Wypełnij podany schemat
JSON. Każde pole wypełniaj OSTROŻNIE - brak informacji to zawsze null,
NIGDY zgadywanie. Lepiej przyznać się do niepewności (ustawiając
odpowiednią flagę *_confident na false albo zwracając null), niż podać
błędną wartość.

CENA (price_pln, price_confident):
- Odczytaj WYŁĄCZNIE liczbę z naklejonej na pudełko cenówki.
- NIGDY nie zgaduj ceny na podstawie tytułu gry, jej popularności ani
  wartości rynkowej - to najczęstszy błąd z poprzednich uruchomień.
- Jeśli cenówka jest zasłonięta, rozmyta, ucięta lub w ogóle niewidoczna,
  zwróć price_pln=null i price_confident=false. Lepszy brak ceny niż cena
  zmyślona.

STAN (condition):
- 'new' TYLKO wtedy, gdy pudełko jest w oryginalnej folii LUB cenówka jest
  POMARAŃCZOWA.
- Biała cenówka oznacza 'used'.
- W razie jakichkolwiek wątpliwości wybierz 'used' - nowe egzemplarze są
  rzadkie, a opisanie używanej gry jako nowej to podstawa do reklamacji.

TYTUŁ (title, title_confident):
- Przepisz dokładnie tak, jak jest na okładce.
- BEZ nazwy platformy (np. "PS4", "Xbox") i bez dopisków typu "PL",
  "Edycja Gry Roku", "Edycja Specjalna" - to nie jest część tytułu.
- Przy podobnych wydaniach tej samej serii (np. kolejne roczniki gry
  sportowej, remastery), gdzie łatwo pomylić konkretną edycję, ustaw
  title_confident=false, nawet jeśli jakiś tytuł podajesz.

PLATFORMA (platform, platform_other):
- Rozpoznaj po logo producenta i oprawie graficznej pudełka.
- Gry na PC nie występują w tym sklepie - nie używaj tej opcji.
- Uwaga na kompatybilność wsteczną: płyta z napisem "PS4", która działa
  też na PS5, to platform=ps4_ps5. Gra WYŁĄCZNIE na PS5 (np. z napisem
  "PS5" lub "wyłącznie na PS5") to platform=ps5. Analogicznie
  platform=xboxone_sx (płyta Xbox One działająca też na Series X/S) vs
  platform=xboxsx (gra wyłącznie na Series X/S), oraz platform=switch1_2
  (działa na obu) vs platform=switch2 (wyłącznie Switch 2).
- Jeśli platformy nie da się ustalić albo nie pasuje do żadnego z kodów,
  ustaw platform=other i krótko opisz co widać w platform_other.

IS_FRONT:
- true TYLKO dla głównej okładki: duża grafika i tytuł gry.
- Tył pudełka (opis, logo PEGI, kod kreskowy) i wnętrze (np. płyta,
  instrukcja) to false.
- Gdy nie jesteś pewien, która to strona - zwróć null, nie zgaduj.

NOTE: krótka uwaga po polsku, jeśli coś jest niejasne (np. "cenówka
częściowo zasłonięta palcem", "możliwy remaster, nie jestem pewien roku").
Pozostaw null, jeśli wszystko jest jasne.
"""


@lru_cache
def _client() -> genai.Client:
    """Buduje (raz na proces) klienta Gemini na podstawie konfiguracji aplikacji.

    Klient google-genai trzyma pod spodem współdzielony transport httpx.
    Tworzenie nowej instancji przy każdym wywołaniu prowadzi do sytuacji, w
    której GC zbiera porzuconą instancję i zamyka jej transport - kolejne
    zadania (np. rozpoznawanie kolejnych zdjęć partii w tym samym procesie)
    trafiają wtedy na zamknięte połączenie ("Cannot send a request, as the
    client has been closed"). `lru_cache` bez argumentów trzyma jedną,
    współdzieloną instancję przez cały czas życia procesu - klient
    google-genai jest bezpieczny do wielokrotnego użycia.

    Returns:
        Klient google-genai uwierzytelniony kluczem API z ustawień.
    """
    settings = get_settings()
    return genai.Client(api_key=settings.gemini_api_key.get_secret_value())


class GeminiQuotaExceededError(Exception):
    """Wyczerpany dzienny limit darmowego tieru Gemini API.

    W przeciwieństwie do przejściowych błędów sieciowych, ponawianie próby
    nic tu nie da - limit odnawia się raz na dobę. Dlatego ten wyjątek NIE
    jest wewnętrznie tłumiony (w odróżnieniu od reszty błędów API) i
    propaguje się aż do wywołującego, przerywając analizę całej partii,
    zamiast bić się o limit przy każdym kolejnym zdjęciu.
    """


def _failure_result(note: str) -> PhotoExtraction:
    """Buduje wynik "nieudanego rozpoznania" o zerowej pewności.

    Jedno wadliwe zdjęcie (błąd sieci, limit API, niepoprawna odpowiedź)
    nie może wywrócić analizy całej partii - dlatego ta funkcja zwraca
    bezpieczny obiekt zamiast pozwalać wyjątkowi propagować się wyżej.

    Args:
        note: Krótki, czytelny opis tego, co poszło nie tak.

    Returns:
        PhotoExtraction z samymi wartościami null/false i podaną notatką.
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


def _generate_content(image: Image.Image, model: str) -> types.GenerateContentResponse:
    """Woła Gemini o rozpoznanie zdjęcia, ponawiając przejściowe błędy.

    Kliencki błąd 429 (limit) NIE jest traktowany jako przejściowy -
    czekanie nic tu nie da (limit odnawia się raz na dobę), więc leci od
    razu, bez zużywania prób. Inne błędy klienta (4xx - np. złe żądanie)
    też nie są ponawiane z tego samego powodu: są deterministyczne, kolejna
    próba zwróci to samo. Ponawiane są wyłącznie błędy przejściowe (błąd
    serwera, przerwane połączenie itp.) - do `RETRY_ATTEMPTS` prób,
    z `RETRY_DELAY_SECONDS` przerwy między nimi.

    Args:
        image: Znormalizowane zdjęcie do rozpoznania.
        model: Nazwa modelu Gemini z konfiguracji.

    Returns:
        Surowa odpowiedź Gemini.

    Raises:
        GeminiQuotaExceededError: Gdy API zwróciło 429 (dzienny limit).
        errors.ClientError: Gdy API zwróciło inny błąd 4xx (nie ponawiane).
        Exception: Ostatni napotkany błąd, gdy wszystkie próby zawiodły.
    """
    last_exc: Exception
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return _client().models.generate_content(
                model=model,
                contents=[PROMPT, image],
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                    response_schema=PhotoExtraction,
                ),
            )
        except errors.ClientError as exc:
            if exc.code == 429:
                raise GeminiQuotaExceededError(
                    "Wyczerpano dzienny limit darmowego tieru Gemini "
                    f"({GEMINI_FREE_TIER_DAILY_LIMIT} zdjęć/dobę). Włącz płatny "
                    "tier w Google Cloud, żeby kontynuować rozpoznawanie."
                ) from exc
            raise
        except Exception as exc:  # retry na każdym innym (przejściowym) błędzie
            last_exc = exc
            logger.warning(
                "Wywołanie Gemini nie powiodło się (próba %d/%d): %s",
                attempt,
                RETRY_ATTEMPTS,
                exc,
            )
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS)

    raise last_exc


def recognize_photo(image_bytes: bytes) -> PhotoExtraction:
    """Rozpoznaje jedno znormalizowane zdjęcie JPEG przez Gemini.

    Args:
        image_bytes: Bajty znormalizowanego zdjęcia JPEG (patrz
            `zibicom.photos.normalize_photo`).

    Returns:
        Wynik rozpoznania. Gdy wywołanie API się nie powiedzie (po
        wyczerpaniu prób) albo zwróci odpowiedź niezgodną ze schematem,
        zwracany jest bezpieczny wynik z flagami pewności ustawionymi na
        false i notatką opisującą błąd.

    Raises:
        GeminiQuotaExceededError: Gdy wyczerpano dzienny limit darmowego
            tieru Gemini - w odróżnieniu od innych błędów, TEN wyjątek
            propaguje się aż do wywołującego, żeby przerwać analizę całej
            partii zamiast zderzać się z limitem przy każdym zdjęciu.
    """
    settings = get_settings()
    image = Image.open(io.BytesIO(image_bytes))

    try:
        response = _generate_content(image, settings.gemini_model)
    except GeminiQuotaExceededError:
        raise
    except Exception:
        logger.exception("Wywołanie Gemini nie powiodło się.")
        return _failure_result(
            "Błąd wywołania Gemini - zdjęcie wymaga ręcznej weryfikacji."
        )

    parsed = response.parsed
    if not isinstance(parsed, PhotoExtraction):
        logger.error(
            "Gemini zwróciło odpowiedź niezgodną ze schematem: %r", response.text
        )
        return _failure_result(
            "Nieprawidłowa odpowiedź Gemini - zdjęcie wymaga ręcznej weryfikacji."
        )

    return parsed
