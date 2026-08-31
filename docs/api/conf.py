# Copyright (c) 2025 Preston Elder
#
# This software is released under the MIT License.
# https://opensource.org/licenses/MIT

"""Sphinx build for the library's API reference.

Only ``door.py`` and ``client.py`` - the two things a consumer imports.
The simulator is a test tool and is documented in prose, where the
explanation matters more than the signatures.

This is a *documentation* deliverable. The machine-readable description
of this API is the shipped type information (PEP 561): ``py.typed`` plus
full annotations, which any type checker or editor already reads without
a docs build. What ``objects.inv`` adds is intersphinx - another Sphinx
project can link to these pages by name.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

project = "pypowerpetdoor"
author = "Preston Elder"
copyright = "2025, Preston Elder"

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
    "sphinx.ext.viewcode",
]

autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "show-inheritance": True,
    "member-order": "bysource",
}
autodoc_typehints = "description"
napoleon_google_docstring = True
napoleon_numpy_docstring = False

intersphinx_mapping = {"python": ("https://docs.python.org/3", None)}

# Smart quotes rewrite `"host"` to `"host"` - fine for prose, wrong for a
# code sample somebody copies out of the API reference.
smartquotes = False

# Untagged code blocks in docstrings are Python; without this the markdown
# builder emits ```default, which no highlighter recognises.
highlight_language = "python"

extensions.append("sphinx_markdown_builder")

html_theme = "sphinx_rtd_theme"
nitpicky = False
exclude_patterns = ["_build"]
