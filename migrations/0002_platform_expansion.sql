-- 0002_platform_expansion.sql
-- Rozszerzenie slownika platform o brakujace generacje konsol.
-- NIE modyfikuje 0001 (juz wykonana na bazie) - dziala wylacznie przez
-- ALTER/UPDATE/UPSERT na istniejacych obiektach.

BEGIN;

-- --------------------------------------------------------------------------
-- platform.generation: SMALLINT -> TEXT
-- --------------------------------------------------------------------------
-- W 0001 "generation" bylo numerem generacji sprzetu (7/8/9). Docelowy
-- slownik wymaga etykiet takich jak "PS4/PS5" czy "Xbox One/Series", ktorych
-- nie da sie zapisac w SMALLINT ani przepuscic przez CHECK (generation > 0).
-- Zmieniamy typ kolumny na TEXT i zastepujemy numeryczny CHECK walidacja
-- "nie pusty string, jesli podany".

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'platform_generation_ck'
    ) THEN
        ALTER TABLE platform DROP CONSTRAINT platform_generation_ck;
    END IF;
END $$;

ALTER TABLE platform
    ALTER COLUMN generation TYPE TEXT USING generation::TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'platform_generation_ck'
    ) THEN
        ALTER TABLE platform
            ADD CONSTRAINT platform_generation_ck
            CHECK (generation IS NULL OR btrim(generation) <> '');
    END IF;
END $$;

-- --------------------------------------------------------------------------
-- Przemianowanie istniejacych kodow (id i FK z game pozostaja bez zmian)
-- --------------------------------------------------------------------------
-- ps4/xboxone/switch moga miec powiazania w game.platform_id (FK po id, nie
-- po code), wiec zamieniamy tylko wartosc code/name/generation w miejscu -
-- zero DELETE, zero nowych id. Warunek WHERE code = '<stary>' czyni ten krok
-- idempotentnym: przy ponownym uruchomieniu kod jest juz przemianowany i
-- UPDATE nie trafia w zaden wiersz.

UPDATE platform SET code = 'ps4_ps5'    WHERE code = 'ps4';
UPDATE platform SET code = 'xboxone_sx' WHERE code = 'xboxone';
UPDATE platform SET code = 'switch1_2'  WHERE code = 'switch';

-- --------------------------------------------------------------------------
-- Docelowy slownik platform (UPSERT - idempotentny)
-- --------------------------------------------------------------------------
-- Kompatybilnosc wsteczna dziala w jedna strone (nowsza konsola odtwarza
-- gry starszej, nie odwrotnie), dlatego dla niektorych generacji wystepuja
-- rownolegle DWA wpisy:
--   * "laczony" (np. ps4_ps5 / "PS4/PS5") - plyta natywna na starsza konsole,
--     ktora uruchomi sie rowniez na nowszej,
--   * "natywny" (np. ps5 / "PlayStation 5") - gra dzialajaca WYLACZNIE na
--     nowszej konsoli.
-- Bez tego rozroznienia oferta z platforma "PS5" moglaby zostac sprzedana
-- klientowi z PS4, ktory takiej gry nie uruchomi.
--
-- olx_attribute_value celowo NIE jest tu nadpisywane (brak w DO UPDATE SET)
-- - to prawdziwa wartosc oczekiwana przez API OLX z 0001 i nie znamy
-- poprawnych wartosci dla nowych kodow, wiec nie zgadujemy ich tutaj.

INSERT INTO platform (code, name, manufacturer, generation, olx_attribute_value)
VALUES
    ('ps1',        'PlayStation 1',        'sony',      'PS1',             NULL),
    ('ps2',        'PlayStation 2',        'sony',      'PS2',             NULL),
    ('ps3',        'PlayStation 3',        'sony',      'PS3',             NULL),
    ('ps4_ps5',    'PS4/PS5',              'sony',      'PS4/PS5',         NULL),
    ('ps5',        'PlayStation 5',        'sony',      'PS5',             NULL),
    ('psp',        'PlayStation Portable', 'sony',      'PSP',             NULL),
    ('psvita',     'PlayStation Vita',     'sony',      'PS Vita',         NULL),
    ('xbox',       'Xbox',                 'microsoft', 'Xbox',            NULL),
    ('xbox360',    'Xbox 360',             'microsoft', 'Xbox 360',        NULL),
    ('xboxone_sx', 'Xbox One/Series',      'microsoft', 'Xbox One/Series', NULL),
    ('xboxsx',     'Xbox Series X/S',      'microsoft', 'Xbox Series X/S', NULL),
    ('switch1_2',  'Switch 1/2',           'nintendo',  'Switch 1/2',      NULL),
    ('switch2',    'Nintendo Switch 2',    'nintendo',  'Switch 2',        NULL),
    ('other',      'Inna platforma',       'other',     NULL,              NULL)
ON CONFLICT (code) DO UPDATE
SET name         = EXCLUDED.name,
    manufacturer = EXCLUDED.manufacturer,
    generation   = EXCLUDED.generation,
    is_active    = TRUE;

COMMIT;
