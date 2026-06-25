# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Robustly inject shared client scripts into a plugin UI fragment.

Plugins prepend shared client JS — ``window.PluginJobs`` (job tracking), the alias
pickers, the config form — into their ``ui.html`` fragment so it runs before the
fragment's own handlers. The historical ``html.replace("<script>", …, 1)`` idiom
is a footgun: it matches the first ``<script>`` *substring* anywhere in the file,
including one mentioned in an HTML comment or a string, and silently injects the
client into that wrong spot (or nowhere useful) with **no error** — the symptom is
an ``undefined`` global at click time, far from the cause.

:func:`inject_scripts` targets the first real, inline ``<script>`` *tag* (ignoring
comments and ``src``-ed scripts) and raises if there is none, so a missing or
renamed tag fails loudly instead of dropping the client.
"""

from __future__ import annotations

import re

# Scan for <script ...> opening tags, then exclude any that fall inside an HTML
# comment so a "<script>" written in the fragment's own docs can't be matched.
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_SCRIPT_OPEN_RE = re.compile(r"<script\b[^>]*>", re.IGNORECASE)


def inject_scripts(html: str, *scripts: str) -> str:
    """Prepend client ``scripts`` inside the fragment's first real ``<script>`` tag.

    Args:
        html: The plugin UI fragment.
        scripts: Raw JS bodies (no ``<script>`` tags). Inserted in order, immediately
            after the opening tag of the first inline ``<script>`` element, so the
            fragment's own code can rely on them having run.

    Returns:
        ``html`` with the scripts injected (unchanged if ``scripts`` is empty).

    Raises:
        ValueError: If ``html`` has no real inline ``<script>`` tag to inject into —
            loud by design, so a missing/renamed tag can't silently drop the client.

    """
    if not scripts:
        return html

    comment_spans = [(m.start(), m.end()) for m in _COMMENT_RE.finditer(html)]

    def _in_comment(pos: int) -> bool:
        return any(start <= pos < end for start, end in comment_spans)

    for match in _SCRIPT_OPEN_RE.finditer(html):
        # Skip a <script> mentioned in a comment, and external scripts (a ``src``
        # script ignores inline content, so injecting into it is a silent no-op).
        if _in_comment(match.start()) or "src=" in match.group().lower():
            continue
        insert_at = match.end()
        payload = "\n" + "\n".join(scripts) + "\n"
        return html[:insert_at] + payload + html[insert_at:]

    msg = "inject_scripts: no inline <script> tag found in the fragment to inject into"
    raise ValueError(msg)
