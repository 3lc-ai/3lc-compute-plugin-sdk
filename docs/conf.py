"""Sphinx configuration for the 3lc-plugin-sdk docs.

Minimal, autodoc-driven: the API reference is generated from the package's own
docstrings (Google-style, via napoleon), and the Markdown author guides render
in-place via myst-parser. Build with::

    uv run --with-requirements docs/requirements.txt \
        sphinx-build -b html docs docs/_build/html
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

# Importable from a raw checkout too (CI installs the package, but this keeps a
# bare `sphinx-build` working without a prior `uv sync`).
sys.path.insert(0, os.path.abspath("../src"))

# -- Project information ------------------------------------------------------
project = "3lc-plugin-sdk"
author = "3LC"
copyright = "2026 3LC Inc."  # noqa: A001 — Sphinx-reserved name

try:
    release = _pkg_version("3lc-plugin-sdk")
except PackageNotFoundError:  # raw checkout, never installed
    release = "0.0.0"
version = release

# -- General configuration ----------------------------------------------------
extensions = [
    "sphinx.ext.autodoc",       # pull docstrings from the package
    "sphinx.ext.napoleon",      # understand Google-style Args/Returns/Raises
    "sphinx.ext.intersphinx",   # cross-link to Python's stdlib docs
    "sphinx.ext.viewcode",      # [source] links next to documented objects
    "myst_parser",              # author guides are Markdown
]

# .md -> MyST, .rst -> reStructuredText (the eval-rst blocks in api.md).
source_suffix = {".rst": "restructuredtext", ".md": "markdown"}

# Generate anchors for h1-h3 headings so intra-guide links like
# [...](#long-running-jobs-run_jobctx) resolve.
myst_heading_anchors = 3

# The author guide cross-links a couple of docs that live only in the private
# 3lc-insights monorepo (plugin-isolation.md, plugin-migration.md). Those targets
# don't exist in this repo, so don't let `-W` turn the dangling links into a build
# failure. This is scoped to MyST link resolution only — autodoc/reference errors
# stay fatal. Remove once the guide's cross-doc links are repointed or inlined.
suppress_warnings = ["myst.xref_missing"]

templates_path: list[str] = []
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# -- Autodoc ------------------------------------------------------------------
autodoc_default_options = {
    "members": True,
    "undoc-members": True,
    "show-inheritance": True,
    "member-order": "bysource",
}
# Keep type hints in the signature, not duplicated into the description.
autodoc_typehints = "signature"

# Render Google-style `Attributes:` sections as an inline field list rather than
# separate py:attribute objects — avoids "duplicate object description" when a
# documented attribute is also a real class member.
napoleon_use_ivar = True

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- HTML output --------------------------------------------------------------
html_theme = "furo"
html_title = f"3lc-plugin-sdk {release}"
