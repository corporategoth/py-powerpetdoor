#!/usr/bin/env bash
# Build the API reference, in both forms, from one Sphinx source.
#
#   docs/api/_build/html/      browsable HTML (and objects.inv, so another
#                              Sphinx project can link here by name)
#   docs/api/_build/markdown/  the same content as markdown, which is what
#                              the wiki publishes and what reads correctly
#                              in an IDE
#
# Neither is committed - both are generated, and `scripts/generate_wiki.py`
# rebuilds the markdown as part of assembling the wiki.
#
# The machine-readable description of this API is not either of these: it
# is the shipped type information (py.typed plus full annotations), which
# any type checker reads without a build.
set -euo pipefail
cd "$(dirname "$0")/.."

# -W on HTML: its warnings are real. Not on markdown - sphinx-markdown-builder
# does not know how to render an `abbreviation` node and says so twice per
# build. The content (a `*` in a signature) survives; the warning is the
# builder's gap, not ours.
uv run --extra docs sphinx-build -q -W -b html docs/api docs/api/_build/html
uv run --extra docs sphinx-build -q -b markdown docs/api docs/api/_build/markdown

echo "html:     docs/api/_build/html/index.html"
echo "markdown: docs/api/_build/markdown/"
