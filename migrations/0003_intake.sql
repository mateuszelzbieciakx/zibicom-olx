-- 0003_intake.sql
-- Warstwa poczekalni (staging) dla partii zdjec przetwarzanych przez AI.
-- Rozpoznanie obrazem jest niepewne (model myli ceny i czasem tytuly), wiec
-- wyniki NIE trafiaja bezposrednio do tabel produkcyjnych game/listing -
-- zyja tutaj, w osobnych tabelach, dopoki czlowiek ich nie zatwierdzi.
-- NIE modyfikuje 0001 ani 0002 (juz wykonane na bazie).

BEGIN;

-- --------------------------------------------------------------------------
-- Typy wyliczeniowe
-- --------------------------------------------------------------------------

CREATE TYPE intake_batch_status AS ENUM (
    'uploaded',
    'extracting',
    'review',
    'published',
    'failed'
);

CREATE TYPE intake_item_status AS ENUM (
    'pending',
    'approved',
    'published',
    'rejected',
    'error'
);

-- --------------------------------------------------------------------------
-- intake_batch (jedna wgrana partia zdjec)
-- --------------------------------------------------------------------------

CREATE TABLE intake_batch (
    id         INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    status     intake_batch_status NOT NULL DEFAULT 'uploaded',
    note       TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TRIGGER intake_batch_set_updated_at
    BEFORE UPDATE ON intake_batch
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- intake_item (jeden rozpoznany egzemplarz, oczekujacy na zatwierdzenie)
-- --------------------------------------------------------------------------

CREATE TABLE intake_item (
    id             INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    batch_id       INTEGER NOT NULL REFERENCES intake_batch (id) ON DELETE CASCADE,
    position       SMALLINT NOT NULL,
    title          TEXT,
    platform_id    INTEGER REFERENCES platform (id) ON DELETE RESTRICT,
    platform_other TEXT,
    price_pln      NUMERIC(10, 2),
    condition      listing_condition,
    ai_raw         JSONB,
    ai_warning     TEXT,
    status         intake_item_status NOT NULL DEFAULT 'pending',
    listing_id     INTEGER REFERENCES listing (id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT intake_item_position_uq UNIQUE (batch_id, position),
    CONSTRAINT intake_item_position_positive_ck CHECK (position > 0),
    CONSTRAINT intake_item_price_ck CHECK (price_pln IS NULL OR price_pln >= 0)
);

COMMENT ON COLUMN intake_item.title IS
    'NULL, gdy AI nie odczytalo tytulu - czlowiek uzupelnia w kroku '
    'zatwierdzania.';
COMMENT ON COLUMN intake_item.platform_id IS
    'NULL, gdy AI nie rozpoznalo platformy - jak wyzej, uzupelnia czlowiek.';
COMMENT ON COLUMN intake_item.listing_id IS
    'Wypelniane dopiero po publikacji oferty (kolejny krok). NULL przez cala '
    'poczekalnie.';
COMMENT ON COLUMN intake_item.ai_warning IS
    'Zbiorcze ostrzezenie ze scalania zdjec (np. brak ceny/tytulu, niepewny '
    'odczyt) - do podswietlenia w widoku zatwierdzania.';

CREATE INDEX intake_item_batch_status_idx ON intake_item (batch_id, status);

CREATE TRIGGER intake_item_set_updated_at
    BEFORE UPDATE ON intake_item
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- intake_photo (jedno zdjecie wgrane w ramach partii)
-- --------------------------------------------------------------------------

CREATE TABLE intake_photo (
    id                INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    batch_id          INTEGER NOT NULL REFERENCES intake_batch (id) ON DELETE CASCADE,
    item_id           INTEGER REFERENCES intake_item (id) ON DELETE SET NULL,
    position          SMALLINT NOT NULL,
    original_filename TEXT,
    public_url        TEXT NOT NULL,
    ai_raw            JSONB,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT intake_photo_position_uq UNIQUE (batch_id, position),
    CONSTRAINT intake_photo_position_positive_ck CHECK (position > 0)
);

COMMENT ON COLUMN intake_photo.item_id IS
    'NULL do momentu grupowania (extract_batch). ON DELETE SET NULL, bo slad '
    'audytowy zdjecia (i jego public_url w R2) ma przetrwac nawet po '
    'skasowaniu pozycji.';
COMMENT ON COLUMN intake_photo.position IS
    'Kolejnosc wgrania w partii - niesie informacje o granicach egzemplarzy '
    '(patrz zibicom.grouping.group_photos). Niezalezna numeracja od '
    'intake_item.position.';

CREATE INDEX intake_photo_item_idx ON intake_photo (item_id);

CREATE TRIGGER intake_photo_set_updated_at
    BEFORE UPDATE ON intake_photo
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

COMMIT;
