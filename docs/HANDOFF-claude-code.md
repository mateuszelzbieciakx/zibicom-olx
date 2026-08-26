# Handoff — zibicom-olx

Dokument przekazania kontekstu dla nowej sesji. Stan na dziś (patrz też `README.md` i
`CLAUDE.md` — ten plik uzupełnia je o rzeczy, które nie są oczywiste z samego kodu).

## 1. Stan projektu

### Działa end-to-end

Cały pipeline od zdjęcia do promocji w tabelach produkcyjnych jest zaimplementowany i
zweryfikowany na prawdziwych publikacjach (nie tylko testach jednostkowych):

```
zdjęcia → upload do R2 → rozpoznanie Gemini → grupowanie zdjęć w egzemplarze
        → poczekalnia (intake_batch/intake_item/intake_photo)
        → ręczna korekta i zatwierdzenie (approve_item)
        → publikacja na OLX (publish_item → olx.create_advert)
        → promocja do tabel produkcyjnych (game/listing/listing_photo)
        → synchronizacja statusu z OLX (sync_advert_status)
```

Dwie pozycje (`listing` id 54 i 58, gry Medal of Honor Airborne i PES 6 Pro Evolution Soccer,
Xbox 360) zostały realnie opublikowane na OLX przez tę aplikację i po synchronizacji mają
poprawny `status='active'`. Endpoint `GET /api/intake/items/{id}/publish/preview` pozwala
zbudować dokładny payload OLX bez publikacji — przydatne do diagnozowania błędów walidacji bez
zużywania kolejnej próby.

### Co zostało do zrobienia

- **Interfejs HTMX.** Nie istnieje w ogóle — zero szablonów, plików statycznych, jakiegokolwiek
  kodu HTMX/Jinja. Cała aplikacja to obecnie czysty JSON API (FastAPI + Postman collection w
  `postman/`). Ktoś musi zbudować UI od zera.
- **Logika sprzedaży / FIFO w praktyce.** Tabela `sale_event` i indeks `listing_fifo_idx`
  (`WHERE status = 'active'`) istnieją w schemacie od migracji 0001 i **teraz faktycznie
  działają** (status `'active'` jest poprawnie mapowany i synchronizowany — patrz sekcja o
  statusach niżej), ale **nie ma żadnego kodu aplikacyjnego**, który by z nich korzystał. Brakuje
  endpointu "sprzedano stacjonarnie X egzemplarzy gry Y w stanie Z", który zdejmowałby
  najstarszą aktywną ofertę (FIFO) i zapisywał `sale_event`.
- **Import istniejących ~1500 ogłoszeń z OLX.** Sklep ma już duży, realny inwentarz wystawiony
  na OLX poza tą aplikacją. Nie istnieje żaden skrypt/endpoint importu — trzeba będzie pobrać
  listę ogłoszeń konta (prawdopodobnie `GET /adverts` z paginacją, analogicznie do `/cities`) i
  zmapować je na `game`/`listing`, prawdopodobnie half-automatycznie (dopasowanie tytułu/EAN,
  ręczna korekta niejednoznaczności).
- **Katalog EAN.** Kolumna `game.ean` (EAN-8/EAN-13, opcjonalny, `CHECK` w 0001) istnieje w
  schemacie, ale nie ma żadnej logiki w kodzie aplikacyjnym — ani wyszukiwania po EAN, ani
  importu/uzupełniania z zewnętrznej bazy kodów kreskowych. Obecnie to martwe pole.

## 2. Twarde fakty o OLX Partner API

Rzeczy, które kosztowały najwięcej czasu diagnostycznego w tej sesji — zanim spróbujesz czegoś
"oczywistego", sprawdź, czy nie jest tu opisane jako pułapka.

- **Zdjęcia = publiczne URL-e, nie upload binarny.** OLX sam pobiera zdjęcia spod podanych
  URL-i (`image_urls` w `build_advert_payload`) — muszą być już publicznie dostępne (R2), zanim
  wywołasz `create_advert`.
- **Brak środowiska testowego.** Każde wywołanie `POST /adverts` to prawdziwe ogłoszenie na
  koncie firmowym. Do diagnostyki bez zużywania próby służy
  `GET /api/intake/items/{id}/publish/preview` (buduje payload, nie wysyła go).
- **Nagłówek `Version: 2.0` wymagany na Partner API, NIE na OAuth.** Brak tego nagłówka daje
  400 "Missing required 'Version' header". Endpoint tokenu (`/api/open/oauth/token`) go nie
  chce i działa bez niego — stąd w `olx.py` są **dwa** klienty httpx
  (`_http_client`/`_partner_http_client`), nie jeden.
- **Własny `User-Agent` wymagany, inaczej CloudFront blokuje pustym 403.** Domyślny
  `python-httpx/...` jest traktowany jako bot. Ustawiony raz na kliencie (`_DEFAULT_HEADERS`),
  nie per-request.
- **Odpowiedzi opakowane w klucz `"data"`.** Dotyczy WSZYSTKICH endpointów Partner API
  (kategorie, miasta, dzielnice, atrybuty, adverts) — rozpakowanie jest scentralizowane w
  `olx._unwrap_data`.
- **Refresh token rotuje przy KAŻDYM odświeżeniu access tokenu i żyje ok. miesiąc.** Nowa para
  musi być zapisana i **natychmiast** zacommitowana (`olx.get_access_token`), inaczej kolejne
  odświeżenie nie ma już czym się uwierzytelnić i autoryzacja jest bezpowrotnie utracona.
  Dlatego `get_access_token` MUSI być wołane przed jakimikolwiek innymi, jeszcze
  niezacommitowanymi zapisami w tej samej transakcji.
- **`ad_delivery` i `auto_extend_enabled` są TYLKO DO ODCZYTU.** Widoczne w `GET /adverts/{id}`,
  ale `POST`/`PUT /adverts` je odrzucają (400, w przypadku `ad_delivery` nawet bez wskazania
  nazwy pola w treści błędu — ustalone przez porównanie payloadów udanej i odrzuconej
  publikacji). Dostawa i autoprzedłużanie są **świadomie poza zakresem automatyzacji** —
  ustawiane ręcznie masowym narzędziem w panelu OLX. `olx.resolve_delivery_attribute` zostaje w
  kodzie (gotowe na przyszłość, gdyby znalazł się właściwy sposób ustawiania dostawy — pewnie
  osobne wywołanie API, nie pole payloadu tworzenia), ale nie jest wywoływane przy budowaniu
  payloadu.
- **`district_id` wymagany dla miast z podziałem na dzielnice.** Kraków: `city_id=8959`,
  `district_id=271` (Bieżanów-Prokocim, w tym repo używany jako przykładowy). Małe miejscowości
  bez dzielnic NIE mogą go dostać (`build_advert_payload` dołącza go tylko gdy `> 0`).
  `GET /api/olx/cities/{city_id}/districts` do ustalenia wartości dla innych miast.
- **Kategorie gier per producent:** PlayStation `2272`, Xbox `2273`, Nintendo `2274`
  (`platform.olx_category_id`, migracja 0005). **Uwaga:** kategoria `1915` "Konsole" to MEBEL
  (parent 565 = Meble) — łatwo pomylić przy ręcznym przeglądaniu drzewa kategorii.
  `GET /api/olx/categories/search?q=` przeszukuje drzewo rekurencyjnie po liściach
  (`is_leaf=true`) — tylko w nich można wystawić ogłoszenie.
- **Tytuł ogłoszenia: max 70 znaków.** `build_title` układa segmenty w kolejności
  `{tytuł gry} | {platforma} | Sklep | Kraków | Wysyłka | Wymiana` i przy przekroczeniu limitu
  usuwa kolejno opcjonalne segmenty (najpierw `Wymiana`, potem `Wysyłka`) — tytuł gry i
  platforma NIGDY nie są ucinane; jeśli nawet bez obu opcjonalnych segmentów się nie mieści,
  funkcja rzuca błąd zamiast wysyłać coś nieprawidłowego.
- **`PUT` wymaga pełnego payloadu, nie fragmentu.** Nie zaimplementowane w tym repo (na razie
  tylko `POST`/`GET`), ale ważne do zapamiętania, jeśli powstanie funkcja edycji ogłoszenia —
  częściowy `PATCH`-owy payload nie zadziała.

## 3. Reguły biznesowe

- **FIFO po parze (gra, stan).** Kopie tego samego tytułu są wymienne TYLKO w obrębie tej samej
  pary `(game_id, condition)` — nowa i używana mają różne ceny, więc nie są wymienne między
  stanami. `listing_fifo_idx` sortuje po `posted_at` w obrębie tej pary.
- **Cena i stan są per egzemplarz, nie per tytuł.** Żyją na `listing`, nie na `game` — dwie
  fizyczne kopie tej samej gry mogą mieć różne ceny (np. różny stan opakowania) mimo tego
  samego `game_id`.
- **Granica egzemplarza to zdjęcie przodu pudełka, NIE tytuł.** Grupowanie zdjęć
  (`zibicom.grouping`) dzieli serię zdjęć na egzemplarze po wykryciu kolejnego zdjęcia
  frontu (`is_front`) — gdyby dzielić po tytule, dwie kopie tego samego tytułu w jednej partii
  skleiłyby się w jeden rekord poczekalni zamiast zostać rozpoznane jako osobne egzemplarze do
  osobnej wyceny/stanu.

## 4. Pułapki techniczne

- **`SecretStr` wymaga `.get_secret_value()`.** Pola sekretów w `Settings` (`config.py`) są
  typu `pydantic.SecretStr` — bezpośrednie użycie w f-stringu/URL-u da dosłowne `SecretStr('***')`
  zamiast wartości.
- **Klienty httpx/genai twórz raz na proces, nigdy per-wywołanie.** `olx._http_client()`,
  `olx._partner_http_client()` i klient Gemini w `vision.py` są budowane przez `@lru_cache` bez
  argumentów. Tworzenie nowego klienta przy każdym wywołaniu kończy się
  `RuntimeError: Cannot send a request, as the client has been closed` — GC zamyka porzucony
  transport, a kolejne wywołania w tym samym procesie trafiają na już zamknięty klient.
- **Swagger UI nie renderuje uploadu plików przy OpenAPI 3.1.** `POST /api/intake/batches`
  (multipart file upload) trzeba testować przez `curl -F` albo Postman
  (`postman/zibicom-olx.postman_collection.json`), nie przez `/docs`.
- **Testy MUSZĄ używać osobnej bazy — to twarda blokada, nie konwencja.** Patrz `CLAUDE.md` /
  sekcja "Test database" w README. `tests/conftest.py` odmawia uruchomienia, jeśli nazwa bazy
  nie kończy się na `_test`, i sprawdza `SELECT current_database()` tuż przed każdym `DELETE`
  jako drugie, niezależne zabezpieczenie. To nie jest nadmiarowa ostrożność — wcześniej w tym
  projekcie testy faktycznie czyściły produkcyjną bazę, zanim to zabezpieczenie powstało.

## 5. Środowisko

- **Mac M1** — Docker działa normalnie, to na nim toczyła się cała ta sesja (baza w
  `docker-compose`, testy, weryfikacje na żywo przeciw prawdziwemu OLX).
- **Windows** — wirtualizacja zablokowana, Docker tam obecnie nie działa. **Nierozwiązane.**
  `README.md` opisuje `python -m zibicom` jako sposób odpalenia aplikacji bezpośrednio na
  Windowsie (bez kontenera) właśnie z myślą o tym ograniczeniu, ale sama baza Postgres nadal
  wymaga gdzieś działającego Dockera (albo innej instalacji Postgresa) — do ustalenia.
