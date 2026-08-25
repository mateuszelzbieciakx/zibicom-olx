# zibicom-olx

Narzędzie do synchronizacji inwentarza sklepu z grami wideo (zibicom)
z ogłoszeniami na OLX.

Stan: **schemat bazy + warstwa poczekalni (upload, rozpoznanie AI, ręczne
zatwierdzanie) + integracja z OLX Partner API (autoryzacja, publikacja
zatwierdzonych pozycji)**. FIFO przy sprzedaży stacjonarnej jeszcze nie
istnieje.

## Stack

| Element        | Wersja                          |
| -------------- | ------------------------------- |
| Python         | 3.14                            |
| Menedżer paczek| uv (nigdy pip)                  |
| API            | FastAPI + uvicorn               |
| Baza           | PostgreSQL 18                   |
| ORM            | SQLAlchemy 2.0 + psycopg 3      |
| Testy / lint   | pytest, ruff (line-length 88)   |

## Układ repozytorium

```
migrations/            numerowane migracje SQL (0001_..., 0002_..., 0003_..., 0004_...)
postman/               kolekcja Postman do endpointów poczekalni
src/zibicom/
  config.py            pydantic-settings: /run/secrets + .env
  db.py                silnik i sesje SQLAlchemy
  main.py              FastAPI, /health, podpięcie routerów
  routers.py           endpointy HTTP warstwy poczekalni (intake) i OLX
  intake.py            logika poczekalni: upload, rozpoznanie AI, zatwierdzanie,
                        publikacja (promocja do game/listing/listing_photo)
  photos.py            normalizacja zdjęć oraz upload/download/delete w R2
  vision.py            rozpoznawanie egzemplarzy na zdjęciach przez Gemini
  grouping.py          grupowanie rozpoznanych zdjęć w egzemplarze
  models.py            modele Pydantic wyniku rozpoznania AI
  crypto.py            szyfrowanie tokenów OLX kluczem Fernet
  olx.py               integracja z OLX Partner API: autoryzacja (OAuth
                        półręczny), odświeżanie tokenu, publikacja ogłoszeń
tests/                 pytest, fixtures w conftest.py
secrets/               pliki sekretów (ignorowane przez git)
```

## Konfiguracja i sekrety

`Settings` czyta z trzech źródeł, w kolejności ważności:

1. zmienne środowiskowe,
2. plik `.env` (lokalny development na Windowsie),
3. pliki w `/run/secrets` (Docker Secrets na VPS).

Nazwa pliku sekretu odpowiada nazwie pola, więc `secrets/postgres_password.txt`
trafia do kontenera jako `/run/secrets/postgres_password` i zasila pole
`postgres_password`. Ten sam plik obsługuje bazę przez `POSTGRES_PASSWORD_FILE`,
dzięki czemu hasło nie pojawia się w żadnej zmiennej środowiskowej.

Katalog `/run/secrets` jest wykrywany dynamicznie — poza Dockerem po prostu nie
istnieje i pydantic-settings go pomija.

### Start lokalny (Windows)

```powershell
uv sync
Copy-Item .env.example .env    # uzupełnij hasło
uv run python -m zibicom
```

> **Dlaczego `python -m zibicom`, a nie `uvicorn ...`?**
> psycopg 3 nie potrafi pracować na `ProactorEventLoop`, który uvicorn wybiera
> domyślnie na Windowsie (`InterfaceError: Psycopg cannot use the
> 'ProactorEventLoop'`). `src/zibicom/__main__.py` wymusza `SelectorEventLoop`
> przez `loop_factory` — bez sięgania po API polityk pętli, które znika
> w Pythonie 3.16. Na Linuksie zachowanie jest niezmienione, więc kontener
> używa tego samego wejścia.
> Ta sama pułapka dotknie testy integracyjne z bazą na Windowsie — będą
> potrzebowały pętli selektorowej (fixture w `conftest.py`).

### Start w Dockerze (VPS)

```bash
mkdir -p secrets
printf '%s' 'silne-haslo' > secrets/postgres_password.txt
chmod 600 secrets/postgres_password.txt
docker compose up -d --build
```

Migracje z `migrations/` są montowane w `/docker-entrypoint-initdb.d` i wykonują
się alfabetycznie **przy pierwszej inicjalizacji** wolumenu bazy. Kolejne pliki
(`0002_...`) na istniejącej bazie trzeba puścić ręcznie:

```bash
docker compose exec -T db psql -U zibicom -d zibicom -v ON_ERROR_STOP=1 < migrations/0004_olx_token.sql
```

Wolumen danych montowany jest na `/var/lib/postgresql` — Postgres 18 zmienił
układ katalogów względem wcześniejszych wersji.

## Schemat bazy

- `platform` — słownik platform (bez PC), z `olx_attribute_value` pod API OLX.
- `game` — katalog produktu: tytuł, platforma, EAN (8 lub 13 cyfr, opcjonalny).
- `listing` — jedna oferta OLX = jeden fizyczny egzemplarz.
- `listing_photo` — zdjęcia oferty, unikalna pozycja w obrębie oferty.
- `sale_event` — sprzedaż (kanał `in_store` albo `olx`).
- `olx_operation` — audyt wywołań API OLX (publikacja, wymiana/odświeżenie tokenu).
- `olx_token` — singleton (id=1) z tokenami OAuth OLX, zaszyfrowanymi Fernetem
  (`zibicom.crypto`) — klucz szyfrujący jest sekretem i nie trafia do bazy.
- `intake_batch` / `intake_item` / `intake_photo` — poczekalnia (staging):
  wynik rozpoznania AI jest niepewny (model myli ceny i czasem tytuły), więc
  ląduje tu, a nie od razu w `game`/`listing` — dopóki człowiek nie zatwierdzi
  pozycji. Publikacja (`POST /api/intake/items/{id}/publish`) promuje
  zatwierdzoną pozycję do `game`/`listing`/`listing_photo` w jednej transakcji
  i uzupełnia `intake_item.listing_id`.

Konwencje: 3NF, soft delete (`is_active`), `created_at`/`updated_at` TIMESTAMPTZ,
przy czym `updated_at` ustawia wspólny trigger `set_updated_at()` — kod
aplikacyjny nigdy nie dotyka tej kolumny.

### FIFO

Sklep trzyma po kilka kopii tego samego tytułu. Kopie są wymienne **tylko
w obrębie pary (gra, stan)** — nowa i używana mają różne ceny. Gdy gra sprzeda
się stacjonarnie, zdejmujemy najstarszą aktywną ofertę dla tej pary:

```sql
CREATE INDEX listing_fifo_idx
    ON listing (game_id, condition, posted_at)
    WHERE status = 'active';
```

EAN celowo **nie jest** duplikowany na `listing` — to złamanie 3NF. FIFO dołącza
do `game` po `game_id`.

## Praca z projektem

```powershell
uv sync                 # zależności
uv run ruff check .     # lint
uv run ruff format .    # formatowanie
uv run pytest           # testy
```

### Testy i baza testowa

Testy (`tests/test_olx.py`, `tests/test_intake.py`) łączą się z **prawdziwym**
Postgresem z `docker-compose` — enumy, CHECK-i i kaskady FK w migracjach nie
są mockowane. Fixture `db_session` (`tests/conftest.py`) czyści dane
(`DELETE FROM ...`) po **każdym** teście, żeby kolejny test startował
z czystym stanem.

To czyszczenie **musi** działać na osobnej bazie, nigdy na tej, z której
korzysta aplikacja — inaczej `pytest` kasowałby autoryzację OLX
(`olx_token`) i docelowo inwentarz sklepu (`game`/`listing`) przy każdym
uruchomieniu. Dlatego testy zawsze łączą się z bazą, której nazwa kończy się
na `_test`:

- domyślnie jest to `zibicom_test` (ta sama instancja Postgresa
  z `docker-compose`, osobna baza w tym samym klastrze);
- nazwę można nadpisać zmienną środowiskową `TEST_POSTGRES_DB` — pod
  warunkiem, że i tak kończy się na `_test`.

To druga część zabezpieczenia jest **twardą blokadą, nie konwencją**:
`tests/conftest.py` odmawia uruchomienia testów, jeśli skonfigurowana nazwa
nie kończy się na `_test` (np. pomyłkowe `TEST_POSTGRES_DB=zibicom`), i
dodatkowo sprawdza `SELECT current_database()` tuż przed każdym `DELETE`
w ramach `db_session` — więc nawet błąd w samym kodzie fixture nie
wystarczy, żeby dotknąć bazy aplikacji.

Baza testowa jest tworzona automatycznie (jeśli jeszcze nie istnieje)
i migrowana od zera przy pierwszym uruchomieniu `pytest` w danym klastrze
Postgres — nie trzeba nic robić ręcznie poza `docker compose up -d db`.
Migracje **nie są idempotentne** (pisane do jednorazowego wykonania przez
`docker-entrypoint-initdb.d`), więc po dodaniu nowej migracji do
`migrations/` trzeba usunąć bazę testową, żeby została zmigrowana od nowa:

```bash
docker compose exec db psql -U zibicom -d postgres -c 'DROP DATABASE zibicom_test'
```

## Endpointy

| Metoda | Ścieżka                               | Opis                                                     |
| ------ | -------------------------------------- | --------------------------------------------------------- |
| GET    | `/health`                              | Stan aplikacji i bazy; 503 gdy baza nie odpowiada.         |
| POST   | `/api/intake/batches`                  | Upload zdjęć (multipart) — tworzy nową partię poczekalni. |
| POST   | `/api/intake/batches/{id}/extract`     | Rozpoznanie AI + grupowanie zdjęć w pozycje.               |
| GET    | `/api/intake/batches/{id}/items`       | Lista pozycji partii do zatwierdzenia.                    |
| PATCH  | `/api/intake/items/{id}`               | Ręczna korekta pól pozycji.                                |
| POST   | `/api/intake/items/{id}/approve`       | Zatwierdzenie pozycji (wymaga tytułu i ceny).              |
| POST   | `/api/intake/items/{id}/reject`        | Odrzucenie pozycji.                                        |
| POST   | `/api/intake/items/{id}/publish`       | Publikacja POJEDYNCZEJ zatwierdzonej pozycji na OLX + promocja do game/listing. |
| GET    | `/api/platforms`                       | Słownik platform do listy wyboru.                          |
| GET    | `/api/olx/authorize`                   | URL logowania OAuth OLX do otwarcia w przeglądarce.        |
| POST   | `/api/olx/exchange`                    | Wymiana kodu (`{"code": "..."}` przepisanego z paska adresu) na tokeny. |
| GET    | `/api/olx/status`                      | Stan autoryzacji OLX: czy jest ważna, kiedy wygasa.        |
| GET    | `/api/olx/categories?parent_id=&q=`    | Kategorie OLX na jednym poziomie drzewa (dzieci `parent_id`, domyślnie kategorie główne); `q` filtruje po nazwie w obrębie poziomu. |
| GET    | `/api/olx/categories/search?q=`        | Rekurencyjne wyszukiwanie kategorii-liści (`is_leaf=true`) w całym drzewie — jedyne, w których można wystawić ogłoszenie. |
| GET    | `/api/olx/cities?q=`                   | Wyszukiwanie miast OLX (do ustalenia `olx_city_id`).       |

Autoryzacja OLX jest **półręczna** — OLX nie akceptuje `localhost`, a
zarejestrowany redirect URI (adres w R2) nie ma działającego endpointu.
Trzeba więc: otworzyć URL z `/api/olx/authorize`, zalogować się, przepisać
parametr `code` z paska adresu po przekierowaniu i przesłać go przez
`/api/olx/exchange`. Publikacja partii hurtem świadomie nie istnieje na tym
etapie — `/api/intake/items/{id}/publish` publikuje jedną pozycję na raz.

Kolekcja Postman z endpointami poczekalni, w kolejności workflow, jest
w `postman/zibicom-olx.postman_collection.json`.
