-- 0001_initial_schema.sql
-- Schemat poczatkowy: katalog gier, oferty OLX, zdjecia, sprzedaz i audyt API.
-- Konwencje: 3NF, soft delete (is_active), created_at/updated_at TIMESTAMPTZ,
-- updated_at pilnuje wspolny trigger set_updated_at().

BEGIN;

-- --------------------------------------------------------------------------
-- Typy wyliczeniowe
-- --------------------------------------------------------------------------

CREATE TYPE listing_condition AS ENUM ('new', 'used');

CREATE TYPE listing_status AS ENUM (
    'draft',
    'pending',
    'active',
    'sold',
    'removed',
    'error'
);

CREATE TYPE sale_channel AS ENUM ('in_store', 'olx');

CREATE TYPE platform_manufacturer AS ENUM (
    'sony',
    'microsoft',
    'nintendo',
    'other'
);

CREATE TYPE photo_role AS ENUM ('front', 'back', 'interior');

-- --------------------------------------------------------------------------
-- Wspolny trigger updated_at
-- --------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

COMMENT ON FUNCTION set_updated_at() IS
    'Ustawia updated_at na now() przy kazdym UPDATE. Kod aplikacji nie dotyka '
    'tej kolumny.';

-- --------------------------------------------------------------------------
-- platform
-- --------------------------------------------------------------------------

CREATE TABLE platform (
    id                  INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    code                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    manufacturer        platform_manufacturer NOT NULL,
    generation          SMALLINT,
    olx_attribute_value TEXT,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT platform_code_lower_ck CHECK (code = lower(code)),
    CONSTRAINT platform_generation_ck CHECK (generation IS NULL OR generation > 0)
);

COMMENT ON COLUMN platform.olx_attribute_value IS
    'Wartosc atrybutu platformy oczekiwana przez API OLX przy publikacji oferty.';

CREATE TRIGGER platform_set_updated_at
    BEFORE UPDATE ON platform
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- game (katalog produktu, bez informacji o egzemplarzu)
-- --------------------------------------------------------------------------

CREATE TABLE game (
    id            INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    ean           TEXT,
    title         TEXT NOT NULL,
    platform_id   INTEGER NOT NULL REFERENCES platform (id) ON DELETE RESTRICT,
    platform_note TEXT,
    cover_url     TEXT,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT game_ean_format_ck CHECK (ean ~ '^([0-9]{8}|[0-9]{13})$'),
    CONSTRAINT game_title_not_blank_ck CHECK (btrim(title) <> '')
);

COMMENT ON COLUMN game.ean IS
    'EAN-8 lub EAN-13. NULL dla pozycji bez kodu kreskowego (np. luzem, import).';
COMMENT ON COLUMN game.platform_note IS
    'Doprecyzowanie platformy, gdy nie ma jej w slowniku (platform.code = other).';

-- EAN unikalny tylko tam, gdzie w ogole istnieje.
CREATE UNIQUE INDEX game_ean_uidx ON game (ean) WHERE ean IS NOT NULL;

CREATE INDEX game_platform_idx ON game (platform_id);
CREATE INDEX game_title_idx ON game (lower(title));

CREATE TRIGGER game_set_updated_at
    BEFORE UPDATE ON game
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- listing (jedna oferta OLX = jeden fizyczny egzemplarz)
-- --------------------------------------------------------------------------

CREATE TABLE listing (
    id             INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    game_id        INTEGER NOT NULL REFERENCES game (id) ON DELETE RESTRICT,
    condition      listing_condition NOT NULL,
    price_pln      NUMERIC(10, 2) NOT NULL,
    status         listing_status NOT NULL DEFAULT 'draft',
    olx_advert_id  BIGINT UNIQUE,
    olx_status     TEXT,
    olx_payload    JSONB,
    posted_at      TIMESTAMPTZ,
    sold_at        TIMESTAMPTZ,
    is_active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT listing_price_ck CHECK (price_pln >= 0)
);

COMMENT ON TABLE listing IS
    'Jeden wiersz = jeden fizyczny egzemplarz gry wystawiony (lub przygotowany) '
    'na OLX. EAN celowo NIE jest tu duplikowany - dolacza sie go przez game_id.';
COMMENT ON COLUMN listing.olx_status IS
    'Surowy status zwrocony przez OLX, niezaleznie od naszego pola status.';

-- FIFO: przy sprzedazy stacjonarnej zdejmujemy najstarsza aktywna oferte
-- w obrebie pary (gra, stan) - nowa i uzywana maja rozne ceny, wiec kopie
-- nie sa wymienne miedzy stanami.
CREATE INDEX listing_fifo_idx
    ON listing (game_id, condition, posted_at)
    WHERE status = 'active';

CREATE INDEX listing_game_idx ON listing (game_id);
CREATE INDEX listing_status_idx ON listing (status) WHERE is_active;
CREATE INDEX listing_olx_advert_idx ON listing (olx_advert_id)
    WHERE olx_advert_id IS NOT NULL;

CREATE TRIGGER listing_set_updated_at
    BEFORE UPDATE ON listing
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- listing_photo
-- --------------------------------------------------------------------------

CREATE TABLE listing_photo (
    id         INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    listing_id INTEGER NOT NULL REFERENCES listing (id) ON DELETE CASCADE,
    position   SMALLINT NOT NULL,
    public_url TEXT NOT NULL,
    role       photo_role,
    is_primary BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT listing_photo_position_ck CHECK (position > 0),
    CONSTRAINT listing_photo_position_uq UNIQUE (listing_id, position)
);

-- Co najwyzej jedno zdjecie glowne na oferte.
CREATE UNIQUE INDEX listing_photo_primary_uidx
    ON listing_photo (listing_id)
    WHERE is_primary;

CREATE TRIGGER listing_photo_set_updated_at
    BEFORE UPDATE ON listing_photo
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- sale_event
-- --------------------------------------------------------------------------

CREATE TABLE sale_event (
    id             INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    listing_id     INTEGER NOT NULL REFERENCES listing (id) ON DELETE RESTRICT,
    sold_price_pln NUMERIC(10, 2) NOT NULL,
    channel        sale_channel NOT NULL,
    sold_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT sale_event_price_ck CHECK (sold_price_pln >= 0)
);

CREATE INDEX sale_event_listing_idx ON sale_event (listing_id);
CREATE INDEX sale_event_sold_at_idx ON sale_event (sold_at);

CREATE TRIGGER sale_event_set_updated_at
    BEFORE UPDATE ON sale_event
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- olx_operation (audyt wywolan API)
-- --------------------------------------------------------------------------

CREATE TABLE olx_operation (
    id               INTEGER PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    listing_id       INTEGER REFERENCES listing (id) ON DELETE SET NULL,
    operation        TEXT NOT NULL,
    request_payload  JSONB,
    response_payload JSONB,
    http_status      SMALLINT,
    succeeded        BOOLEAN NOT NULL,
    olx_error        TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT olx_operation_http_status_ck
        CHECK (http_status IS NULL OR http_status BETWEEN 100 AND 599)
);

COMMENT ON COLUMN olx_operation.listing_id IS
    'NULL dla wywolan niezwiazanych z oferta (np. odswiezenie tokenu) oraz po '
    'twardym usunieciu oferty - slad audytowy ma przetrwac.';

CREATE INDEX olx_operation_listing_idx ON olx_operation (listing_id, created_at DESC);
CREATE INDEX olx_operation_failed_idx ON olx_operation (created_at DESC)
    WHERE NOT succeeded;

CREATE TRIGGER olx_operation_set_updated_at
    BEFORE UPDATE ON olx_operation
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- --------------------------------------------------------------------------
-- Seed slownika platform (UPSERT - migracja jest idempotentna)
-- --------------------------------------------------------------------------

INSERT INTO platform (code, name, manufacturer, generation, olx_attribute_value)
VALUES
    ('ps3',      'PlayStation 3',      'sony',      7,    'PlayStation 3'),
    ('ps4',      'PlayStation 4',      'sony',      8,    'PlayStation 4'),
    ('ps5',      'PlayStation 5',      'sony',      9,    'PlayStation 5'),
    ('psp',      'PlayStation Portable', 'sony',    7,    'PSP'),
    ('psvita',   'PlayStation Vita',   'sony',      8,    'PS Vita'),
    ('xbox360',  'Xbox 360',           'microsoft', 7,    'Xbox 360'),
    ('xboxone',  'Xbox One',           'microsoft', 8,    'Xbox One'),
    ('xboxsx',   'Xbox Series X/S',    'microsoft', 9,    'Xbox Series X|S'),
    ('switch',   'Nintendo Switch',    'nintendo',  8,    'Nintendo Switch'),
    ('other',    'Inna platforma',     'other',     NULL, NULL)
ON CONFLICT (code) DO UPDATE
SET name                = EXCLUDED.name,
    manufacturer        = EXCLUDED.manufacturer,
    generation          = EXCLUDED.generation,
    olx_attribute_value = EXCLUDED.olx_attribute_value;

COMMIT;
