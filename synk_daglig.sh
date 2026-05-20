#!/bin/bash
# synk_daglig.sh — Daglig synk av isländsk riksdags- och rättsdata.
#
# Körordning:
#   1. Lagasafn — konsoliderade lagar (inkrementell upsert)
#   2. Reglugerðir — förordningar (inkrementell upsert)
#   3. Rit og skýrslur — regeringspublikationer (hoppar poster med befintlig fulltext_md)
#   4. Chunkning + embedding för nya/uppdaterade dokument (hoppar befintliga chunks)
#
# Anropas av launchd varje dag kl. 03:00 (kör vid uppvakning om datorn sov).
# Kör manuellt: bash ~/MCP-Servers/island/synk_daglig.sh

set -euo pipefail

MAPP="$HOME/MCP-Servers/island"
LOG_DIR="$MAPP/logs"
mkdir -p "$LOG_DIR"

# En loggfil per dag — gör det lätt att hitta vad som hände vilket datum
LOG_FIL="$LOG_DIR/synk-$(date +%Y-%m-%d).log"
exec >> "$LOG_FIL" 2>&1

echo
echo "===== $(date '+%Y-%m-%d %H:%M:%S') — daglig synk startar ====="

# Rensa loggfiler äldre än 30 dagar
find "$LOG_DIR" -name "synk-*.log" -mtime +30 -delete

# Ladda .env om den finns
if [ -f "$MAPP/.env" ]; then
    set -a
    source "$MAPP/.env"
    set +a
fi

cd "$MAPP"

# Välj Python-tolk — kan styras via PYTHON_SOKVAG i .env
PYTHON="${PYTHON_SOKVAG:-$HOME/MCP-Servers/.venv/bin/python3}"

# ---------------------------------------------------------------------------
# Steg 1: Lagasafn (inkrementell upsert av konsoliderade lagar)
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 1: Lagasafn"
"$PYTHON" "$MAPP/01_synka_lagasafn.py" || {
    echo "[$(date '+%H:%M:%S')] Steg 1 felade — avbryter"
    exit 1
}
echo "[$(date '+%H:%M:%S')] Steg 1 klar"

# ---------------------------------------------------------------------------
# Steg 2: Reglugerðir (inkrementell upsert av förordningar)
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 2: Reglugerðir"
"$PYTHON" "$MAPP/02_synka_reglugerd.py" || {
    echo "[$(date '+%H:%M:%S')] Steg 2 felade — avbryter"
    exit 1
}
echo "[$(date '+%H:%M:%S')] Steg 2 klar"

# ---------------------------------------------------------------------------
# Steg 3: Rit og skýrslur (hoppar poster med befintlig fulltext_md)
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 3: Rit og skýrslur (stjornarradid.is)"
"$PYTHON" "$MAPP/stjornarradid_rit.py" || {
    echo "[$(date '+%H:%M:%S')] Steg 3 felade — avbryter"
    exit 1
}
echo "[$(date '+%H:%M:%S')] Steg 3 klar"

# ---------------------------------------------------------------------------
# Steg 4: Chunkning + embedding (hoppar dokument med befintlig embedding)
# ---------------------------------------------------------------------------
echo "[$(date '+%H:%M:%S')] Steg 4: Chunkning och embedding"
"$PYTHON" "$MAPP/03_chunka_och_embedda.py" || {
    echo "[$(date '+%H:%M:%S')] Steg 4 felade — avbryter"
    exit 1
}
echo "[$(date '+%H:%M:%S')] Steg 4 klar"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') — daglig synk klar ====="
