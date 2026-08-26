-- 0007_sony_nintendo_attributes.sql
-- Mapowanie platform Sony/Nintendo na wartosc atrybutu "type" ("Platforma")
-- ich kategorii OLX - ustalone empirycznie (GET
-- /api/olx/categories/{id}/attributes, patrz zibicom.olx.fetch_category_attributes),
-- analogicznie do Xbox w 0006_olx_attribute_mapping.sql:
--   kategoria 2272 (PlayStation): ps5 / ps4 / ps3 / ps2 / ps1 / portable / vita
--   kategoria 2274 (Nintendo):    nintendo-switch / nintendo-switch-2 /
--                                 nintendo-wii-u / nintendo-wii / nintendo-3ds /
--                                 nintendo-ds
--
-- ps4_ps5 ("PS4/PS5", plyta natywna na PS4 dzialajaca tez na PS5) -> 'ps4':
-- fizyczny nosnik jest wydaniem na PS4, kompatybilnosc wsteczna nie zmienia
-- tego faktu (ta sama zasada co xboxone_sx->'xbox-one' w 0006, i
-- "laczony"/"natywny" w 0002). Analogicznie switch1_2 -> 'nintendo-switch'.
--
-- NIE modyfikuje 0001-0006 (juz wykonane na bazie) - dziala wylacznie przez
-- UPDATE na istniejacych wierszach. IDEMPOTENTNA: bezwarunkowy UPDATE po
-- code (powtorne uruchomienie ustawia te sama wartosc - zero efektu
-- ubocznego). Zadnych DROP ani DELETE. "other" NIE jest tu ruszane - juz
-- jest NULL (0001) i tak zostaje (brak ustalonej kategorii/atrybutow, patrz
-- 0005_olx_category_mapping.sql).

BEGIN;

UPDATE platform SET olx_attribute_value = 'ps1'    WHERE code = 'ps1';
UPDATE platform SET olx_attribute_value = 'ps2'    WHERE code = 'ps2';
UPDATE platform SET olx_attribute_value = 'ps3'    WHERE code = 'ps3';
UPDATE platform SET olx_attribute_value = 'ps4'    WHERE code = 'ps4_ps5';
UPDATE platform SET olx_attribute_value = 'ps5'    WHERE code = 'ps5';
UPDATE platform SET olx_attribute_value = 'portable' WHERE code = 'psp';
UPDATE platform SET olx_attribute_value = 'vita'   WHERE code = 'psvita';

UPDATE platform SET olx_attribute_value = 'nintendo-switch'   WHERE code = 'switch1_2';
UPDATE platform SET olx_attribute_value = 'nintendo-switch-2' WHERE code = 'switch2';

COMMIT;
