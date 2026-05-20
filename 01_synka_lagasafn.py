# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
synka_lagasafn.py — Bulk-synk av isländsk lagstiftning från althingi.is/lagasafn/
MCP-server för isländsk riksdags- och rättsdata

Laddar ner lagasafn bulk-ZIP (nuna/allt.zip), parsar alla HTML-lagfiler och
lagrar fulltext + metadata i island.dokument.

────────────────────────────────────────────────────────────────────────────────
Bulk-ZIP-format:
  URL:       https://www.althingi.is/lagasafn/zip/nuna/allt.zip
  Storlek:   8.8 MB
  Innehåll:  ~1 788 filer varav 1 708 lagfiler + index/bilder
  Filnamn:   {ar}{nr}.html  t.ex. 1944033.html (lag nr 33/1944)
             År är alltid 4 siffror, nr är 3+ siffror (nr > 999 paddas ej)

HTML-struktur (gammal men konsistent — se lagasafn.py för detaljer):
  Lagtext-behållare: div.article.box.login > div.boxbody
  Kapitelrubriker:   <b>I.</b> → ## I.
  Paragrafnummer:    <b>1. gr.</b> → **1. gr.**
  Paragraftext:      img[src="/lagas/hk.jpg"].tail
  Ändringshistorik:  <small> (inkluderas som kursiv not)

Versioner: 'nuna' (gällande, standard). Historiska versioner (per riksmöte)
kan hämtas genom att byta version-parametern — se hamta_log_lista_versioner()
i lagasafn.py. Bulk-synk körs med 'nuna' och ersätter befintliga poster.

robots.txt: Crawl-delay: 5 — tillämpas INTE på bulk-ZIP (en enda fil).
User-Agent: mcp-for-althingi-stjornarradid-lagasafn/1.0 krävs.
────────────────────────────────────────────────────────────────────────────────
"""

import io
import logging
import re
import time
import zipfile
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

log = logging.getLogger(__name__)

ZIP_URL  = "https://www.althingi.is/lagasafn/zip/{version}/allt.zip"
HEADERS  = {
    "User-Agent": "mcp-for-althingi-stjornarradid-lagasafn/1.0 (+https://github.com/MagnusKolsjo/mcp-for-althingi-stjornarradid-lagasafn)",
    "Accept":     "*/*",
}

# Filnamns-mönster: {ar:4d}{nr:3+d}.html — t.ex. 1944033.html, 2020100.html
# Index-sidor och bilder filtreras bort via detta mönster.
_LAG_FILNAMN_RE = re.compile(r'^(\d{4})(\d{3,})\.html$', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Nedladdning
# ---------------------------------------------------------------------------

def ladda_ner_zip(version: str = "nuna") -> bytes:
    """
    Laddar ner bulk-ZIP för angiven version.
    version: 'nuna' (gällande) eller riksmötessuffix som '157a', '156b', '155'.
    Returnerar ZIP-innehållet som bytes.
    """
    url = ZIP_URL.format(version=version)
    log.info("Laddar ner %s ...", url)
    start = time.monotonic()
    try:
        r = httpx.get(url, headers=HEADERS, timeout=120, follow_redirects=True)
        r.raise_for_status()
        elapsed = time.monotonic() - start
        log.info("ZIP nedladdad: %.1f MB på %.1f s",
                 len(r.content) / 1_048_576, elapsed)
        return r.content
    except Exception as exc:
        log.error("Kunde inte ladda ner ZIP från %s: %s", url, exc)
        raise


# ---------------------------------------------------------------------------
# Filnamnsparsning
# ---------------------------------------------------------------------------

def _parsa_filnamn(filnamn: str) -> Optional[tuple[int, int]]:
    """
    Parsar ar och nr ur ett lagasafn-filnamn.

    Filnamnsformat: {ar:4d}{nr:3+d}.html
      1944033.html  → (1944, 33)
      2020100.html  → (2020, 100)
      20201000.html → (2020, 1000)   (nr > 999 saknar nollpadding)

    Returnerar (ar, nr) eller None om filnamnet inte matchar.
    """
    m = _LAG_FILNAMN_RE.match(Path(filnamn).name)
    if not m:
        return None
    ar  = int(m.group(1))
    nr  = int(m.group(2))
    # Sanity-check: ar 1200-2030, nr 1-9999
    if not (1200 <= ar <= 2030 and 1 <= nr <= 9999):
        return None
    return ar, nr


# ---------------------------------------------------------------------------
# Synk
# ---------------------------------------------------------------------------

def synka_lagasafn(version: str = "nuna") -> dict:
    """
    Laddar ner lagasafn bulk-ZIP, parsar alla HTML-lagfiler och lagrar
    metadata + fulltext i island.dokument.

    Flöde:
      1. Ladda ner {version}/allt.zip (8.8 MB för 'nuna').
      2. Öppna ZIP i minnet (ingen disk-I/O).
      3. För varje HTML-fil med giltigt lagnamn:
           a. Parsa ar/nr ur filnamnet.
           b. Extrahera titill + fulltext_md med lagasafn._parse_lagtext().
           c. Upsert i island.dokument via db.upsert_dokument().
      4. Uppdatera island.sync_status.

    Parametrar:
      version — 'nuna' (standard) eller riksmötessuffix ('157a', '156b', ...).

    Returnerar statistikdict: {version, totalt, upsertade, tomma, fel}

    OBS: Kör INTE via bash-verktyget — kör i Magnus terminal:
      python3 synka_lagasafn.py
    Tar ca 5–10 min för 1 708 lagar (parsing-begränsad, inget crawl-delay).
    """
    import db as db_mod
    from lagasafn import _parse_lagtext, _hamta_html   # type: ignore
    from lxml import html as lhtml                      # type: ignore

    # Initiera schema
    db_mod.initiera_schema()

    stats = {
        "version":   version,
        "totalt":    0,
        "upsertade": 0,
        "tomma":     0,
        "fel":       [],
    }

    # ── Steg 1: Ladda ner ZIP ────────────────────────────────────────────────
    try:
        zip_bytes = ladda_ner_zip(version)
    except Exception as exc:
        stats["fel"].append({"fil": "allt.zip", "fel": str(exc)})
        return stats

    # ── Steg 2+3: Parsa och lagra ────────────────────────────────────────────
    log.info("Parsning startar ...")
    start = time.monotonic()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        namelist = zf.namelist()
        lag_filer = [
            n for n in namelist
            if _parsa_filnamn(Path(n).name) is not None
        ]
        log.info("ZIP innehåller %d filer, %d lagfiler identifierade",
                 len(namelist), len(lag_filer))

        for filnamn in lag_filer:
            stats["totalt"] += 1
            parsed = _parsa_filnamn(Path(filnamn).name)
            if not parsed:
                continue
            ar, nr = parsed

            try:
                html_bytes = zf.read(filnamn)
                tree       = lhtml.fromstring(html_bytes)
                titill, fulltext_md = _parse_lagtext(tree)

                if not titill and not fulltext_md:
                    log.debug("Tom lag: %s", filnamn)
                    stats["tomma"] += 1
                    continue

                beteckning = f"{nr}/{ar}"
                url        = (
                    f"https://www.althingi.is/lagas/{version}/{ar}{nr:03d}.html"
                )

                db_mod.upsert_dokument(
                    kilde      = "lagasafn",
                    malstegund = "log",
                    beteckning = beteckning,
                    titill     = titill,
                    thing_nr   = None,
                    skjalnr    = None,
                    dagsetning = str(ar),
                    url        = url,
                    pdf_url    = None,
                    fulltext_md = fulltext_md,
                )
                stats["upsertade"] += 1

                if stats["upsertade"] % 100 == 0:
                    elapsed = time.monotonic() - start
                    log.info(
                        "  %d / %d lagar upsertade (%.0f s)",
                        stats["upsertade"], len(lag_filer), elapsed,
                    )

            except Exception as exc:
                log.error("Fel vid parsning av %s: %s", filnamn, exc)
                stats["fel"].append({"fil": filnamn, "fel": str(exc)})

    # ── Steg 4: sync_status ──────────────────────────────────────────────────
    try:
        db_mod.set_sync_status(
            f"lagasafn_{version}",
            checksum=str(stats["upsertade"]),
            detaljer=stats,
        )
    except Exception as exc:
        log.warning("Kunde inte uppdatera sync_status: %s", exc)

    elapsed = time.monotonic() - start
    log.info(
        "synka_lagasafn klar — version=%s, upsertade=%d, tomma=%d, "
        "fel=%d, tid=%.0f s",
        version, stats["upsertade"], stats["tomma"],
        len(stats["fel"]), elapsed,
    )
    return stats



# ---------------------------------------------------------------------------
# Schemainstallation — genererar och installerar launchd-plist dynamiskt
# ---------------------------------------------------------------------------

def _installera_launchd_schema() -> None:
    """
    Genererar launchd-plist med korrekt hemsökväg och installerar den i
    ~/Library/LaunchAgents/. Anropas med flaggan --installera-schema.

    Plist-ID: se.magnuskolsjo.mcp-island-synk-daglig
    Schema:   dagligen 03:00 (kör vid uppvakning om datorn sov)
    """
    import plistlib

    hem  = Path.home()
    mapp = hem / "MCP-Servers" / "island"
    logs = mapp / "logs"

    plist_id = "se.magnuskolsjo.mcp-island-synk-daglig"
    plist_innehall = {
        "Label": plist_id,
        "ProgramArguments": ["/bin/bash", str(mapp / "synk_daglig.sh")],
        "StartCalendarInterval": {"Hour": 3, "Minute": 0},
        "RunAtLoad": False,
        "StandardOutPath": str(logs / "launchd.out"),
        "StandardErrorPath": str(logs / "launchd.err"),
    }

    agents_mapp = hem / "Library" / "LaunchAgents"
    agents_mapp.mkdir(parents=True, exist_ok=True)
    plist_fil = agents_mapp / f"{plist_id}.plist"

    with open(plist_fil, "wb") as fh:
        plistlib.dump(plist_innehall, fh)

    print(f"Plist skriven: {plist_fil}")
    print("\nAktivera med:")
    print(f"  launchctl load {plist_fil}")
    print("\nInaktivera med:")
    print(f"  launchctl unload {plist_fil}")

# ---------------------------------------------------------------------------
# Direkt körning — python3 synka_lagasafn.py [--version 157a]
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    if "--installera-schema" in sys.argv:
        _installera_launchd_schema()
        sys.exit(0)

    version = "nuna"
    if "--version" in sys.argv:
        idx = sys.argv.index("--version")
        if idx + 1 < len(sys.argv):
            version = sys.argv[idx + 1]

    resultat = synka_lagasafn(version=version)
    print(
        f"\nSynk klar:\n"
        f"  Version:    {resultat['version']}\n"
        f"  Totalt:     {resultat['totalt']}\n"
        f"  Upsertade:  {resultat['upsertade']}\n"
        f"  Tomma:      {resultat['tomma']}\n"
        f"  Fel:        {len(resultat['fel'])}"
    )
    if resultat["fel"]:
        print("\nFeldetaljer (max 5):")
        for f in resultat["fel"][:5]:
            print(f"  {f['fil']}: {f['fel']}")
