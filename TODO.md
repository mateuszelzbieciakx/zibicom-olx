# TODO

Bugi/rozbieżności znalezione poza zakresem bieżącego zadania — do naprawy we właściwym zadaniu,
nie „przy okazji".

## Formatowanie (ruff format) — poza zakresem ZADANIA 0

`uv run ruff format --check .` zgłasza 2 pliki niesformatowane, oba w kodzie niezwiązanym
z ZADANIEM 0 (istniały przed nim, commit `2b2c438`):

- `src/zibicom/intake.py:1202` — wielolinijkowe `logger.exception(...)` w
  `sync_pending_listings`, które mieściłoby się teraz w jednej linii.
- `src/zibicom/routers.py:303` — wywołanie `intake.sync_pending_listings(...)` w endpointzie
  reconcilera, analogicznie.

Do naprawienia (`uv run ruff format src/zibicom/intake.py src/zibicom/routers.py`) w zadaniu,
które dotyka tych plików — nie zrobiłem tego teraz, żeby nie mieszać niepowiązanego
formatowania z commitem ZADANIA 0 (UI karty pozycji).

## Test nieaktualny po zmianie mapowania statusu — poza zakresem ZADANIA 0

`uv run pytest` failuje na `test_map_olx_status_znane_wartosci[disabled-removed]`
(`tests/test_intake.py`). Przyczyna: `_OLX_STATUS_TO_LISTING_STATUS` w `intake.py` (commit
`2b2c438`, "docs: note OLX returns disabled on create, activates asynchronously") świadomie
zmienił mapowanie `"disabled"` z `'removed'` na `'pending'` — OLX zwraca `"disabled"` przejściowo
zaraz po `POST /adverts`, zanim aktywuje ogłoszenie asynchronicznie (reconciler,
`sync_pending_listings`), więc mapowanie na `'removed'` było przedwczesne/błędne. Sama zmiana w
`intake.py` wygląda słusznie — test po prostu nie został zaktualizowany razem z nią.

Do naprawienia: zmienić oczekiwaną wartość w parametrze `("disabled", "removed")` na
`("disabled", "pending")` w `tests/test_intake.py`. Nie zrobiłem tego w ZADANIU 0 — dotyczy
mapowania statusu OLX, nie karty pozycji w UI.
