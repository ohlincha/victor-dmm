#!/bin/sh
# Build the user manual: docs/MANUAL.md -> docs/MANUAL.html (shipped with the program, Help > Manual)
# and docs/VictorDMM-Manual.pdf (printable). Needs pandoc and Google Chrome (or Chromium).
set -eu
cd "$(dirname "$0")/../docs"
pandoc MANUAL.md -s --embed-resources --metadata title="VICTOR DMM manual" -c manual.css -o MANUAL.html
CHROME=$(command -v google-chrome || command -v chromium || command -v chromium-browser)
"$CHROME" --headless --disable-gpu --no-pdf-header-footer --print-to-pdf="$PWD/VictorDMM-Manual.pdf" "file://$PWD/MANUAL.html"
echo "wrote docs/MANUAL.html and docs/VictorDMM-Manual.pdf"
