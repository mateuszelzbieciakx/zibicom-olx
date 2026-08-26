-- 0006_olx_attribute_mapping.sql
-- Mapowanie platform na wartosc atrybutu "type" ("Platforma") kategorii OLX
-- Xbox (2273) - ustalone empirycznie (GET /api/olx/categories/2273/attributes,
-- patrz zibicom.olx.fetch_category_attributes):
--   state (WYMAGANY, "Stan"):     used / new / damaged
--   type  (opcjonalny, "Platforma"): classic / xbox360 / xbox-one / xbox-series
-- "state" mapuje sie 1:1 na nasz enum listing_condition (new/used) - nie
-- wymaga slownika, patrz zibicom.olx.build_advert_payload. "type" wymaga
-- mapowania per platforma, stad ta migracja.
--
-- xboxone_sx ("Xbox One/Series", plyta Xbox One dzialajaca tez na Series) ->
-- 'xbox-one': fizyczny nosnik jest wydaniem na Xbox One, kompatybilnosc
-- wsteczna nie zmienia tego faktu (patrz analogiczna uwaga w 0002 o
-- rozroznieniu "laczony"/"natywny").
--
-- Sony (2272) i Nintendo (2274) maja WLASNE listy wartosci atrybutu "type"
-- (inna kategoria = inny zestaw dozwolonych kodow) - NIE sa czescia tej
-- migracji, ustalimy je osobno przez /api/olx/categories/{id}/attributes.
--
-- Ich olx_attribute_value ("PlayStation 4", "Nintendo Switch", ...) to
-- placeholdery z 0001 sprzed jakiejkolwiek empirycznej weryfikacji Partner
-- API - ten sam rodzaj zgadywania, ktore dla Xbox okazalo sie bledne
-- (kod atrybutu "platform" zamiast prawdziwego "type", wartosci-etykiety
-- zamiast krotkich kodow). build_advert_payload wysyla olx_attribute_value
-- ZAWSZE, gdy nie jest NULL - zostawienie tych zgadywanek grozi realnym
-- zgloszeniem bledu z OLX (albo, gorzej, cichym zaakceptowaniem zlego
-- atrybutu) przy pierwszej publikacji Sony/Nintendo. Zerujemy je wiec tutaj
-- i czekamy na taka sama empiryczna weryfikacje, jaka przeszedl Xbox.
--
-- NIE modyfikuje 0001-0005 (juz wykonane na bazie) - dziala wylacznie przez
-- UPDATE na istniejacych wierszach. IDEMPOTENTNA: bezwarunkowy UPDATE po
-- code/manufacturer (powtorne uruchomienie ustawia te sama wartosc - zero
-- efektu ubocznego). Zadnych DROP ani DELETE.

BEGIN;

UPDATE platform SET olx_attribute_value = 'classic'     WHERE code = 'xbox';
UPDATE platform SET olx_attribute_value = 'xbox360'     WHERE code = 'xbox360';
UPDATE platform SET olx_attribute_value = 'xbox-one'    WHERE code = 'xboxone_sx';
UPDATE platform SET olx_attribute_value = 'xbox-series' WHERE code = 'xboxsx';

UPDATE platform SET olx_attribute_value = NULL
    WHERE manufacturer IN ('sony', 'nintendo');

COMMIT;
