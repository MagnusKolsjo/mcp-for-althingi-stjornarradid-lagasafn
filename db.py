# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
db.py — Databaslager för MCP-servern för Althingi, Lagasafn, reglugerd och Stjórnarráðið.

Stödjer två lagringsbackender — användaren väljer vid installation via
DATABASE_URL i .env:

  postgresql://anvandare:losenord@localhost:5432/<DATABASNAMN>
    → PostgreSQL + pgvector (schema: island)
    → Ger samtidiga skrivningar och pgvector-baserad semantisk sökning

  sqlite:///island_cache.db
    → SQLite (en lokal fil, ingen serverprocess)
    → Snabbt att komma igång; vektorsökning kräver Postgres

Anslutningsmönstret är per-anrop: varje funktion öppnar och stänger sin
egen anslutning. Detta gör koden tråd-säker, slipper stale-connection-
problematik och håller transaktionerna korta.

Tabeller (Postgres-schema: island):
  dokument       — þingskjöl, lög, reglugerðir (metadata + fulltext_md)
  dokument_rit   — rit og skýrslur från stjornarradid.is
  chunks         — chunked fulltext för RAG (þingskjöl, lög, reglugerðir)
  chunks_rit     — chunked fulltext för RAG (rit og skýrslur)
  sync_status    — synkroniseringstidsstämplar per källa
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "")


# ---------------------------------------------------------------------------
# Backend-väljare och hjälpfunktioner
# ---------------------------------------------------------------------------

def _ar_postgres() -> bool:
    """True om DATABASE_URL pekar mot Postgres, annars False."""
    return DATABASE_URL.startswith(("postgresql://", "postgres://"))


def _hamta_db():
    """
    Öppnar en ny databasanslutning. Varje anropare ansvarar för att stänga den,
    typiskt via `with _hamta_db() as conn:` eller via `_cursor()`-kontexthanteraren.
    """
    if _ar_postgres():
        try:
            import psycopg2
        except ImportError as exc:
            raise RuntimeError(
                "DATABASE_URL pekar mot Postgres men psycopg2 är inte installerat. "
                "Kör 'pip install psycopg2-binary' eller välj sqlite:// i DATABASE_URL."
            ) from exc
        return psycopg2.connect(DATABASE_URL)

    # sqlite:///relativ.db ger sökvägen "/relativ.db" och sqlite:////abs/fil.db
    # ger "//abs/fil.db". Bara det första snedstrecket hör till URL-syntaxen.
    sokvag = urlparse(DATABASE_URL).path
    if sokvag.startswith("/"):
        sokvag = sokvag[1:]
    if not sokvag:
        raise RuntimeError("DATABASE_URL för SQLite saknar filsökväg")
    conn = sqlite3.connect(sokvag, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ph() -> str:
    """Platshållare för parameterbindning. Postgres: %s. SQLite: ?."""
    return "%s" if _ar_postgres() else "?"


def _prefix() -> str:
    """Schema-prefix för tabellnamn. Postgres får 'island.', SQLite får ''."""
    return "island." if _ar_postgres() else ""


# ---------------------------------------------------------------------------
# Schema-init
# ---------------------------------------------------------------------------

def initiera_schema():
    """
    Skapar tabeller och index om de inte finns. Kör SQL från db/schema_*.sql.
    Säker att köra många gånger — schemat är idempotent.

    Loggar varning och returnerar utan att kasta om DB är otillgänglig,
    så att MCP-servern står kvar i MCP-klienten även när Postgres-containern
    är nere. Verktygsanrop felar i så fall tills DB kommer upp igen.
    """
    if not DATABASE_URL:
        log.warning("DATABASE_URL är inte satt — databasen används inte")
        return

    sql_filnamn = "schema_postgres.sql" if _ar_postgres() else "schema_sqlite.sql"
    sql_sokvag = Path(__file__).parent / "db" / sql_filnamn
    if not sql_sokvag.exists():
        log.error("Schemafil saknas: %s", sql_sokvag)
        return

    sql = sql_sokvag.read_text(encoding="utf-8")
    try:
        conn = _hamta_db()
        try:
            if _ar_postgres():
                with conn.cursor() as cur:
                    cur.execute(sql)
            else:
                conn.executescript(sql)
            conn.commit()
            log.info("Schema initierat (%s)", "postgres" if _ar_postgres() else "sqlite")
        finally:
            conn.close()
    except Exception as exc:
        log.warning(
            "Databasinitiering misslyckades: %s — servern startar utan DB. "
            "Verktygsanrop felar tills databasen är tillgänglig.",
            exc,
        )


# ---------------------------------------------------------------------------
# Cursor-kontexthanterare
# ---------------------------------------------------------------------------

@contextmanager
def _cursor():
    """
    Kontexthanterare som ger en databasmarkör och committar/rollbackar.
    Öppnar och stänger en frisk anslutning per anrop.
    """
    conn = _hamta_db()
    cur = conn.cursor()
    try:
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# FTS — þingskjöl / lög / reglugerðir
# ---------------------------------------------------------------------------

def fts_sok(
    fraga: str,
    kilde_filter: Optional[str] = None,
    malstegund_filter: Optional[str] = None,
    max_treff: int = 10,
) -> list[dict]:
    """
    Fulltextsökning i island.dokument (þingskjöl, lög, reglugerðir).

    PostgreSQL: GIN-index med 'simple' konfiguration (isländska stöds ej av pg FTS).
    SQLite: ILIKE-fallback.

    Kommaseparerade termer = OR-logik.

    Returnerar: id, kilde, malstegund, beteckning, titill, thing_nr, skjalnr,
                dagsetning, url, pdf_url, rank.
    """
    termer = [t.strip() for t in fraga.split(",") if t.strip()]
    if not termer:
        return []

    if _ar_postgres():
        return _pg_fts_dok(termer, kilde_filter, malstegund_filter, max_treff)
    return _sq_fts_dok(termer, kilde_filter, malstegund_filter, max_treff)


def _pg_fts_dok(termer, kilde_filter, malstegund_filter, max_treff) -> list[dict]:
    """PostgreSQL FTS med 'simple' konfiguration (ej isländsk stemming)."""
    tsq_parts = " || ".join(["plainto_tsquery('simple', %s)"] * len(termer))

    villkor = []
    filter_params = []
    if kilde_filter:
        villkor.append("kilde = %s")
        filter_params.append(kilde_filter)
    if malstegund_filter:
        villkor.append("malstegund ILIKE %s")
        filter_params.append(f"%{malstegund_filter}%")

    where_extra = ("AND " + " AND ".join(villkor)) if villkor else ""
    params = list(termer) + filter_params + [max_treff]

    sql = f"""
        WITH q AS (SELECT {tsq_parts} AS tsq)
        SELECT
            d.id, d.kilde, d.malstegund, d.beteckning, d.titill,
            d.thing_nr, d.skjalnr, d.dagsetning, d.url, d.pdf_url,
            ts_rank_cd(
                to_tsvector('simple',
                    coalesce(d.titill,'') || ' ' || coalesce(d.fulltext_md,'')),
                q.tsq
            ) AS rank
        FROM   island.dokument d, q
        WHERE  to_tsvector('simple',
                   coalesce(d.titill,'') || ' ' || coalesce(d.fulltext_md,''))
               @@ q.tsq
        {where_extra}
        ORDER  BY rank DESC, d.dagsetning DESC NULLS LAST
        LIMIT  %s
    """
    with _cursor() as cur:
        cur.execute(sql, params)
        rader = cur.fetchall()
    return [_rad_til_dok(r) for r in rader]


def _sq_fts_dok(termer, kilde_filter, malstegund_filter, max_treff) -> list[dict]:
    """SQLite ILIKE-fallback."""
    or_del = " OR ".join(["(titill LIKE ? OR fulltext_md LIKE ?)"] * len(termer))
    villkor = [f"({or_del})"]
    params: list = []
    for t in termer:
        params += [f"%{t}%", f"%{t}%"]

    if kilde_filter:
        villkor.append("kilde = ?")
        params.append(kilde_filter)
    if malstegund_filter:
        villkor.append("malstegund LIKE ?")
        params.append(f"%{malstegund_filter}%")

    params.append(max_treff)
    sql = f"""
        SELECT id, kilde, malstegund, beteckning, titill,
               thing_nr, skjalnr, dagsetning, url, pdf_url, 0.0 AS rank
        FROM   dokument
        WHERE  {' AND '.join(villkor)}
        ORDER  BY dagsetning DESC
        LIMIT  ?
    """
    with _cursor() as cur:
        cur.execute(sql, params)
        rader = cur.fetchall()
    return [_rad_til_dok(r) for r in rader]


def _rad_til_dok(r) -> dict:
    return {
        "id":         r[0],
        "kilde":      r[1],
        "malstegund": r[2],
        "beteckning": r[3],
        "titill":     r[4],
        "thing_nr":   r[5],
        "skjalnr":    r[6],
        "dagsetning": str(r[7]) if r[7] else None,
        "url":        r[8],
        "pdf_url":    r[9],
        "rank":       round(float(r[10]), 4) if r[10] is not None else 0.0,
    }


# ---------------------------------------------------------------------------
# FTS — rit og skýrslur
# ---------------------------------------------------------------------------

def fts_sok_rit(
    fraga: str,
    ministerium: Optional[str] = None,
    ar: Optional[int] = None,
    dokumenttyp: Optional[str] = None,
    max_treff: int = 10,
) -> list[dict]:
    """
    Fulltextsökning i island.dokument_rit (rit og skýrslur från stjornarradid.is).

    Kommaseparerade termer = OR-logik.
    OBS: Sajtens egen sökmotor är trasig — all sökning sker mot lokal DB.

    Returnerar: url, titill, slug, dagsetning, ministerium, tema, ar,
                dokumenttyp_isl, pdf_url, rank.
    """
    termer = [t.strip() for t in fraga.split(",") if t.strip()]
    if not termer:
        return []

    if _ar_postgres():
        return _pg_fts_rit(termer, ministerium, ar, dokumenttyp, max_treff)
    return _sq_fts_rit(termer, ministerium, ar, dokumenttyp, max_treff)


def _pg_fts_rit(termer, ministerium, ar, dokumenttyp, max_treff) -> list[dict]:
    tsq_parts = " || ".join(["plainto_tsquery('simple', %s)"] * len(termer))

    villkor = []
    filter_params: list = []
    if ministerium:
        villkor.append("ministerium ILIKE %s")
        filter_params.append(f"%{ministerium}%")
    if ar:
        villkor.append("ar = %s")
        filter_params.append(ar)
    if dokumenttyp:
        villkor.append("dokumenttyp_isl ILIKE %s")
        filter_params.append(f"%{dokumenttyp}%")

    where_extra = ("AND " + " AND ".join(villkor)) if villkor else ""
    params = list(termer) + filter_params + [max_treff]

    sql = f"""
        WITH q AS (SELECT {tsq_parts} AS tsq)
        SELECT
            r.url, r.titill, r.slug, r.dagsetning, r.ministerium,
            r.tema, r.ar, r.dokumenttyp_isl, r.pdf_url,
            ts_rank(
                setweight(to_tsvector('simple', coalesce(r.titill,'')), 'A') ||
                setweight(to_tsvector('simple', coalesce(r.fulltext_md,'')), 'C'),
                q.tsq,
                32
            ) AS rank
        FROM   island.dokument_rit r, q
        WHERE  to_tsvector('simple',
                   coalesce(r.titill,'') || ' ' || coalesce(r.fulltext_md,''))
               @@ q.tsq
        {where_extra}
        ORDER  BY rank DESC, r.dagsetning DESC NULLS LAST
        LIMIT  %s
    """
    with _cursor() as cur:
        cur.execute(sql, params)
        rader = cur.fetchall()
    return [_rad_til_rit(r) for r in rader]


def _sq_fts_rit(termer, ministerium, ar, dokumenttyp, max_treff) -> list[dict]:
    or_del = " OR ".join(["(titill LIKE ? OR fulltext_md LIKE ?)"] * len(termer))
    villkor = [f"({or_del})"]
    params: list = []
    for t in termer:
        params += [f"%{t}%", f"%{t}%"]

    if ministerium:
        villkor.append("ministerium LIKE ?")
        params.append(f"%{ministerium}%")
    if ar:
        villkor.append("ar = ?")
        params.append(ar)
    if dokumenttyp:
        villkor.append("dokumenttyp_isl LIKE ?")
        params.append(f"%{dokumenttyp}%")

    params.append(max_treff)
    sql = f"""
        SELECT url, titill, slug, dagsetning, ministerium,
               tema, ar, dokumenttyp_isl, pdf_url, 0.0 AS rank
        FROM   dokument_rit
        WHERE  {' AND '.join(villkor)}
        ORDER  BY dagsetning DESC
        LIMIT  ?
    """
    with _cursor() as cur:
        cur.execute(sql, params)
        rader = cur.fetchall()
    return [_rad_til_rit(r) for r in rader]


def _rad_til_rit(r) -> dict:
    return {
        "url":             r[0],
        "titill":          r[1],
        "slug":            r[2],
        "dagsetning":      str(r[3]) if r[3] else None,
        "ministerium":     r[4],
        "tema":            r[5],
        "ar":              r[6],
        "dokumenttyp_isl": r[7],
        "pdf_url":         r[8],
        "rank":            round(float(r[9]), 4) if r[9] is not None else 0.0,
    }


# ---------------------------------------------------------------------------
# Upsert — dokument
# ---------------------------------------------------------------------------

def upsert_dokument(
    kilde: str,
    malstegund: Optional[str],
    beteckning: Optional[str],
    titill: Optional[str],
    thing_nr: Optional[int],
    skjalnr: Optional[int],
    dagsetning: Optional[str],
    url: Optional[str],
    pdf_url: Optional[str],
    fulltext_md: Optional[str] = None,
) -> int:
    """Infogar eller uppdaterar ett þingskjal / lag / reglugerð."""
    if _ar_postgres():
        return _pg_upsert_dok(kilde, malstegund, beteckning, titill,
                               thing_nr, skjalnr, dagsetning, url, pdf_url, fulltext_md)
    return _sq_upsert_dok(kilde, malstegund, beteckning, titill,
                           thing_nr, skjalnr, dagsetning, url, pdf_url, fulltext_md)


def _pg_upsert_dok(kilde, malstegund, beteckning, titill,
                    thing_nr, skjalnr, dagsetning, url, pdf_url, fulltext_md) -> int:
    # Normalisera dagsetning: PostgreSQL DATE kräver YYYY-MM-DD.
    # Källdatan skickar ibland bara ett år (t.ex. '1294' för lagasafn) — komplettera till YYYY-01-01.
    import re as _re
    if dagsetning and _re.match(r'^\d{3,4}$', dagsetning):
        dagsetning = f"{dagsetning}-01-01"

    sql = """
        INSERT INTO island.dokument
            (kilde, malstegund, beteckning, titill, thing_nr, skjalnr,
             dagsetning, url, pdf_url, fulltext_md, cachad_vid)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT (kilde, url) DO UPDATE SET
            titill      = EXCLUDED.titill,
            fulltext_md = EXCLUDED.fulltext_md,
            cachad_vid  = NOW()
        RETURNING id
    """
    with _cursor() as cur:
        cur.execute(sql, (kilde, malstegund, beteckning, titill, thing_nr, skjalnr,
                          dagsetning, url, pdf_url, fulltext_md))
        row = cur.fetchone()
    return row[0]


def _sq_upsert_dok(kilde, malstegund, beteckning, titill,
                    thing_nr, skjalnr, dagsetning, url, pdf_url, fulltext_md) -> int:
    sql = """
        INSERT INTO dokument
            (kilde, malstegund, beteckning, titill, thing_nr, skjalnr,
             dagsetning, url, pdf_url, fulltext_md)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (kilde, url) DO UPDATE SET
            titill      = excluded.titill,
            fulltext_md = excluded.fulltext_md,
            cachad_vid  = datetime('now')
    """
    with _cursor() as cur:
        cur.execute(sql, (kilde, malstegund, beteckning, titill, thing_nr, skjalnr,
                          dagsetning, url, pdf_url, fulltext_md))
        return cur.lastrowid or -1


# ---------------------------------------------------------------------------
# Upsert — dokument_rit
# ---------------------------------------------------------------------------

def upsert_dokument_rit(
    url: str,
    titill: str,
    slug: str,
    dagsetning: Optional[str],
    ministerium: Optional[str],
    tema: Optional[str],
    ar: Optional[int],
    dokumenttyp_isl: Optional[str],
    pdf_url: Optional[str],
    fulltext_md: Optional[str] = None,
) -> str:
    """Infogar eller uppdaterar en post i dokument_rit. Returnerar url (PK)."""
    if _ar_postgres():
        sql = """
            INSERT INTO island.dokument_rit
                (url, titill, slug, dagsetning, ministerium, tema, ar,
                 dokumenttyp_isl, pdf_url, fulltext_md, cachad_vid)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (url) DO UPDATE SET
                titill          = EXCLUDED.titill,
                fulltext_md     = EXCLUDED.fulltext_md,
                cachad_vid      = NOW()
        """
    else:
        sql = """
            INSERT INTO dokument_rit
                (url, titill, slug, dagsetning, ministerium, tema, ar,
                 dokumenttyp_isl, pdf_url, fulltext_md)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (url) DO UPDATE SET
                titill      = excluded.titill,
                fulltext_md = excluded.fulltext_md,
                cachad_vid  = datetime('now')
        """

    with _cursor() as cur:
        cur.execute(sql, (url, titill, slug, dagsetning, ministerium, tema, ar,
                          dokumenttyp_isl, pdf_url, fulltext_md))
    return url


# ---------------------------------------------------------------------------
# Sync-status
# ---------------------------------------------------------------------------

def get_sync_status(kilde: str) -> dict:
    """Returnerar synkstatus för en källa."""
    if _ar_postgres():
        sql = "SELECT kilde, sist_synkad, checksum, detaljer FROM island.sync_status WHERE kilde=%s"
    else:
        sql = "SELECT kilde, sist_synkad, checksum, detaljer FROM sync_status WHERE kilde=?"

    with _cursor() as cur:
        cur.execute(sql, (kilde,))
        row = cur.fetchone()

    if not row:
        return {}

    if _ar_postgres():
        return {"kilde": row[0], "sist_synkad": str(row[1]),
                "checksum": row[2], "detaljer": row[3]}
    return dict(row)


def set_sync_status(kilde: str, checksum: Optional[str] = None, detaljer=None):
    """Uppdaterar synkstatus för en källa."""
    import json
    if isinstance(detaljer, dict):
        detaljer = json.dumps(detaljer)

    if _ar_postgres():
        sql = """
            INSERT INTO island.sync_status (kilde, sist_synkad, checksum, detaljer)
            VALUES (%s, NOW(), %s, %s::jsonb)
            ON CONFLICT (kilde) DO UPDATE SET
                sist_synkad = NOW(),
                checksum    = EXCLUDED.checksum,
                detaljer    = EXCLUDED.detaljer
        """
    else:
        sql = """
            INSERT INTO sync_status (kilde, sist_synkad, checksum, detaljer)
            VALUES (?, datetime('now'), ?, ?)
            ON CONFLICT (kilde) DO UPDATE SET
                sist_synkad = datetime('now'),
                checksum    = excluded.checksum,
                detaljer    = excluded.detaljer
        """

    with _cursor() as cur:
        cur.execute(sql, (kilde, checksum, detaljer))


# ---------------------------------------------------------------------------
# Vektorsökning (kräver PostgreSQL + pgvector)
# ---------------------------------------------------------------------------

def vektor_sok(
    embedding: list[float],
    tabell: str = "chunks",
    max_treff: int = 10,
) -> list[dict]:
    """
    Semantisk sökning via pgvector (cosinuslikhet).
    tabell: 'chunks' (þingskjöl/lög/regl.) eller 'chunks_rit' (rit og skýrslur).
    Kräver PostgreSQL — returnerar tom lista vid SQLite.
    """
    if not _ar_postgres():
        log.warning("vektor_sok: pgvector kräver PostgreSQL.")
        return []

    vec_str = "[" + ",".join(str(float(x)) for x in embedding) + "]"
    schema_tab = f"island.{tabell}"

    if tabell == "chunks":
        join_sql = "JOIN island.dokument d ON d.id = c.dok_id"
        select_extra = "d.kilde, d.malstegund, d.beteckning, d.titill, d.dagsetning, d.url"
    else:
        join_sql = "JOIN island.dokument_rit d ON d.url = c.dok_url"
        select_extra = "d.ministerium, d.dokumenttyp_isl, d.titill, d.dagsetning, d.url"

    sql = f"""
        SELECT c.chunk_index, c.text,
               1 - (c.embedding <=> %s::vector) AS likhet,
               {select_extra}
        FROM   {schema_tab} c
        {join_sql}
        WHERE  c.embedding IS NOT NULL
        ORDER  BY c.embedding <=> %s::vector
        LIMIT  %s
    """

    try:
        with _cursor() as cur:
            cur.execute(sql, [vec_str, vec_str, max_treff])
            rader = cur.fetchall()
            kolumner = [c[0] for c in (cur.description or [])]
    except Exception as exc:
        log.error("vektor_sok misslyckades: %s", exc)
        return []

    return [
        {
            "chunk_index": r[0],
            "text":        r[1],
            "likhet":      round(float(r[2]), 4) if r[2] is not None else 0.0,
            **dict(zip(kolumner[3:], r[3:])),
        }
        for r in rader
    ]
