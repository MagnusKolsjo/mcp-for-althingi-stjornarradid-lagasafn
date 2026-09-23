# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
04_rensa_reglugerd.py — Rensar överflödiga förordningsdata i den lokala databasen
MCP-server för isländsk riksdags- och rättsdata

is_sok_reglugerd och is_hamta_reglugerd går direkt mot api.reglugerd.is. Den
lokala kopian (island.dokument, kilde 'reglugerd') används bara för den
semantiska sökningen och behöver text, titel och beteckning.

Äldre synkar lagrade en PDF-länk per förordning som byggdes utan kontroll
(…/current/pdf/). För förordningar utan konsoliderad PDF ger den 404, och
verktygen använder den inte längre. Skriptet nollställer de länkarna och
redovisar poster som saknar text (de ger ingen semantisk sökträff förrän
02_synka_reglugerd.py har fyllt dem).

Kör med --torrkorning först; det visar vad som skulle ändras utan att skriva.
"""

import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)


def rensa(torrkorning: bool) -> dict:
    """Nollställer pdf_url för kilde 'reglugerd'. Returnerar {pdf_lankar, utan_text}."""
    import db as db_mod

    p, ph = db_mod._prefix(), db_mod._ph()
    with db_mod._cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM {p}dokument WHERE kilde = {ph} AND pdf_url IS NOT NULL",
            ("reglugerd",),
        )
        pdf_lankar = cur.fetchone()[0]
        cur.execute(
            f"SELECT count(*) FROM {p}dokument WHERE kilde = {ph} "
            f"AND (fulltext_md IS NULL OR fulltext_md = '')",
            ("reglugerd",),
        )
        utan_text = cur.fetchone()[0]
        if not torrkorning and pdf_lankar:
            cur.execute(
                f"UPDATE {p}dokument SET pdf_url = NULL WHERE kilde = {ph} AND pdf_url IS NOT NULL",
                ("reglugerd",),
            )
    return {"pdf_lankar": pdf_lankar, "utan_text": utan_text}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Nollställer okontrollerade PDF-länkar för förordningar i den lokala "
                    "databasen och redovisar poster utan text.",
    )
    parser.add_argument("--torrkorning", action="store_true",
                        help="visa vad som skulle ändras, utan att skriva")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    r = rensa(args.torrkorning)
    verb = "skulle nollställas" if args.torrkorning else "nollställda"
    print(f"PDF-länkar för förordningar {verb}: {r['pdf_lankar']}")
    print(f"Förordningar utan text (fylls av 02_synka_reglugerd.py): {r['utan_text']}")
