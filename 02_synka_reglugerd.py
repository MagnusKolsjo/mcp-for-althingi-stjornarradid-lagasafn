# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
02_synka_reglugerd.py — Synk av isländska förordningar för semantisk sökning
MCP-server för isländsk riksdags- och rättsdata

Verktygen is_sok_reglugerd och is_hamta_reglugerd går direkt mot källan
(fritextsökning respektive text ur detaljsvaret). Den lokala kopian behövs
bara för den semantiska sökningen (is_sok_i_dokument): texten lagras i
island.dokument (kilde 'reglugerd') så att 03_chunka_och_embedda.py kan bygga
chunks och embeddings av den.

────────────────────────────────────────────────────────────────────────────────
Källa:

  GET /api/v1/regulations/all/current/full
    → alla förordningar (~6 200) med metadata och `text` (HTML) i ett svar,
      ~57 MB. Ett anrop per synk i stället för ett per förordning.

Texten görs om till markdown. Har en förordnings text ändrats sedan förra
synken tas dess chunks bort, så att embeddingsteget bygger om dem. Oförändrade
förordningar berörs inte.

Kör:  python3 02_synka_reglugerd.py
────────────────────────────────────────────────────────────────────────────────
"""

import logging
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import reglugerd as rg

log = logging.getLogger(__name__)


def _normalisera_beteckning(name: str) -> str:
    """'0179/2018' → '179/2018'. Andra format lämnas orörda."""
    try:
        nr, ar = name.split("/")
        return f"{int(nr)}/{int(ar)}"
    except Exception:
        return name


def synka_reglugerd(max_antal: int | None = None) -> dict:
    """
    Hämtar alla förordningar med text och lagrar dem i island.dokument.

    Parametrar:
      max_antal — begränsa antalet förordningar som skrivs, t.ex. vid test.

    Returnerar statistikdict: {totalt, nya, andrade, oforandrade, utan_text, fel}
    """
    import db as db_mod

    db_mod.initiera_schema()
    stats: dict = {"totalt": 0, "nya": 0, "andrade": 0, "oforandrade": 0,
                   "utan_text": 0, "fel": []}
    start = time.monotonic()

    try:
        poster = rg.hamta_alla_med_text()
    except Exception as exc:
        log.error("Bulkhämtningen från api.reglugerd.is misslyckades: %s", exc)
        stats["fel"].append({"name": None, "fel": str(exc)})
        return stats

    if max_antal is not None:
        poster = poster[:max_antal]
    stats["totalt"] = len(poster)
    log.info("Hämtade %d förordningar", len(poster))

    for post in poster:
        name = post["name"]
        if not name or "/" not in name:
            continue
        nr_del, ar_del = name.split("/", 1)
        try:
            url = f"{rg.API_BASE}/regulation/{int(nr_del):04d}-{ar_del}/current/"
        except ValueError:
            continue
        if not post["text_md"]:
            stats["utan_text"] += 1
        try:
            utfall = db_mod.upsert_reglugerd(
                beteckning  = _normalisera_beteckning(name),
                titill      = post["titill"],
                dagsetning  = post["publicerad"],
                url         = url,
                fulltext_md = post["text_md"] or None,
            )
        except Exception as exc:
            log.error("Upsert-fel för %s: %s", name, exc)
            stats["fel"].append({"name": name, "fel": str(exc)})
            continue
        stats[{"ny": "nya", "andrad": "andrade"}.get(utfall, "oforandrade")] += 1

    try:
        db_mod.set_sync_status("reglugerd", detaljer={
            k: (len(v) if isinstance(v, list) else v) for k, v in stats.items()
        })
    except Exception as exc:
        log.warning("Kunde inte uppdatera sync_status: %s", exc)

    log.info(
        "synka_reglugerd klar — nya=%d, ändrade=%d, oförändrade=%d, fel=%d, tid=%.0f s",
        stats["nya"], stats["andrade"], stats["oforandrade"], len(stats["fel"]),
        time.monotonic() - start,
    )
    return stats


if __name__ == "__main__":
    import argparse
    import sys

    # Tolkas innan något körs, så att --help och okända flaggor aldrig
    # startar en synk.
    parser = argparse.ArgumentParser(
        description="Synk av isländska förordningar (med text) från api.reglugerd.is "
                    "för den semantiska sökningen.",
    )
    parser.add_argument("--max-antal", type=int, metavar="N",
                        help="skriv bara de N första förordningarna, t.ex. vid test")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    resultat = synka_reglugerd(max_antal=args.max_antal)
    print(
        f"\nSynk klar:\n"
        f"  Totalt:       {resultat['totalt']}\n"
        f"  Nya:          {resultat['nya']}\n"
        f"  Ändrade:      {resultat['andrade']}\n"
        f"  Oförändrade:  {resultat['oforandrade']}\n"
        f"  Utan text:    {resultat['utan_text']}\n"
        f"  Fel:          {len(resultat['fel'])}"
    )
    for f in resultat["fel"][:5]:
        print(f"  {f}")
    sys.exit(1 if resultat["fel"] or not resultat["totalt"] else 0)
