#!/bin/sh
# Builds the 2 printable pages from the Markdown files. Needs pandoc and WeasyPrint.
# The PDF files in this folder were built with this script.
set -e
cd "$(dirname "$0")"
for lang in en et; do
    pandoc -f markdown -t html5 --standalone \
        --metadata pagetitle="Permissions sheet ($lang)" --metadata lang="$lang" \
        -c print.css "permissions-sheet.$lang.md" -o "permissions-sheet.$lang.html"
    # -p: keep the numbers of the rules (the lists go on from 1 to 9 across 3 blocks)
    weasyprint -p "permissions-sheet.$lang.html" "permissions-sheet.$lang.pdf"
    rm "permissions-sheet.$lang.html"
done
