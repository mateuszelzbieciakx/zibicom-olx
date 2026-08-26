-- 0005_olx_category_mapping.sql
-- Mapowanie platform na kategorie OLX Partner API - ustalone empirycznie
-- (GET /categories, przejscie drzewa recznie przez zibicom.olx):
--   Elektronika (99) > Gry i Konsole (93) > Gry (1603)
--     > PlayStation (2272), Xbox (2273), Nintendo (2274)
-- OLX NIE rozdziela kategorii per generacja konsoli - jest jedna kategoria
-- "Gry" na producenta, wiec mapowanie jest po platform.manufacturer, nie po
-- platform.code/generation. Konkretna konsola (np. "Xbox 360") jest
-- prawdopodobnie atrybutem WEWNATRZ tej kategorii, nie osobna kategoria -
-- do potwierdzenia przez GET /api/olx/categories/2273/attributes.
--
-- UWAGA na kategorie o podobnych nazwach, ktore to NIE sa (latwo pomylic):
--   * 1915 "Konsole" - MEBEL (parent 565 = Meble), nie ma nic wspolnego z
--     grami wideo.
--   * 1604 - konsole jako SPRZET (do ewentualnej osobnej oferty na sama
--     konsole), nie gry - tej migracji nie dotyczy.
--
-- NIE modyfikuje 0001/0002/0003/0004 (juz wykonane na bazie) - dziala
-- wylacznie przez ALTER/UPDATE na istniejacych obiektach. IDEMPOTENTNA:
-- ADD COLUMN IF NOT EXISTS + UPDATE po manufacturer (bezwarunkowy, wiec
-- powtorne uruchomienie ustawia te sama wartosc - zero efektu ubocznego).
-- Zadnych DROP ani DELETE.

BEGIN;

ALTER TABLE platform
    ADD COLUMN IF NOT EXISTS olx_category_id INTEGER;

COMMENT ON COLUMN platform.olx_category_id IS
    'Id kategorii OLX (lisc drzewa, is_leaf=true) dla ofert tej platformy - '
    'jedna kategoria na producenta (Sony/Microsoft/Nintendo), OLX nie '
    'rozdziela kategorii per generacja konsoli. NULL dla "other" - brak '
    'ustalonej kategorii.';

UPDATE platform SET olx_category_id = 2272 WHERE manufacturer = 'sony';
UPDATE platform SET olx_category_id = 2273 WHERE manufacturer = 'microsoft';
UPDATE platform SET olx_category_id = 2274 WHERE manufacturer = 'nintendo';
UPDATE platform SET olx_category_id = NULL WHERE manufacturer = 'other';

COMMIT;
