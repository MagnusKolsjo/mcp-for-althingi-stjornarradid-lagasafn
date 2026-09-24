#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
05_konvertera_vektorer.py — Byter embeddings från vector(768) till halfvec(768)

halfvec lagrar varje komponent som 16-bitars flyttal: ungefär hälften av
lagringen per vektor. Träffsäkerheten påverkas inte mätbart för cosinus-
sökning. Vektorindexen byggs om som HNSW (m=16, ef_construction=64), och
dubblettindex som äldre versioner byggde på samma kolumn tas bort.

Servern fungerar före, under (med väntan) och efter konverteringen: den läser
kolumntypen vid varje sökning. Nya och nästan tomma databaser konverteras
automatiskt vid uppstart; det här skriptet är till för befintliga databaser,
där omskrivningen tar tid och kräver diskutrymme.

Så går det till, per tabell (island.chunks, island.chunks_rit):
  1. Vektorindexen på tabellen tas bort (deras operatorklass gäller vector).
  2. ALTER TABLE … TYPE halfvec(768) skriver om tabellen. Den är låst under
     tiden; semantiska sökningar väntar.
  3. Ett HNSW-index byggs.

Kör:
  python3 05_konvertera_vektorer.py --torrkorning          # visa läget och planen
  python3 05_konvertera_vektorer.py                         # båda tabellerna
  python3 05_konvertera_vektorer.py --tabell chunks_rit     # en tabell
  python3 05_konvertera_vektorer.py --bara-index            # bygg om HNSW
"""

import argparse
import logging
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(_SCRIPT_DIR))

from dotenv import load_dotenv

load_dotenv(_SCRIPT_DIR / ".env")

import db  # noqa: E402

log = logging.getLogger("konvertera_vektorer")

# Byte per vektor i tabell + TOAST: vector(768) 4·768+8, halfvec(768) 2·768+8.
SPARAT_PER_VEKTOR = 2 * db.VEKTOR_DIM
# Ungefärlig HNSW-storlek per vektor med m=16 för halfvec(768).
HNSW_BYTE_PER_RAD = 2_100


def _gb(byte: float) -> str:
    return f"{byte / 1024**3:.2f} GB"


def lagesbild(tabell: str) -> dict:
    """Storlekar och index för en tabell, ur katalogen."""
    with db._cursor() as cur:
        cur.execute("""
            SELECT pg_relation_size(c.oid),
                   coalesce(pg_total_relation_size(nullif(c.reltoastrelid, 0)), 0),
                   pg_total_relation_size(c.oid)
            FROM pg_class c WHERE c.oid = %s::regclass""", (f"island.{tabell}",))
        heap, toast, totalt = cur.fetchone()
        cur.execute(f"SELECT count(*), count(embedding) FROM island.{tabell}")
        rader, vektorer = cur.fetchone()
        cur.execute("""
            SELECT i.indexname, am.amname, pg_relation_size(format('%%I.%%I', i.schemaname, i.indexname)::regclass)
            FROM pg_indexes i
            JOIN pg_class ic ON ic.relname = i.indexname
             AND ic.relnamespace = 'island'::regnamespace
            JOIN pg_am am ON am.oid = ic.relam
            WHERE i.schemaname = 'island' AND i.tablename = %s
              AND am.amname IN ('ivfflat', 'hnsw')""", (tabell,))
        index = cur.fetchall()
        typ = db.vektortyp(tabell, cur)
    return {"tabell": tabell, "typ": typ, "rader": rader, "vektorer": vektorer,
            "heap": heap, "toast": toast, "totalt": totalt, "index": index}


def torrkorning(tabeller: list[str]) -> None:
    for tabell in tabeller:
        info = lagesbild(tabell)
        index_nu = sum(i[2] for i in info["index"])
        log.info("island.%s: %s, %d rader (%d vektorer), totalt %s (tabell+TOAST %s, vektorindex %s)",
                 tabell, info["typ"], info["rader"], info["vektorer"], _gb(info["totalt"]),
                 _gb(info["heap"] + info["toast"]), _gb(index_nu))
        for namn, am, storlek in info["index"]:
            anm = ""
            if namn == db.dubblettindex_namn(tabell):
                anm = "  ← dubblett, tas bort"
            log.info("    index %s (%s) %s%s", namn, am, _gb(storlek), anm)
        if info["typ"] == "halfvec":
            har_hnsw = any(am == "hnsw" for _, am, _ in info["index"])
            log.info("  redan halfvec%s", "" if har_hnsw else "; HNSW-index saknas (--bara-index)")
            continue
        tabell_efter = max(info["heap"] + info["toast"] - info["vektorer"] * SPARAT_PER_VEKTOR, 0)
        hnsw = info["vektorer"] * HNSW_BYTE_PER_RAD
        ovrigt = info["totalt"] - info["heap"] - info["toast"] - index_nu
        log.info("  plan: ta bort %d vektorindex (%s), skriv om till halfvec(%d), bygg HNSW",
                 len(info["index"]), _gb(index_nu), db.VEKTOR_DIM)
        log.info("  uppskattning (±50 %%): tabell+TOAST %s, HNSW %s, övriga index %s; "
                 "slutstorlek %s, frigör %s",
                 _gb(tabell_efter), _gb(hnsw), _gb(ovrigt),
                 _gb(tabell_efter + hnsw + ovrigt),
                 _gb(info["totalt"] - (tabell_efter + hnsw + ovrigt)))
        log.info("  tillfälligt diskbehov under omskrivningen: ungefär %s",
                 _gb(tabell_efter + ovrigt))
    log.info("Torrkörning — inget ändrat.")


def konvertera(tabeller: list[str], minne: str, parallella: int, bara_index: bool) -> None:
    for tabell in tabeller:
        with db._cursor() as cur:
            if db.ta_bort_dubblettindex(tabell, cur):
                log.info("[%s] dubblettindexet borttaget", tabell)
            typ = db.vektortyp(tabell, cur)
        if not bara_index and typ == "vector":
            start = time.time()
            log.info("[%s] skriver om till halfvec; tabellen är låst under tiden...", tabell)
            with db._cursor() as cur:
                db.konvertera_till_halfvec(tabell, cur)
            log.info("[%s] omskrivning klar på %.0f s", tabell, time.time() - start)
        start = time.time()
        log.info("[%s] bygger HNSW-index (maintenance_work_mem=%s)...", tabell, minne)
        db.bygg_vektorindex(tabell, minne=minne, parallella=parallella)
        log.info("[%s] index klart på %.0f s", tabell, time.time() - start)
        with db._cursor() as cur:
            cur.execute(f"ANALYZE island.{tabell}")
            cur.execute("SELECT pg_total_relation_size(%s::regclass)", (f"island.{tabell}",))
            log.info("[%s] efter konverteringen: %s", tabell, _gb(cur.fetchone()[0]))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Byter embeddings till halfvec(768) med HNSW-index och tar bort "
                    "dubblettindex i island.chunks och island.chunks_rit.",
    )
    parser.add_argument("--torrkorning", action="store_true",
                        help="visa läge, plan och uppskattad storlek; ändrar inget")
    parser.add_argument("--tabell", choices=["chunks", "chunks_rit", "bada"], default="bada",
                        help="vilken tabell (standard: båda)")
    parser.add_argument("--minne", default="2GB",
                        help="maintenance_work_mem för HNSW-bygget (standard 2GB)")
    parser.add_argument("--parallella", type=int, default=2,
                        help="parallella arbetare för indexbygget (standard 2)")
    parser.add_argument("--bara-index", action="store_true",
                        help="bygg bara om HNSW-indexen, ingen typkonvertering")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s",
                        datefmt="%H:%M:%S")
    if not db._ar_postgres():
        log.error("Skriptet kräver PostgreSQL med pgvector.")
        sys.exit(1)
    tabeller = list(db.VEKTORTABELLER) if args.tabell == "bada" else [args.tabell]
    if args.torrkorning:
        torrkorning(tabeller)
        return
    konvertera(tabeller, args.minne, args.parallella, args.bara_index)
    log.info("Klar.")


if __name__ == "__main__":
    main()
