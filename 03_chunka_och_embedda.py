#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
03_chunka_och_embedda.py — Chunkning och embedding för isländsk riksdags- och rättsdata

Läser fulltext_md från island.dokument (þingskjöl, lög, reglugerðir) och
island.dokument_rit (rit og skýrslur), delar upp i stycken och genererar
vektorer med:

  intfloat/multilingual-e5-base (768 dim) → embedding

Vektorerna lagras i:
  island.chunks      (dok_id FK → island.dokument)
  island.chunks_rit  (dok_url FK → island.dokument_rit)

Kräver PostgreSQL + pgvector — SQLite-fallback saknar vektorsökning.

Användning:
  python3 03_chunka_och_embedda.py                       # Alla utan chunks
  python3 03_chunka_och_embedda.py --kalla dokument      # Bara þingskjöl/lög/regl.
  python3 03_chunka_och_embedda.py --kalla rit           # Bara rit og skýrslur
  python3 03_chunka_och_embedda.py --tvinga              # Återskapa befintliga
  python3 03_chunka_och_embedda.py --bygg-index          # Bygg om HNSW-indexen
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

_SCRIPT_DIR = Path(__file__).parent.resolve()
load_dotenv(_SCRIPT_DIR / ".env")

sys.path.insert(0, str(_SCRIPT_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Konfiguration ──────────────────────────────────────────────────────────────

EMBEDDING_MODEL  = os.getenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-base")
CHUNK_MAX_TECKEN = int(os.getenv("CHUNK_MAX_TECKEN",        "800"))
CHUNK_MIN_TECKEN = int(os.getenv("CHUNK_MIN_TECKEN",        "100"))
EMBEDDING_BATCH  = int(os.getenv("EMBEDDING_BATCH_STORLEK", "32"))

_modell = None


# ---------------------------------------------------------------------------
# Modell-laddning (FD1-skyddad)
# ---------------------------------------------------------------------------

def _hamta_modell():
    """
    Laddar intfloat/multilingual-e5-base lazily.
    Skyddar FD 1 mot tqdm/transformers-utskrifter som annars kraschar
    MCP stdio-protokollet om skriptet körs via MCP.
    """
    global _modell
    if _modell is not None:
        return _modell

    log.info("Laddar embeddingmodell: %s", EMBEDDING_MODEL)

    log_sokvag = _SCRIPT_DIR / "logs" / "embedding.log"
    log_sokvag.parent.mkdir(parents=True, exist_ok=True)
    log_fd   = os.open(str(log_sokvag), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    save_fd1 = os.dup(1)
    try:
        os.dup2(log_fd, 1)
        from sentence_transformers import SentenceTransformer
        _modell = SentenceTransformer(EMBEDDING_MODEL)
    finally:
        os.dup2(save_fd1, 1)
        os.close(save_fd1)
        os.close(log_fd)

    log.info("Embeddingmodell laddad")
    return _modell


# ---------------------------------------------------------------------------
# Chunkning
# ---------------------------------------------------------------------------

def chunka_text(text: str) -> list[dict]:
    """
    Delar upp text i stycken på ~CHUNK_MAX_TECKEN tecken.

    Splittningsstrategi (i fallande prioritet):
      1. §-/kapitelrubriker (##, ###) — naturliga gränser i lagtext-markdown
      2. Dubbla radbrytningar (styckebrytning)
      3. Meningsgränser (. ? !) om stycket fortfarande är för långt
      4. Hårt snitt på CHUNK_MAX_TECKEN om inget bättre alternativ finns

    Returnerar lista med dicts:
      {chunk_index, text, tecken_start, tecken_slut}
    """
    if not text:
        return []

    # Steg 1: dela på kapitel-/paragrafrubriker
    delar = re.split(r"(?=\n##\s)", text)

    stycken: list[str] = []
    for del_ in delar:
        del_ = del_.strip()
        if not del_:
            continue
        if len(del_) <= CHUNK_MAX_TECKEN:
            stycken.append(del_)
        else:
            # Dela ytterligare på dubbla radbrytningar
            understycken = re.split(r"\n{2,}", del_)
            nuvarande = ""
            for us in understycken:
                us = us.strip()
                if not us:
                    continue
                if len(nuvarande) + len(us) + 2 <= CHUNK_MAX_TECKEN:
                    nuvarande = (nuvarande + "\n\n" + us).strip() if nuvarande else us
                else:
                    if nuvarande:
                        stycken.append(nuvarande)
                    if len(us) > CHUNK_MAX_TECKEN:
                        # Dela på meningsgränser
                        meningar = re.split(r"(?<=[.?!])\s+", us)
                        nuvarande = ""
                        for m in meningar:
                            if len(nuvarande) + len(m) + 1 <= CHUNK_MAX_TECKEN:
                                nuvarande = (nuvarande + " " + m).strip() if nuvarande else m
                            else:
                                if nuvarande:
                                    stycken.append(nuvarande)
                                while len(m) > CHUNK_MAX_TECKEN:
                                    stycken.append(m[:CHUNK_MAX_TECKEN])
                                    m = m[CHUNK_MAX_TECKEN:]
                                nuvarande = m
                        if nuvarande:
                            stycken.append(nuvarande)
                            nuvarande = ""
                    else:
                        nuvarande = us
            if nuvarande:
                stycken.append(nuvarande)

    # Bygg chunks med positionsinfo
    chunks = []
    pos   = 0
    index = 0
    for s in stycken:
        s = s.strip()
        if len(s) < CHUNK_MIN_TECKEN:
            continue
        idx   = text.find(s[:40], pos)
        start = idx if idx >= 0 else pos
        slut  = start + len(s)
        chunks.append({
            "chunk_index":  index,
            "text":         s,
            "tecken_start": start,
            "tecken_slut":  slut,
        })
        pos = max(pos, slut)
        index += 1

    return chunks


# ---------------------------------------------------------------------------
# Databas — hämtning
# ---------------------------------------------------------------------------

def _hamta_dokument_att_embeda(kalla: str, tvinga: bool) -> list[dict]:
    """
    Returnerar island.dokument (þingskjöl/lög/regl.) som saknar embedding.
    Hoppas över dokument som saknar fulltext_md.
    """
    import db
    if not db._ar_postgres():
        log.error("Embedding kräver PostgreSQL — SQLite-backend stöder inte pgvector.")
        return []

    villkor = ["d.fulltext_md IS NOT NULL", "d.fulltext_md != ''"]
    if not tvinga:
        villkor.append(
            f"NOT EXISTS (SELECT 1 FROM {db._prefix()}chunks c WHERE c.dok_id = d.id AND c.embedding IS NOT NULL)"
        )

    with db._cursor() as cur:
        cur.execute(
            f"""
            SELECT d.id, d.kilde, d.malstegund, d.beteckning, d.titill,
                   length(d.fulltext_md) AS teckenlangd
            FROM   island.dokument d
            WHERE  {' AND '.join(villkor)}
            ORDER  BY d.id
            """
        )
        rader = cur.fetchall()

    return [
        {"id": r[0], "kilde": r[1], "malstegund": r[2],
         "beteckning": r[3], "titill": r[4], "teckenlangd": r[5]}
        for r in rader
    ]


def _hamta_rit_att_embeda(tvinga: bool) -> list[dict]:
    """
    Returnerar island.dokument_rit (rit og skýrslur) som saknar embedding.
    Hoppas över poster som saknar fulltext_md.
    """
    import db
    if not db._ar_postgres():
        return []

    villkor = ["d.fulltext_md IS NOT NULL", "d.fulltext_md != ''"]
    if not tvinga:
        villkor.append(
            f"NOT EXISTS (SELECT 1 FROM {db._prefix()}chunks_rit c WHERE c.dok_url = d.url AND c.embedding IS NOT NULL)"
        )

    with db._cursor() as cur:
        cur.execute(
            f"""
            SELECT d.url, d.titill, d.ministerium, d.dokumenttyp_isl, d.ar,
                   length(d.fulltext_md) AS teckenlangd
            FROM   island.dokument_rit d
            WHERE  {' AND '.join(villkor)}
            ORDER  BY d.ar DESC, d.url
            """
        )
        rader = cur.fetchall()

    return [
        {"url": r[0], "titill": r[1], "ministerium": r[2],
         "dokumenttyp_isl": r[3], "ar": r[4], "teckenlangd": r[5]}
        for r in rader
    ]


def _hamta_fulltext_dok(dok_id: int) -> str | None:
    """Hämtar fulltext_md för ett island.dokument."""
    import db
    with db._cursor() as cur:
        cur.execute("SELECT fulltext_md FROM island.dokument WHERE id = %s", (dok_id,))
        rad = cur.fetchone()
    return rad[0] if rad else None


def _hamta_fulltext_rit(dok_url: str) -> str | None:
    """Hämtar fulltext_md för ett island.dokument_rit."""
    import db
    with db._cursor() as cur:
        cur.execute("SELECT fulltext_md FROM island.dokument_rit WHERE url = %s", (dok_url,))
        rad = cur.fetchone()
    return rad[0] if rad else None


# ---------------------------------------------------------------------------
# Databas — sparning
# ---------------------------------------------------------------------------

def _spara_chunks_dok(dok_id: int, chunks: list[dict], embeddings):
    """Sparar chunks i island.chunks (FK: dok_id INTEGER)."""
    import db
    with db._cursor() as cur:
        typ = db.vektortyp("chunks", cur)
        cur.execute(f"DELETE FROM {db._prefix()}chunks WHERE dok_id = %s", (dok_id,))
        for ch, emb in zip(chunks, embeddings):
            vec_str = "[" + ",".join(str(float(x)) for x in emb) + "]"
            cur.execute(
                f"""
                INSERT INTO {db._prefix()}chunks (dok_id, chunk_index, text, embedding)
                VALUES (%s, %s, %s, %s::{typ})
                ON CONFLICT (dok_id, chunk_index) DO UPDATE SET
                    text      = EXCLUDED.text,
                    embedding = EXCLUDED.embedding
                """,
                (dok_id, ch["chunk_index"], ch["text"], vec_str),
            )


def _spara_chunks_rit(dok_url: str, chunks: list[dict], embeddings):
    """Sparar chunks i island.chunks_rit (FK: dok_url TEXT)."""
    import db
    with db._cursor() as cur:
        typ = db.vektortyp("chunks_rit", cur)
        cur.execute(f"DELETE FROM {db._prefix()}chunks_rit WHERE dok_url = %s", (dok_url,))
        for ch, emb in zip(chunks, embeddings):
            vec_str = "[" + ",".join(str(float(x)) for x in emb) + "]"
            cur.execute(
                f"""
                INSERT INTO {db._prefix()}chunks_rit (dok_url, chunk_index, text, embedding)
                VALUES (%s, %s, %s, %s::{typ})
                ON CONFLICT (dok_url, chunk_index) DO UPDATE SET
                    text      = EXCLUDED.text,
                    embedding = EXCLUDED.embedding
                """,
                (dok_url, ch["chunk_index"], ch["text"], vec_str),
            )


# ---------------------------------------------------------------------------
# Embedding av ett enskilt dokument
# ---------------------------------------------------------------------------

def _embeda(titel: str | None, text: str, embed_fn) -> tuple[list[dict], object]:
    """Chunkar text och embeddar den. Returnerar (chunks, embeddings)."""
    chunks = chunka_text(text)
    if not chunks:
        return [], None

    modell = _hamta_modell()
    texter = [
        f"{titel}\n\n{ch['text']}" if titel else ch["text"]
        for ch in chunks
    ]

    log_sokvag = _SCRIPT_DIR / "logs" / "embedding.log"
    log_fd   = os.open(str(log_sokvag), os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    save_fd1 = os.dup(1)
    try:
        os.dup2(log_fd, 1)
        embeddings = modell.encode(
            texter,
            batch_size=EMBEDDING_BATCH,
            show_progress_bar=False,
            normalize_embeddings=True,
        )
    finally:
        os.dup2(save_fd1, 1)
        os.close(save_fd1)
        os.close(log_fd)

    embed_fn(chunks, embeddings)
    return chunks, embeddings


# ---------------------------------------------------------------------------
# Huvudkörning
# ---------------------------------------------------------------------------

def kor_embedding_dokument(tvinga: bool = False) -> dict:
    """Embeddar island.dokument → island.chunks."""
    dokument = _hamta_dokument_att_embeda("dokument", tvinga)
    if not dokument:
        log.info("Inga island.dokument att embeda.")
        return {"total": 0, "lyckade": 0, "hoppade": 0, "fel": 0, "chunks": 0}

    log.info("Embeddar %d dokument (island.dokument)...", len(dokument))
    stat = {"total": len(dokument), "lyckade": 0, "hoppade": 0, "fel": 0, "chunks": 0}

    for dok in dokument:
        try:
            text = _hamta_fulltext_dok(dok["id"])
            if not text:
                stat["hoppade"] += 1
                continue

            chunks, _ = _embeda(
                dok.get("titill"), text,
                lambda chs, embs: _spara_chunks_dok(dok["id"], chs, embs),
            )

            if not chunks:
                stat["hoppade"] += 1
            else:
                stat["lyckade"] += 1
                stat["chunks"]  += len(chunks)
                log.debug(
                    "OK  id=%-6s  %3d chunks  [%s]  %s",
                    dok["id"], len(chunks), dok.get("kilde", ""),
                    (dok.get("titill") or dok.get("beteckning") or "")[:55],
                )
        except Exception as exc:
            stat["fel"] += 1
            log.warning("FEL  id=%s: %s", dok["id"], exc)

    log.info(
        "[DOKUMENT] Klar — lyckade: %d, hoppade: %d, fel: %d, chunks: %d",
        stat["lyckade"], stat["hoppade"], stat["fel"], stat["chunks"],
    )
    return stat


def kor_embedding_rit(tvinga: bool = False) -> dict:
    """Embeddar island.dokument_rit → island.chunks_rit."""
    dokument = _hamta_rit_att_embeda(tvinga)
    if not dokument:
        log.info("Inga island.dokument_rit att embeda.")
        return {"total": 0, "lyckade": 0, "hoppade": 0, "fel": 0, "chunks": 0}

    log.info("Embeddar %d rit og skýrslur (island.dokument_rit)...", len(dokument))
    stat = {"total": len(dokument), "lyckade": 0, "hoppade": 0, "fel": 0, "chunks": 0}

    for dok in dokument:
        try:
            text = _hamta_fulltext_rit(dok["url"])
            if not text:
                stat["hoppade"] += 1
                continue

            chunks, _ = _embeda(
                dok.get("titill"), text,
                lambda chs, embs: _spara_chunks_rit(dok["url"], chs, embs),
            )

            if not chunks:
                stat["hoppade"] += 1
            else:
                stat["lyckade"] += 1
                stat["chunks"]  += len(chunks)
                log.debug(
                    "OK  url=%.60s  %3d chunks",
                    dok["url"], len(chunks),
                )
        except Exception as exc:
            stat["fel"] += 1
            log.warning("FEL  url=%s: %s", dok["url"], exc)

    log.info(
        "[RIT] Klar — lyckade: %d, hoppade: %d, fel: %d, chunks: %d",
        stat["lyckade"], stat["hoppade"], stat["fel"], stat["chunks"],
    )
    return stat


# ---------------------------------------------------------------------------
# Vektorindex
# ---------------------------------------------------------------------------

def bygg_vektorindex(minne: str | None = None) -> None:
    """
    Bygger om HNSW-indexen för island.chunks och island.chunks_rit (operator-
    klass efter kolumntyp) och tar bort eventuella dubblettindex.

    Behövs sällan: HNSW tål inskrivningar, så den dagliga synken kräver ingen
    ombyggnad. Konverteringen till halfvec görs av 05_konvertera_vektorer.py.
    """
    import db

    for tabell in db.VEKTORTABELLER:
        with db._cursor() as cur:
            db.ta_bort_dubblettindex(tabell, cur)
        log.info("Bygger HNSW-index för island.%s...", tabell)
        db.bygg_vektorindex(tabell, minne=minne)
    log.info("HNSW-index klara.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=(
            "Chunkar och embeddar isländska riksdags- och rättsdokument.\n"
            f"Modell: {EMBEDDING_MODEL} (768 dim)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--kalla",
        default="alla",
        choices=["alla", "dokument", "rit"],
        help=(
            "Källfilter:\n"
            "  alla      — island.dokument + island.dokument_rit (standard)\n"
            "  dokument  — þingskjöl, lög, reglugerðir (island.dokument)\n"
            "  rit       — rit og skýrslur (island.dokument_rit)"
        ),
    )
    parser.add_argument(
        "--tvinga",
        action="store_true",
        help="Återskapa chunks även för dokument som redan är embeddade",
    )
    parser.add_argument(
        "--bygg-index",
        action="store_true",
        help="Bygg om HNSW-indexen för island.chunks och island.chunks_rit",
    )
    parser.add_argument(
        "--minne",
        default=None,
        help="maintenance_work_mem för indexbygget, t.ex. 2GB",
    )
    args = parser.parse_args()

    import db
    db.initiera_schema()

    total_stat: dict[str, dict] = {}

    if args.kalla in ("alla", "dokument"):
        total_stat["dokument"] = kor_embedding_dokument(tvinga=args.tvinga)

    if args.kalla in ("alla", "rit"):
        total_stat["rit"] = kor_embedding_rit(tvinga=args.tvinga)

    print("\n── Resultat ──────────────────────────────────────────")
    for kat, s in total_stat.items():
        print(
            f"  [{kat.upper():10s}]  lyckade: {s['lyckade']}, "
            f"hoppade: {s['hoppade']}, fel: {s['fel']}, "
            f"chunks: {s['chunks']}"
        )
    print()

    if args.bygg_index:
        bygg_vektorindex(args.minne)
