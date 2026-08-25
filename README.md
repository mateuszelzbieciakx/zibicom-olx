# zibicom-olx

Narzędzie do synchronizacji inwentarza sklepu z grami wideo (zibicom)
z ogłoszeniami na OLX.

Stan: **szkielet projektu + schemat bazy**. Logika aplikacyjna (klient API OLX,
import inwentarza, FIFO przy sprzedaży stacjonarnej) jeszcze nie istnieje.

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
migrations/            numerowane migracje SQL (0001_..., 0002_...)
src/zibicom/
  config.py            pydantic-settings: /run/secrets + .env
  db.py                silnik i sesje SQLAlchemy
  main.py              FastAPI, /health
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
docker compose exec -T db psql -U zibicom -d zibicom < migrations/0002_....sql
```

Wolumen danych montowany jest na `/var/lib/postgresql` — Postgres 18 zmienił
układ katalogów względem wcześniejszych wersji.

## Schemat bazy

- `platform` — słownik platform (bez PC), z `olx_attribute_value` pod API OLX.
- `game` — katalog produktu: tytuł, platforma, EAN (8 lub 13 cyfr, opcjonalny).
- `listing` — jedna oferta OLX = jeden fizyczny egzemplarz.
- `listing_photo` — zdjęcia oferty, unikalna pozycja w obrębie oferty.
- `sale_event` — sprzedaż (kanał `in_store` albo `olx`).
- `olx_operation` — audyt wywołań API OLX.

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

## Endpointy

| Metoda | Ścieżka   | Opis                                                |
| ------ | --------- | --------------------------------------------------- |
| GET    | `/health` | Stan aplikacji i bazy; 503 gdy baza nie odpowiada.   |
