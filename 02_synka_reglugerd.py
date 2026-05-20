# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
synka_reglugerd.py — Bulk-synk av isländska förordningar från api.reglugerd.is
MCP-server för isländsk riksdags- och rättsdata

Laddar ner metadata för alla förordningar via sök-API:t och lagrar i island.dokument.
PDF-fulltext hämtas inte vid bulk-synk — on-demand via MCP-verktyget is_hamta_reglugerd.

────────────────────────────────────────────────────────────────────────────────
API-struktur:

  GET /api/v1/years
    → lista med år som strängar, t.ex. ['1912', '1916', ..., '2026'] (96 år)

  GET /api/v1/search?year={ar}&page={sida}
    → { page, perPage(30), totalPages, totalItems, data: [...] }
    OBS: tom q ger 0 resultat — sök alltid per år, aldrig med tom fraga.

  Varje post i data:
    { "name": "1424/2020", "title": "Reglugerð um ...",
      "publishedDate": "2020-12-30", "ministry": "..." }

  PDF-URL: https://api.reglugerd.is/api/v1/regulation/{nr:04d}-{ar}/current/pdf/

Totalt ~3 000–5 000 förordningar (skattat från stickprov per år).
Inga robots.txt-restriktioner — API utan crawl-delay.
OBS: Kör INTE via bash-verktyget — kör i Magnus terminal:
  python3 synka_reglugerd.py
Tar ca 2–5 min (nätverks-begränsad, 96 år × paginering).
────────────────────────────────────────────────────────────────────────────────
"""

import logging
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

API_BASE = "https://api.reglugerd.is/api/v1"
HEADERS  = {
    "User-Agent": "mcp-for-althingi-stjornarradid-lagasafn/1.0 (+https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn)",
    "Accept":     "application/json",
}
PER_PAGE    = 30   # API:ts standardvärde
SLEEP_AR    = 0.5  # sekunder mellan år (artighetspausi)
SLEEP_PAGE  = 0.3  # sekunder mellan sidor inom ett år


# ---------------------------------------------------------------------------
# Internfunktioner
# ---------------------------------------------------------------------------

def _hamta_ar_lista(client: httpx.Client) -> list[str]:
    """Hämtar alla tillgängliga år från /api/v1/years, sorterade stigande."""
    r = client.get(f"{API_BASE}/years", headers=HEADERS, timeout=30)
    r.raise_for_status()
    ar_lista = r.json()
    return sorted(ar_lista)  # '1912' → '2026'


def _hamta_sida(client: httpx.Client, ar: str, sida: int) -> dict:
    """
    Hämtar en sida sökresultat för givet år.
    Returnerar rå API-dict med page, perPage, totalPages, totalItems, data.
    """
    r = client.get(
        f"{API_BASE}/search",
        params={"year": ar, "page": sida},
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _bygg_pdf_url(nr_str: str, ar_str: str) -> str:
    """
    Bygger PDF-URL från name-fält (t.ex. '179/2018' eller '1424/2020').
    Format: /api/v1/regulation/{nr:04d}-{ar}/current/pdf/
    """
    try:
        nr_del, ar_del = nr_str.split("/")
        nr_int = int(nr_del)
        return f"{API_BASE}/regulation/{nr_int:04d}-{ar_del}/current/pdf/"
    except Exception:
        return ""


def _normalisera_beteckning(name: str) -> str:
    """
    Normaliserar name-fältet från API ('1424/2020') till 'nr/år'-format.
    API:t returnerar redan detta format — validering mot heltal.
    """
    try:
        nr, ar = name.split("/")
        return f"{int(nr)}/{int(ar)}"
    except Exception:
        return name


# ---------------------------------------------------------------------------
# Synk
# ---------------------------------------------------------------------------

def synka_reglugerd() -> dict:
    """
    Hämtar metadata för alla isländska förordningar från api.reglugerd.is
    och lagrar i island.dokument via db.upsert_dokument().

    Strategi:
      1. Hämta alla tillgängliga år från /api/v1/years.
      2. För varje år, paginera /api/v1/search?year={ar}.
      3. Upserta varje förordning (utan PDF-fulltext — on-demand via MCP).
      4. Uppdatera island.sync_status.

    Returnerar statistikdict: {totalt, upsertade, tomma_ar, fel}

    OBS: Kör INTE via bash-verktyget — kör i Magnus terminal:
      python3 synka_reglugerd.py
    """
    import db as db_mod

    db_mod.initiera_schema()

    stats = {
        "totalt":    0,
        "upsertade": 0,
        "tomma_ar":  0,
        "fel":       [],
    }

    start = time.monotonic()

    with httpx.Client(follow_redirects=True) as client:

        # ── Steg 1: Hämta år ────────────────────────────────────────────────
        try:
            ar_lista = _hamta_ar_lista(client)
        except Exception as exc:
            log.error("Kunde inte hämta år-lista: %s", exc)
            stats["fel"].append({"ar": "years", "fel": str(exc)})
            return stats

        log.info("Hittade %d år: %s – %s", len(ar_lista), ar_lista[0], ar_lista[-1])

        # ── Steg 2+3: Paginera per år ────────────────────────────────────────
        for ar in ar_lista:
            ar_total  = 0
            ar_upsert = 0
            sida      = 1

            while True:
                try:
                    svar = _hamta_sida(client, ar, sida)
                except Exception as exc:
                    log.warning("Fel vid hämtning av år=%s sida=%d: %s", ar, sida, exc)
                    stats["fel"].append({"ar": ar, "sida": sida, "fel": str(exc)})
                    break

                total_sidor = svar.get("totalPages", 0)
                total_items = svar.get("totalItems", 0)
                data        = svar.get("data", [])

                if total_items == 0:
                    # År saknar förordningar (t.ex. luckor i historiken)
                    stats["tomma_ar"] += 1
                    break

                for post in data:
                    stats["totalt"] += 1
                    ar_total += 1

                    name      = post.get("name", "")
                    titill    = post.get("title", "")
                    publicerad = post.get("publishedDate", "")
                    ministerium = post.get("ministry", "")

                    if not name:
                        log.debug("Post utan name: %s", post)
                        continue

                    beteckning = _normalisera_beteckning(name)
                    pdf_url    = _bygg_pdf_url(name, ar)
                    url        = f"{API_BASE}/regulation/{name.replace('/', '-').zfill(9)}/current/"

                    # Bygg url på enklare sätt: {nr:04d}-{ar}
                    try:
                        nr_del, ar_del = name.split("/")
                        nr_int = int(nr_del)
                        url = f"{API_BASE}/regulation/{nr_int:04d}-{ar_del}/current/"
                    except Exception:
                        pass

                    try:
                        db_mod.upsert_dokument(
                            kilde      = "reglugerd",
                            malstegund = "reglugerd",
                            beteckning = beteckning,
                            titill     = titill,
                            thing_nr   = None,
                            skjalnr    = None,
                            dagsetning = publicerad,
                            url        = url,
                            pdf_url    = pdf_url,
                            fulltext_md = None,  # on-demand via MCP
                        )
                        stats["upsertade"] += 1
                        ar_upsert += 1
                    except Exception as exc:
                        log.error("Upsert-fel för %s: %s", name, exc)
                        stats["fel"].append({"name": name, "fel": str(exc)})

                # Nästa sida?
                if sida >= total_sidor:
                    break
                sida += 1
                time.sleep(SLEEP_PAGE)

            if ar_upsert > 0:
                log.debug("  År %s: %d/%d upsertade", ar, ar_upsert, ar_total)

            time.sleep(SLEEP_AR)

        # Mellanlogg var 500:e post
        if stats["upsertade"] % 500 == 0 and stats["upsertade"] > 0:
            elapsed = time.monotonic() - start
            log.info("  %d upsertade (%.0f s)", stats["upsertade"], elapsed)

    # ── Steg 4: sync_status ──────────────────────────────────────────────────
    try:
        db_mod.set_sync_status(
            "reglugerd",
            checksum=str(stats["upsertade"]),
            detaljer=stats,
        )
    except Exception as exc:
        log.warning("Kunde inte uppdatera sync_status: %s", exc)

    elapsed = time.monotonic() - start
    log.info(
        "synka_reglugerd klar — upsertade=%d, tomma_ar=%d, fel=%d, tid=%.0f s",
        stats["upsertade"], stats["tomma_ar"], len(stats["fel"]), elapsed,
    )
    return stats


# ---------------------------------------------------------------------------
# Direkt körning — python3 synka_reglugerd.py
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    resultat = synka_reglugerd()
    print(
        f"\nSynk klar:\n"
        f"  Totalt:     {resultat['totalt']}\n"
        f"  Upsertade:  {resultat['upsertade']}\n"
        f"  Tomma år:   {resultat['tomma_ar']}\n"
        f"  Fel:        {len(resultat['fel'])}"
    )
    if resultat["fel"]:
        print("\nFeldetaljer (max 5):")
        for f in resultat["fel"][:5]:
            print(f"  {f}")
