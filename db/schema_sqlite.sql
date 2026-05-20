-- MCP-server för isländsk riksdags- och rättsdata
-- SQLite-schema (utan pgvector)

CREATE TABLE IF NOT EXISTS dokument (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kilde        TEXT    NOT NULL,
    malstegund   TEXT,
    beteckning   TEXT,
    titill       TEXT,
    thing_nr     INTEGER,
    skjalnr      INTEGER,
    dagsetning   TEXT,
    url          TEXT    NOT NULL,
    pdf_url      TEXT,
    fulltext_md  TEXT,
    cachad_vid   TEXT    DEFAULT (datetime('now')),
    UNIQUE (kilde, url)
);

CREATE INDEX IF NOT EXISTS dokument_kilde_idx    ON dokument (kilde);
CREATE INDEX IF NOT EXISTS dokument_thing_nr_idx ON dokument (thing_nr);

CREATE TABLE IF NOT EXISTS dokument_rit (
    url               TEXT PRIMARY KEY,
    titill            TEXT NOT NULL,
    slug              TEXT,
    dagsetning        TEXT,
    ministerium       TEXT,
    tema              TEXT,
    ar                INTEGER,
    dokumenttyp_isl   TEXT,
    pdf_url           TEXT,
    fulltext_md       TEXT,
    pdf_extraherad_at TEXT,
    sync_status       TEXT DEFAULT 'ny',
    cachad_vid        TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS dokument_rit_ar_idx ON dokument_rit (ar DESC);

-- SQLite stöder inte pgvector — chunks utan embedding
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dok_id      INTEGER NOT NULL REFERENCES dokument(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    UNIQUE (dok_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS chunks_rit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dok_url     TEXT    NOT NULL REFERENCES dokument_rit(url) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    text        TEXT    NOT NULL,
    UNIQUE (dok_url, chunk_index)
);

CREATE TABLE IF NOT EXISTS sync_status (
    kilde       TEXT PRIMARY KEY,
    sist_synkad TEXT,
    checksum    TEXT,
    detaljer    TEXT
);
