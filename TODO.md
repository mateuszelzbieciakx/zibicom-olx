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

