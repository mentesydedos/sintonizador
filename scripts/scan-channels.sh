#!/usr/bin/env bash
# scan-channels.sh — initial ATSC OTA channel scan
# Outputs ./channels.conf in DVBv5 format, ready to be parsed by the Python service.

set -euo pipefail

ADAPTER="${1:-0}"
OUT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/channels.conf"

# DVBv5 ships ATSC frequency tables under /usr/share/dvb/atsc/
TABLE="/usr/share/dvb/atsc/us-ATSC-center-frequencies-8VSB"
[[ -f "$TABLE" ]] || TABLE="/usr/share/dvbv5/dvb-t/us-ATSC-center-frequencies-8VSB"
[[ -f "$TABLE" ]] || { echo "ERROR: ATSC frequency table not found. Install dvb-tools/dtv-scan-tables."; exit 1; }

echo "Scanning ATSC on adapter $ADAPTER using $TABLE"
echo "Output: $OUT"
echo

# Input format = DVBV5 (default). La tabla `us-ATSC-center-frequencies-8VSB`
# de dtv-scan-tables viene con secciones [CHANNEL] y KEY=VALUE, que es DVBV5.
# Output también DVBV5 (default) — formato más rico, ideal para el wrapper Python.
dvbv5-scan -a "$ADAPTER" -o "$OUT" "$TABLE"

echo
echo "Done. Found channels:"
grep -c "^\[" "$OUT" || true
