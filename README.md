# zibicom-olx

Synchronizacja inwentarza sklepu z grami wideo (zibicom, Kraków) z ogłoszeniami
na OLX. Zdjęcia pudełek trafiają do rozpoznania AI, człowiek weryfikuje wynik,
zatwierdzone pozycje lądują na OLX przez Partner API.

Aplikacja publikuje prawdziwe ogłoszenia na produkcyjnym koncie firmowym —
OLX nie udostępnia środowiska testowego.

## Co działa

- Pełna pętla przyjęcia towaru w przeglądarce: upload zdjęć → rozpoznanie AI
  w tle → korekta i zatwierdzenie → publikacja na OLX → promocja do tabel
  produkcyjnych.
- Autoryzacja OAuth do OLX z automatycznym odświeżaniem rotującego tokenu.
- Reconciler statusów — OLX aktywuje ogłoszenia asynchronicznie, więc status
  zapisany w chwili publikacji trzeba dosynchronizować.
- JSON API równoległe do interfejsu WWW (te same funkcje serwisowe).

## Czego nie ma

- **Sprzedaż FIFO.** Schemat (`sale_event`) i indeks `listing_fifo_idx`
  istnieją od migracji 0001, brak kodu aplikacyjnego zdejmującego najstarsze
  ogłoszenie przy sprzedaży stacjonarnej.
- **Import istniejących ogłoszeń.** Sklep ma ~1500 ofert wystawionych poza tą
  aplikacją; nie są zmapowane na `game`/`listing`.
- **Katalog EAN.** Kolumna `game.ean` istnieje, ale nie jest używana.
- **Edycja opublikowanych ogłoszeń** (`PUT /adverts` wymaga pełnego payloadu).
- **Uwierzytelnianie panelu** — interfejs zakłada zaufaną sieć lokalną.
- Harmonogram dla reconcilera (wywoływany przyciskiem w GUI).

## Stack

| Element | Wersja |
| --- | --- |
| Python | 3.14 |
| Menedżer paczek | uv (nigdy pip) |
| API | FastAPI + uvicorn |
| Frontend | Jinja2 + HTMX + Tailwind (CDN) |
| Baza | PostgreSQL 18 |
| ORM | SQLAlchemy 2.0 + psycopg 3 |
| Rozpoznanie obrazu | Gemini |
| Hosting zdjęć | Cloudflare R2 (S3-compatible) |
| Testy / lint | pytest, ruff (line-length 88) |

## Uruchomienie

Środowisko robocze: macOS (Apple Silicon), Docker Desktop.

```bash
uv sync
cp .env.example .env                  # uzupełnij wartości
mkdir -p secrets
printf '%s' 'silne-haslo' > secrets/postgres_password.txt
chmod 600 secrets/postgres_password.txt

docker compose up -d db
uv run uvicorn zibicom.main:app --reload
```

Interfejs: <http://localhost:8000/ui/batches>

Przy pierwszym uruchomieniu i po miesiącu bez użycia trzeba odnowić
autoryzację OLX (patrz „Autoryzacja OLX").

### Docker (VPS)

```bash
docker compose up -d --build
```

Migracje z `migrations/` montują się w `/docker-entrypoint-initdb.d` i wykonują
alfabetycznie **przy pierwszej inicjalizacji wolumenu**. Nową migrację na
istniejącej bazie trzeba puścić ręcznie:

```bash
docker compose exec -T db psql -U zibicom -d zibicom -v ON_ERROR_STOP=1 \
    < migrations/0006_olx_attribute_mapping.sql
```

Wolumen danych montowany jest na `/var/lib/postgresql` — Postgres 18 zmienił
układ katalogów względem wcześniejszych wersji.

### Windows

Wirtualizacja jest zablokowana na maszynie deweloperskiej (`HypervisorPresent`
pozostaje `False` mimo spełnionych wymagań Hyper-V) — Docker tam nie startuje,
problem nierozwiązany. Aplikację można uruchomić bez kontenera przez
`uv run python -m zibicom`, ale baza nadal wymaga Postgresa z innego źródła.

Osobne wejście `__main__.py` istnieje, bo psycopg 3 nie działa na
`ProactorEventLoop`, który uvicorn wybiera domyślnie na Windowsie
(`InterfaceError: Psycopg cannot use the 'ProactorEventLoop'`). Wymusza
`SelectorEventLoop` przez `loop_factory`, bez API polityk pętli usuwanego
w Pythonie 3.16. Na Linuksie zachowanie jest identyczne, więc kontener
korzysta z tego samego wejścia.

## Konfiguracja i sekrety

`Settings` czyta z trzech źródeł, w kolejności ważności: zmienne środowiskowe,
plik `.env`, pliki w `/run/secrets`. Nazwa pliku odpowiada nazwie pola —
`secrets/postgres_password.txt` trafia do kontenera jako
`/run/secrets/postgres_password` i zasila `postgres_password`. Ten sam plik
obsługuje bazę przez `POSTGRES_PASSWORD_FILE`, więc hasło nie pojawia się
w żadnej zmiennej środowiskowej. Katalog `/run/secrets` wykrywany jest
dynamicznie — poza Dockerem nie istnieje i pydantic-settings go pomija.

Wymagane sekrety: `postgres_password`, `gemini_api_key`, `r2_access_key_id`,
`r2_secret_access_key`, `olx_client_id`, `olx_client_secret`,
`token_encryption_key`.

Zmiana `token_encryption_key` czyni zapisany token OLX nieodczytywalnym
i wymusza ponowną autoryzację.

## Interfejs WWW

`src/zibicom/web/` — HTMX + Jinja2, równoległy do JSON API, wywołujący te same
funkcje serwisowe w `intake.py`. Logika biznesowa nie istnieje w dwóch kopiach.

1. `/ui/batches` — lista partii i formularz uploadu. Zwykły multipart POST
   z przekierowaniem 303, żeby odświeżenie strony nie powtórzyło wgrywania.
2. „Rozpoznaj" uruchamia rozpoznanie AI w tle (`BackgroundTasks`) i zwraca
   pasek postępu odpytywany co 2 sekundy. Wywołanie synchroniczne zerwałoby
   połączenie — rozpoznanie dużej partii trwa minuty.
3. Karty pozycji pozwalają poprawić tytuł, cenę, stan i platformę, a następnie
   zatwierdzić albo odrzucić pozycję. Błąd walidacji renderuje się w karcie
   ze statusem 200 — HTMX podmienia DOM wyłącznie przy odpowiedziach 2xx, więc
   `HTTPException` dałoby martwy przycisk bez komunikatu.
4. Zatwierdzona pozycja odsłania „Publikuj". Publikacja jest nieodwracalna,
   więc wymaga potwierdzenia w przeglądarce.

Przycisk „Odśwież statusy OLX" w nagłówku odpytuje OLX dla ofert czekających
na aktywację i aktualizuje ich status lokalnie.

## Schemat bazy

- `platform` — słownik platform (bez PC), z mapowaniem na kategorie
  i atrybuty OLX.
- `game` — katalog produktu: tytuł, platforma, EAN (8 lub 13 cyfr, opcjonalny).
- `listing` — jedna oferta OLX = jeden fizyczny egzemplarz.
- `listing_photo` — zdjęcia oferty, unikalna pozycja w obrębie oferty.
- `sale_event` — sprzedaż (kanał `in_store` albo `olx`).
- `olx_operation` — audyt wywołań API OLX; przechowuje wysłany payload
  i surową odpowiedź, co pozwala zdiagnozować odrzucenie przez porównanie
  z podglądem z `/publish/preview`.
- `olx_token` — singleton z tokenami OAuth zaszyfrowanymi Fernetem; klucz
  szyfrujący jest sekretem i nie trafia do bazy.
- `intake_batch` / `intake_item` / `intake_photo` — poczekalnia. Wynik
  rozpoznania AI bywa niepewny, więc nie trafia od razu do `game`/`listing`.
  Publikacja promuje zatwierdzoną pozycję do tabel produkcyjnych w jednej
  transakcji — nie może powstać ogłoszenie na OLX bez rekordu w bazie.

Konwencje: 3NF, soft delete (`is_active`), `created_at`/`updated_at`
TIMESTAMPTZ, przy czym `updated_at` ustawia wspólny trigger `set_updated_at()`.
Kod aplikacyjny nigdy nie dotyka tej kolumny.

### FIFO (schemat gotowy, logika do napisania)

Sklep trzyma po kilka kopii tego samego tytułu. Kopie są wymienne wyłącznie
w obrębie pary `(gra, stan)` — nowa i używana mają różne ceny. Przy sprzedaży
stacjonarnej należy zdjąć najstarszą aktywną ofertę tej pary:

```sql
CREATE INDEX listing_fifo_idx
    ON listing (game_id, condition, posted_at)
    WHERE status = 'active';
```

EAN celowo nie jest duplikowany na `listing` — FIFO dołącza do `game`
po `game_id`.

## Reguły rozpoznawania

- **Granica egzemplarza to zdjęcie przodu pudełka**, nie zmiana tytułu.
  Dwie kopie tej samej gry sfotografowane po kolei mają identyczny tytuł —
  grupowanie po tytule scaliłoby je w jedną pozycję i zgubiło drugie
  ogłoszenie razem z ceną. Konsekwencja operacyjna: fotografuj egzemplarze
  po kolei, zawsze zaczynając od przodu. Liczba zdjęć na egzemplarz jest
  dowolna (2 dla PlayStation i Xbox, 3+ dla Switcha i steelbooków).
- Cena pochodzi **wyłącznie z naklejonej cenówki**, nigdy z wartości
  rynkowej tytułu.
- Stan `new` tylko przy folii lub pomarańczowej cenówce; biała oznacza
  `used`. W razie wątpliwości `used` — opisanie używanej jako nowej kończy się
  reklamacją.
- Tytuł i cena mają osobne flagi pewności; pewność bierze się wyłącznie ze
  zdjęcia, które daną wartość dostarczyło (tył pudełka nie widzi cenówki,
  więc jego flagi nie mogą obniżać pewności odczytu z przodu).

## Endpointy

| Metoda | Ścieżka | Opis |
| --- | --- | --- |
| GET | `/health` | Stan aplikacji i bazy; 503 gdy baza nie odpowiada. |
| GET | `/api/intake/batches` | Lista partii z licznikami pozycji i statusów. |
| POST | `/api/intake/batches` | Upload zdjęć (multipart) — tworzy partię. |
| POST | `/api/intake/batches/{id}/extract` | Rozpoznanie AI i grupowanie w pozycje. |
| GET | `/api/intake/batches/{id}/items` | Pozycje partii do zatwierdzenia. |
| PATCH | `/api/intake/items/{id}` | Ręczna korekta pól pozycji. |
| POST | `/api/intake/items/{id}/approve` | Zatwierdzenie (wymaga tytułu i ceny). |
| POST | `/api/intake/items/{id}/reject` | Odrzucenie pozycji. |
| GET | `/api/intake/items/{id}/publish/preview` | Payload OLX bez wysyłki — diagnostyka bez zużywania próby. |
| POST | `/api/intake/items/{id}/publish` | Publikacja na OLX i promocja do `game`/`listing`. |
| POST | `/api/listings/sync-pending` | Reconciler: odpytuje OLX o oferty czekające na aktywację. |
| POST | `/api/listings/{id}/sync-status` | Odświeżenie statusu pojedynczej oferty. |
| GET | `/api/platforms` | Słownik platform. |
| GET | `/api/olx/authorize` | URL logowania OAuth do otwarcia w przeglądarce. |
| POST | `/api/olx/exchange` | Wymiana kodu na tokeny. |
| GET | `/api/olx/status` | Stan autoryzacji: ważność, data wygaśnięcia. |
| GET | `/api/olx/categories` | Kategorie na jednym poziomie drzewa. |
| GET | `/api/olx/categories/search` | Rekurencyjne wyszukiwanie kategorii-liści. |
| GET | `/api/olx/categories/{id}/attributes` | Atrybuty kategorii (wymagane i opcjonalne). |
| GET | `/api/olx/cities` | Wyszukiwanie miast. |
| GET | `/api/olx/cities/{id}/districts` | Dzielnice miasta (puste dla małych miejscowości). |

Kolekcja Postman w `postman/zibicom-olx.postman_collection.json`.

## Autoryzacja OLX

Półręczna, wymagana raz na miesiąc. OLX nie akceptuje `localhost` jako
redirect URI, a zarejestrowany adres (bucket R2) nie ma działającego
endpointu — kod przepisuje się z paska adresu.

```bash
curl -s http://localhost:8000/api/olx/authorize      # otwórz zwrócony URL
curl -s -X POST http://localhost:8000/api/olx/exchange \
     -H "Content-Type: application/json" -d '{"code":"..."}'
curl -s http://localhost:8000/api/olx/status
```

Refresh token rotuje przy każdym odświeżeniu access tokenu i żyje około
miesiąca. Nowa para musi zostać zapisana i natychmiast zacommitowana —
w przeciwnym razie kolejne odświeżenie nie ma czym się uwierzytelnić
i autoryzacja przepada bezpowrotnie. Z tego samego powodu publikacje
wykonywane są sekwencyjnie: dwa równoległe odświeżenia unieważniłyby się
nawzajem.

## Rozwój

```bash
uv sync
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pytest -q
```

Zmiany schematu wyłącznie przez numerowane migracje SQL. Wcześniejszych
migracji nie modyfikujemy. Bez `DROP`/`DELETE` na danych — soft delete
przez `is_active`.

### Baza testowa

Testy łączą się z prawdziwym Postgresem z `docker-compose` — enumy, CHECK-i
i kaskady FK nie są mockowane. Fixture `db_session` czyści dane po każdym
teście.

Czyszczenie musi działać na osobnej bazie, nigdy na tej używanej przez
aplikację: wcześniej w tym projekcie `pytest` realnie kasował autoryzację OLX
przy każdym uruchomieniu. Zabezpieczenie jest dwuwarstwowe i twarde —
`conftest.py` odmawia startu, jeśli nazwa bazy nie kończy się na `_test`,
i niezależnie sprawdza `SELECT current_database()` tuż przed każdym `DELETE`.

Domyślnie `zibicom_test`, do nadpisania przez `TEST_POSTGRES_DB` (nadal musi
kończyć się na `_test`). Baza tworzona jest automatycznie i migrowana od zera
przy pierwszym uruchomieniu. Migracje nie są idempotentne, więc po dodaniu
nowej trzeba usunąć bazę testową:

```bash
docker compose exec db psql -U zibicom -d postgres -c 'DROP DATABASE zibicom_test'
```

## Układ repozytorium

```
migrations/          numerowane migracje SQL (0001–0006)
postman/             kolekcja Postman w kolejności workflow
src/zibicom/
  config.py          pydantic-settings: /run/secrets + .env
  db.py              silnik i sesje SQLAlchemy
  main.py            FastAPI, /health, podpięcie routerów
  routers.py         JSON API: poczekalnia, listingi, OLX
  intake.py          logika poczekalni, publikacja, reconciler statusów
  photos.py          normalizacja zdjęć, upload/download/delete w R2
  vision.py          rozpoznawanie egzemplarzy przez Gemini
  grouping.py        grupowanie zdjęć w egzemplarze po is_front
  models.py          modele Pydantic wyniku rozpoznania
  crypto.py          szyfrowanie tokenów OLX (Fernet)
  olx.py             OLX Partner API: OAuth, publikacja, słowniki
  web/               interfejs WWW (router + szablony Jinja2)
tests/               pytest, fixtures w conftest.py
secrets/             pliki sekretów (ignorowane przez git)
```

## Znane pułapki OLX Partner API

Ustalone empirycznie — dokumentacja ich nie opisuje.

- Nagłówek `Version: 2.0` wymagany na Partner API, ale **nie** na OAuth.
- Własny `User-Agent` obowiązkowy; domyślny `python-httpx` dostaje puste 403
  od CloudFronta.
- Odpowiedzi opakowane w klucz `data`.
- `POST /adverts` zwraca zwykle status `disabled`; aktywacja następuje
  asynchronicznie po kilku minutach. Dlatego istnieje reconciler — bez niego
  oferta zostaje w bazie jako nieaktywna i jest niewidoczna dla FIFO.
- `ad_delivery` i `auto_extend_enabled` są tylko do odczytu; `POST`/`PUT` je
  odrzucają (`ad_delivery` pustym 400 bez wskazania pola). Dostawa
  i autoprzedłużanie ustawiane są ręcznie masowym narzędziem OLX.
- Tytuł maksymalnie 70 znaków.
- `district_id` wymagany dla miast z podziałem na dzielnice.
- Kategoria 1915 „Konsole" należy do drzewa mebli — właściwe kategorie gier
  to 2272 (PlayStation), 2273 (Xbox), 2274 (Nintendo).
- Limit 4500 żądań na 5 minut; bump ogłoszenia nie częściej niż raz na 14 dni.
