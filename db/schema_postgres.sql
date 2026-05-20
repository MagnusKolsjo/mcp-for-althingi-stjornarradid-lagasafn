-- SPDX-License-Identifier: AGPL-3.0-or-later
-- MCP-server för isländsk riksdags- och rättsdata
-- PostgreSQL-schema: island
-- Kör: psql -d riksdag -f schema_postgres.sql

-- Aktivera pgvector-extension (kräver pgvector installerat)
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS island;

-- ─────────────────────────────────────────────────────────────────────────────
-- dokument — þingskjöl, lög (lagasafn), reglugerðir
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS island.dokument (
    id            SERIAL PRIMARY KEY,

    -- Källa: 'althingi', 'lagasafn', 'reglugerd', 'stjornartidindi'
    kilde         TEXT        NOT NULL,

    -- Dokumenttyp per källa:
    --   althingi:       skjalategund (t.ex. 'stjórnarfrumvarp', 'nefndarálit')
    --   lagasafn:       'log'
    --   reglugerd:      'reglugerd'
    --   stjornartidindi: mainType (t.ex. 'LOG', 'REGLUGERD')
    malstegund    TEXT,

    -- Beteckning: "þing/skjalnr" för þingskjöl, "nr/ár" för lög/reglugerðir
    beteckning    TEXT,

    titill        TEXT,
    thing_nr      INTEGER,          -- þingnúmer (riksmötesnummer)
    skjalnr       INTEGER,          -- þingskjalsnúmer

    dagsetning    DATE,
    url           TEXT        NOT NULL,
    pdf_url       TEXT,
    fulltext_md   TEXT,

    cachad_vid    TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT dokument_kilde_url_uq UNIQUE (kilde, url)
);

CREATE INDEX IF NOT EXISTS dokument_kilde_idx       ON island.dokument (kilde);
CREATE INDEX IF NOT EXISTS dokument_thing_nr_idx    ON island.dokument (thing_nr);
CREATE INDEX IF NOT EXISTS dokument_malstegund_idx  ON island.dokument (malstegund);
CREATE INDEX IF NOT EXISTS dokument_dagsetning_idx  ON island.dokument (dagsetning DESC);

-- GIN-index för FTS med 'simple' konfiguration (isländska har ingen pg-stemmer)
CREATE INDEX IF NOT EXISTS dokument_fts_idx ON island.dokument
    USING GIN (to_tsvector('simple',
               coalesce(titill,'') || ' ' || coalesce(fulltext_md,'')));


-- ─────────────────────────────────────────────────────────────────────────────
-- dokument_rit — rit og skýrslur från stjornarradid.is
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS island.dokument_rit (
    -- URL som primärnyckel (stabil identifierare från stjornarradid.is)
    url               TEXT PRIMARY KEY,

    titill            TEXT        NOT NULL,

    -- URL-slug (sista segmentet i /stakt-rit/YYYY/MM/DD/SLUG/)
    slug              TEXT,

    dagsetning        DATE,
    ministerium       TEXT,          -- isländskt ministeriumsnamn (12 värden)
    tema              TEXT,          -- verkefni/ämnesord
    ar                INTEGER,       -- publiceringsår (2021-2026)

    -- Heuristisk dokumenttyp baserat på titelprefixet (~38 % täckning)
    -- Värden: 'Skýrsla', 'Ársskýrsla', 'Greinargerð', 'Aðgerðaáætlun',
    --         'Hvítbók', 'Grænbók', 'Stöðuskýrsla', m.fl. NULL om okänt.
    dokumenttyp_isl   TEXT,

    -- Direkt PDF-länk (från /library/-lagret på stjornarradid.is)
    pdf_url            TEXT,

    -- Fulltext extraherad från PDF (pymupdf4llm), raderas ej (skillnad från ström 9)
    -- Nyckeln är att vi inte cachelagrar PDF-filen — bara extraherad text
    fulltext_md        TEXT,

    pdf_extraherad_at  TIMESTAMPTZ,

    -- Synkstatus per post: 'ny', 'pdf_extraherad', 'embeddad'
    sync_status        TEXT DEFAULT 'ny',

    cachad_vid         TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS dokument_rit_ar_idx          ON island.dokument_rit (ar DESC);
CREATE INDEX IF NOT EXISTS dokument_rit_ministerium_idx ON island.dokument_rit (ministerium);
CREATE INDEX IF NOT EXISTS dokument_rit_typ_idx         ON island.dokument_rit (dokumenttyp_isl);

CREATE INDEX IF NOT EXISTS dokument_rit_fts_idx ON island.dokument_rit
    USING GIN (to_tsvector('simple',
               coalesce(titill,'') || ' ' || coalesce(fulltext_md,'')));


-- ─────────────────────────────────────────────────────────────────────────────
-- chunks — chunked fulltext för RAG (þingskjöl, lög, reglugerðir)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS island.chunks (
    id           SERIAL PRIMARY KEY,
    dok_id       INTEGER      NOT NULL REFERENCES island.dokument(id) ON DELETE CASCADE,
    chunk_index  INTEGER      NOT NULL,
    text         TEXT         NOT NULL,
    -- 768-dim för intfloat/multilingual-e5-base
    embedding    VECTOR(768),

    CONSTRAINT chunks_dok_chunk_uq UNIQUE (dok_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_embedding_idx ON island.chunks
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);


-- ─────────────────────────────────────────────────────────────────────────────
-- chunks_rit — chunked fulltext för RAG (rit og skýrslur)
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS island.chunks_rit (
    id           SERIAL PRIMARY KEY,
    dok_url      TEXT         NOT NULL REFERENCES island.dokument_rit(url) ON DELETE CASCADE,
    chunk_index  INTEGER      NOT NULL,
    text         TEXT         NOT NULL,
    embedding    VECTOR(768),

    CONSTRAINT chunks_rit_dok_chunk_uq UNIQUE (dok_url, chunk_index)
);

CREATE INDEX IF NOT EXISTS chunks_rit_embedding_idx ON island.chunks_rit
    USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);


-- ─────────────────────────────────────────────────────────────────────────────
-- sync_status — synkroniseringsstatus per källa
-- ─────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS island.sync_status (
    kilde        TEXT PRIMARY KEY,
    sist_synkad  TIMESTAMPTZ,
    checksum     TEXT,
    detaljer     JSONB
);
