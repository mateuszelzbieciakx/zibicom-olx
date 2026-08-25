-- 0004_olx_token.sql
-- Token OAuth integracji z OLX Partner API - singleton (jeden wiersz, id=1),
-- bo konto OLX firmy jest jedno. Refresh token ROTUJE przy KAZDYM odswiezeniu
-- access tokenu (zywotnosc ok. miesiaca) - kolumna jest wiec NOT NULL, bo bez
-- niego autoryzacja jest bezpowrotnie utracona. Tokeny sa szyfrowane Fernetem
-- PRZED zapisem (zibicom.crypto) - klucz szyfrujacy jest sekretem i NIE
-- trafia do bazy (secrets/token_encryption_key.txt), wiec sam wyciek dumpa
-- nie wystarcza do przejecia konta OLX.
-- NIE modyfikuje 0001/0002/0003 (juz wykonane na bazie).

BEGIN;

CREATE TABLE olx_token (
    id                      SMALLINT PRIMARY KEY DEFAULT 1,
    access_token_encrypted  BYTEA,
    refresh_token_encrypted BYTEA NOT NULL,
    access_expires_at       TIMESTAMPTZ,
    scope                   TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT olx_token_singleton_ck CHECK (id = 1)
);

COMMENT ON TABLE olx_token IS
    'Singleton (jeden wiersz, id=1) przechowujacy autoryzacje OLX Partner API '
    'jednego konta firmowego. Zapelniany przez zibicom.olx.exchange_code, '
    'aktualizowany przez zibicom.olx.get_access_token przy kazdym odswiezeniu.';
COMMENT ON COLUMN olx_token.access_token_encrypted IS
    'Access token zaszyfrowany Fernetem (zibicom.crypto), NIGDY w postaci '
    'jawnej. NULL tylko chwilowo - miedzy wymiana kodu autoryzacyjnego a '
    'pierwszym zapisem tokenu w tej samej transakcji.';
COMMENT ON COLUMN olx_token.refresh_token_encrypted IS
    'Refresh token zaszyfrowany Fernetem. ROTUJE przy kazdym odswiezeniu '
    'access tokenu - nowa wartosc trzeba zapisac natychmiast, inaczej '
    'autoryzacja jest bezpowrotnie utracona (OLX uniewaznia poprzedni token).';
COMMENT ON COLUMN olx_token.access_expires_at IS
    'Moment wygasniecia access tokenu (OLX: godzina od wydania). '
    'zibicom.olx.get_access_token odswieza z 60-sekundowym marginesem.';

CREATE TRIGGER olx_token_set_updated_at
    BEFORE UPDATE ON olx_token
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

COMMIT;
