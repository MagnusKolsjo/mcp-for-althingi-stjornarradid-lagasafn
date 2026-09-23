# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Magnus Kolsjö

"""
takt.py — Gemensam strypning av anrop per värd.

althingi.py och lagasafn.py anropar båda althingi.is. Crawl-delay gäller
värden, inte modulen, så de delar en strypning via vänta("althingi.is", 5.0).

Varje anrop reserverar nästa tillåtna starttid under ett lås
(nästa = max(nu, nästa) + intervall) och sover sedan utanför låset till den
reserverade tiden. Starttiderna hamnar därmed minst `intervall` isär även när
flera trådar anropar samtidigt, och en tråd som sover blockerar inte andra
från att reservera sin plats i kön.

Ingångspunkt:
    vanta(vard, intervall) -> None
"""

import threading
import time

# Trådar vaknar inte exakt på utsatt tid. Utan marginal kan en tråd som vaknar
# några millisekunder sent följas av en som vaknar i tid, så att de faktiska
# starterna hamnar strax under intervallet.
MARGINAL = 0.05

_las = threading.Lock()
_nasta_start: dict[str, float] = {}


def vanta(vard: str, intervall: float) -> None:
    """Väntar tills nästa anrop mot `vard` får starta, och reserverar tiden."""
    with _las:
        nu    = time.monotonic()
        start = max(nu, _nasta_start.get(vard, 0.0))
        _nasta_start[vard] = start + intervall + MARGINAL
    vila = start - time.monotonic()
    if vila > 0:
        time.sleep(vila)
