# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Check a plugin's UI fragment against the Hub's styling conventions.

The Hub injects a plugin's ``ui.html`` into its own page as it is, with no iframe or shadow DOM, so
the fragment inherits the Hub's stylesheets and theme. An inline surface style or a hardcoded colour
outranks those stylesheets and looks the same in every theme, and a selector the fragment doesn't
scope reaches the rest of the page. The rules (plugin guide, "Styling & UI Conventions"):

- ``hardcoded-color``: a hex, ``rgb()``/``hsl()`` or named colour in CSS, in a colour attribute
  (``fill="#fff"``) or in a script string. Use the theme's tokens, e.g. ``var(--text-muted)``; a
  fallback inside ``var()`` is allowed.
- ``surface-inline-style``: ``background``, ``box-shadow`` or ``color`` set inline: in a ``style``
  attribute, in one a script builds, or through ``element.style``. Put it in a class; inline layout
  (grid, gap, margin, width) is fine.
- ``important``: ``!important``.
- ``fixed-position``: ``position: fixed``; the Hub owns the viewport.
- ``unscoped-selector``: a rule in the fragment's ``<style>`` whose selector names no class or id of
  the plugin's own (``.<id>-…``), or a ``@keyframes`` name that isn't the plugin's: it reaches the
  rest of the page.
- ``shared-class-restyled``: a rule in the fragment's ``<style>`` that styles a shared ``.plugin-*``
  class. Those belong to the Hub; using one as the context of a selector is fine.

Run it on a plugin, or on every plugin under a folder::

    python -m tlc_plugin_sdk.lint src/
    python -m tlc_plugin_sdk.lint src/tlc_plugin_my_plugin/ui.html

or from a test, with :func:`lint_plugin`.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Finding", "lint_fragment", "lint_plugin", "main"]


@dataclass(frozen=True)
class Finding:
    """One place where a fragment breaks a rule.

    Attributes:
        rule: The rule's name, e.g. ``hardcoded-color``.
        line: 1-based line of the offending text.
        column: 1-based column of the offending text.
        text: The offending text.
        message: What is wrong and what to do instead.
        path: The fragment's file, when the fragment came from one.
    """

    rule: str
    line: int
    column: int
    text: str
    message: str
    path: Path | None = None

    def __str__(self) -> str:
        where = f"{self.path}:" if self.path else ""
        return f"{where}{self.line}:{self.column}: {self.rule}: {self.message}: {self.text}"


_SURFACE_PROPERTIES = frozenset({"background", "background-color", "background-image", "box-shadow", "color"})
# Properties whose value is (or holds) a colour, so a bare word there may be a named colour.
_COLOR_PROPERTY = re.compile(
    r"^(color|background(-color|-image)?|border(-(top|right|bottom|left|block|inline))?(-color)?|outline(-color)?"
    r"|box-shadow|text-shadow|fill|stroke|stop-color|flood-color|lighting-color|caret-color|accent-color"
    r"|text-decoration(-color)?|column-rule(-color)?|scrollbar-color|filter)$"
)
_COLOR_ATTRIBUTES = frozenset({"fill", "stroke", "stop-color", "flood-color", "lighting-color", "color", "bgcolor"})
_NAMED_COLOR_WORDS = (
    "aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood"
    " cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod"
    " darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon"
    " darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray"
    " dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green"
    " greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon"
    " lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon"
    " lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen"
    " magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue"
    " mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy"
    " oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip"
    " peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown"
    " seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal"
    " thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen"
)
_NAMED_COLORS = frozenset(_NAMED_COLOR_WORDS.split())
_HEX = re.compile(r"(?<![\w&#])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})(?![\w-])")
_COLOR_FUNCTION = re.compile(r"(?<![\w-])(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch)\(", re.IGNORECASE)
_WORD = re.compile(r"(?<![\w-])[a-zA-Z]+(?![\w-])")
# Text before a hex-looking script string that makes it a selector or a link, not a colour.
_NOT_A_COLOR_BEFORE = re.compile(
    r"((querySelector(All)?|closest|matches|\$)\(\s*[\"'`]?|href\s*=\s*\\?[\"']?|url\(\s*[\"']?)$"
)

_TAG = re.compile(r"<([a-zA-Z][\w:-]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s>]+))?)*)\s*/?>")
_ATTRIBUTE = re.compile(r"([^\s=>/]+)(?:\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|([^\s>]+)))?")
_RAW_TEXT_OPEN = re.compile(r"<(script|style)\b((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", re.IGNORECASE)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

_IDENTIFIER = re.compile(r"[\w$]+")
_STYLE_IN_STRING = re.compile(r"\bstyle\s*=\s*(\\?[\"'])")
_DECLARATIONS_STRING_BEFORE = re.compile(r"(\.style\.cssText\s*\+?=|setAttribute\(\s*[\"']style[\"']\s*,)\s*$")
_STYLE_ASSIGNMENT = re.compile(r"\.style\.(background\w*|boxShadow|color)\s*=(?!=)")
_STYLE_SET_PROPERTY = re.compile(r"\.style\.setProperty\(\s*([\"'])(background[\w-]*|box-shadow|color)\1")
_POSITION_ASSIGNMENT = re.compile(r"\.style\.position\s*=\s*([\"'])fixed\1")
_REGEX_AFTER = frozenset([*"(,=:[!&|?{};+-*%<>~^", "", "return", "typeof", "case", "do", "else", "in", "of", "new"])
_GROUP_AT_RULES = frozenset({"media", "supports", "container", "layer", "document", "scope", "starting-style"})

_USE_A_TOKEN = "use a theme token, e.g. var(--text-muted), var(--success-bg) or var(--shadow)"
_USE_A_CLASS = "an inline surface style outranks the Hub's theme; set it in a class (inline layout is fine)"
_NO_IMPORTANT = "drop !important; scope the rule instead"
_NO_FIXED = "the Hub owns the viewport; place the element inside the plugin's page"


class _Linter:
    """Walks one fragment and collects its findings."""

    def __init__(self, source: str, plugin_id: str) -> None:
        self.source = source
        # The source with the comments of the CSS read so far blanked: CSS is read from here, at the
        # same offsets, so a commented-out colour or ``;`` is no part of a declaration.
        self.css = list(source)
        self.prefix = plugin_id.strip().lower().replace("_", "-")
        self.findings: dict[tuple[str, int], Finding] = {}
        self._line_starts = [0] + [m.end() for m in re.finditer(r"\n", source)]

    def report(self, rule: str, offset: int, text: str, message: str) -> None:
        if (rule, offset) in self.findings:
            return
        line = _bisect(self._line_starts, offset)
        column = offset - self._line_starts[line - 1] + 1
        self.findings[(rule, offset)] = Finding(rule, line, column, " ".join(text.split())[:80], message)

    def own(self, name: str) -> bool:
        name = name.lower()
        return name == self.prefix or name.startswith(self.prefix + "-")

    def css_text(self, start: int, end: int) -> str:
        return "".join(self.css[start:end])

    def mask_css_comments(self, start: int, end: int) -> None:
        for m in _CSS_COMMENT.finditer(self.source, start, end):
            self.css[m.start() : m.end()] = " " * (m.end() - m.start())

    # ── markup ──────────────────────────────────────────────────

    def markup(self, start: int, end: int) -> None:
        """Lint ``source[start:end]``: its tags, and its ``<style>`` and ``<script>`` blocks."""
        src = self.source
        i = start
        while i < end:
            lt = src.find("<", i, end)
            if lt < 0:
                return
            if src.startswith("<!--", lt):
                close = src.find("-->", lt + 4, end)
                i = end if close < 0 else close + 3
                continue
            raw = _RAW_TEXT_OPEN.match(src, lt, end)
            if raw:
                kind = raw.group(1).lower()
                close_tag = re.compile(rf"</{kind}\s*>", re.IGNORECASE).search(src, raw.end(), end)
                body_end = close_tag.start() if close_tag else end
                self.attributes(raw.group(2), raw.start(2))
                if kind == "style":
                    self.mask_css_comments(raw.end(), body_end)
                    self.css_rules(raw.end(), body_end)
                else:
                    script_type = _attribute_value(raw.group(2), "type").lower()
                    if "html" in script_type or "template" in script_type:
                        self.markup(raw.end(), body_end)
                    elif not script_type or "javascript" in script_type or script_type == "module":
                        self.script(raw.end(), body_end)
                i = close_tag.end() if close_tag else end
                continue
            tag = _TAG.match(src, lt, end)
            if tag:
                self.attributes(tag.group(2), tag.start(2))
                i = tag.end()
            else:
                i = lt + 1

    def attributes(self, text: str, base: int) -> None:
        for m in _ATTRIBUTE.finditer(text):
            name = m.group(1).lower()
            group = next((g for g in (2, 3, 4) if m.group(g) is not None), None)
            if group is None:
                continue
            value_start, value_end = base + m.start(group), base + m.end(group)
            if name == "style":
                self.mask_css_comments(value_start, value_end)
                self.declarations(value_start, value_end, inline=True)
            elif name in _COLOR_ATTRIBUTES:
                self.colors(value_start, value_end, named=True)
            elif name.startswith("on"):
                self.script(value_start, value_end)

    # ── CSS ─────────────────────────────────────────────────────

    def css_rules(self, start: int, end: int, *, keyframes: bool = False) -> None:
        """Lint a run of CSS rules at ``[start, end)``."""
        css = self.css
        i = start
        while i < end:
            while i < end and css[i].isspace():
                i += 1
            if i >= end:
                return
            stop = _scan_css(css, i, end, "{;")
            prelude = self.css_text(i, stop).strip()
            if stop >= end or css[stop] == ";":
                i = stop + 1
                continue
            close = _matching_brace(css, stop, end)
            at = re.match(r"@([\w-]+)\s*(.*)", prelude, re.DOTALL)
            if at and at.group(1).lower().endswith("keyframes"):
                if not self.own(at.group(2).strip()):
                    self.report(
                        "unscoped-selector",
                        i,
                        prelude,
                        f"name the keyframes after the plugin ('{self.prefix}-…'): keyframe names are page-wide",
                    )
                self.css_rules(stop + 1, close, keyframes=True)
            elif at and at.group(1).lower() in _GROUP_AT_RULES:
                self.css_rules(stop + 1, close)
            else:
                if not at and not keyframes:
                    self.selectors(i, stop)
                self.block(stop + 1, close)
            i = close + 1

    def selectors(self, start: int, end: int) -> None:
        text = _blank(self.css_text(start, end), r"\[[^\]]*\]|\"[^\"]*\"|'[^']*'")
        for sel_start, sel_end in _split_top_level(text, ","):
            selector = text[sel_start:sel_end].strip()
            offset = start + sel_start + len(text[sel_start:sel_end]) - len(text[sel_start:sel_end].lstrip())
            names = re.findall(r"[.#](-?[_a-zA-Z][\w-]*)", selector)
            if not any(self.own(n) for n in names):
                self.report(
                    "unscoped-selector",
                    offset,
                    selector,
                    f"scope the rule with a class or id named after the plugin ('.{self.prefix}-…'); "
                    "as it is, it styles the rest of the Hub page",
                )
            subject = re.split(r"\s*[>+~]\s*|\s+", selector)[-1]
            if any(c.startswith("plugin-") for c in re.findall(r"\.(-?[_a-zA-Z][\w-]*)", subject)):
                self.report(
                    "shared-class-restyled",
                    offset,
                    selector,
                    "the shared .plugin-* classes belong to the Hub; style a class of the plugin's own",
                )

    def block(self, start: int, end: int) -> None:
        """Lint a declaration block, nested rules (CSS nesting) included."""
        i = start
        while i < end:
            stop = _scan_css(self.css, i, end, ";{")
            if stop < end and self.css[stop] == "{":
                close = _matching_brace(self.css, stop, end)
                self.block(stop + 1, close)
                i = close + 1
                continue
            self.declaration(i, stop, inline=False)
            i = stop + 1

    def declarations(self, start: int, end: int, *, inline: bool) -> None:
        """Lint a ``;``-separated declaration list, e.g. a ``style`` attribute."""
        for part_start, part_end in _split_top_level(self.css[start:end], ";"):
            self.declaration(start + part_start, start + part_end, inline=inline)

    def declaration(self, start: int, end: int, *, inline: bool) -> None:
        text = self.css_text(start, end)
        colon = text.find(":")
        if colon < 0:
            return
        prop = text[:colon].strip().lower()
        prop_offset = start + len(text) - len(text.lstrip())
        value_start = start + colon + 1
        important = text.find("!important", colon)
        if important >= 0:
            self.report("important", start + important, "!important", _NO_IMPORTANT)
        if prop == "position" and re.search(r"(?<![\w-])fixed(?![\w-])", text[colon:]):
            self.report("fixed-position", prop_offset, text, _NO_FIXED)
        if inline and prop in _SURFACE_PROPERTIES:
            self.report("surface-inline-style", prop_offset, text, _USE_A_CLASS)
        self.colors(value_start, end, named=bool(_COLOR_PROPERTY.match(prop)))

    def colors(self, start: int, end: int, *, named: bool, in_script: bool = False) -> None:
        """Report the hardcoded colours in a CSS value, or in a script string, at ``[start, end)``."""
        text = _blank_var_fallbacks(_blank(self.css_text(start, end), r"url\([^)]*\)"))
        if not in_script:
            text = _blank(text, r"\"[^\"]*\"|'[^']*'")
        for m in _HEX.finditer(text):
            if in_script and _NOT_A_COLOR_BEFORE.search(self.source, max(0, start - 40), start + m.start()):
                continue
            self.report("hardcoded-color", start + m.start(), m.group(), _USE_A_TOKEN)
        for m in _COLOR_FUNCTION.finditer(text):
            close = _matching_paren(text, m.end() - 1)
            if "var(" not in text[m.end() : close]:
                self.report("hardcoded-color", start + m.start(), text[m.start() : close + 1], _USE_A_TOKEN)
        if named:
            for m in _WORD.finditer(text):
                if m.group().lower() in _NAMED_COLORS:
                    self.report("hardcoded-color", start + m.start(), m.group(), _USE_A_TOKEN)

    # ── scripts ─────────────────────────────────────────────────

    def script(self, start: int, end: int) -> None:
        """Lint a script's ``element.style`` writes, and the HTML and CSS its strings hold."""
        strings, code = _scan_script(self.source, start, end)
        for m in _STYLE_ASSIGNMENT.finditer(code, start, end):
            self.report("surface-inline-style", m.start() + 1, m.group().rstrip("= "), _USE_A_CLASS)
        for pattern in (_STYLE_SET_PROPERTY, _POSITION_ASSIGNMENT):
            for m in pattern.finditer(self.source, start, end):
                if code[m.start()] != ".":  # in a string or a comment
                    continue
                if pattern is _STYLE_SET_PROPERTY:
                    self.report("surface-inline-style", m.start() + 1, m.group(), _USE_A_CLASS)
                else:
                    self.report("fixed-position", m.start() + 1, m.group(), _NO_FIXED)
        for s, e, expressions in strings:
            # A template literal is read whole, with its own ${…} blanked, so a style attribute it
            # builds around an expression is still one attribute; the strings inside an expression
            # are strings of their own.
            saved = [(a, self.css[a:b]) for a, b in expressions]
            for a, b in expressions:
                self.css[a:b] = " " * (b - a)
            self.script_string(s, e, code)
            for a, chunk in saved:
                self.css[a : a + len(chunk)] = chunk

    def script_string(self, s: int, e: int, code: str) -> None:
        if _DECLARATIONS_STRING_BEFORE.search(code, max(0, s - 60), s - 1):
            self.declarations(s, e, inline=True)
            return
        text = self.css_text(s, e)
        rest = s
        for m in _STYLE_IN_STRING.finditer(text):
            close = text.find(m.group(1)[-1], m.end())
            value_end = e if close < 0 else s + close - (1 if text[close - 1] == "\\" else 0)
            self.loose_string(rest, s + m.start())
            self.declarations(s + m.end(), value_end, inline=True)
            rest = value_end
        self.loose_string(rest, e)

    def loose_string(self, start: int, end: int) -> None:
        """A script string outside the ``style`` attributes it holds: colours, ``!important``, fixed."""
        text = self.css_text(start, end)
        for m in re.finditer(r"!important", text):
            self.report("important", start + m.start(), m.group(), _NO_IMPORTANT)
        for m in re.finditer(r"position\s*:\s*fixed", text):
            self.report("fixed-position", start + m.start(), m.group(), _NO_FIXED)
        self.colors(start, end, named=False, in_script=True)


def lint_fragment(html: str, plugin_id: str) -> list[Finding]:
    """Check a UI fragment against the Hub's styling conventions.

    Args:
        html: The fragment, as the plugin's ``get_ui_fragment()`` serves it.
        plugin_id: The plugin's manifest ``id``; the fragment's own classes start with it.

    Returns:
        The findings, in the order they appear in the fragment.
    """
    linter = _Linter(html, plugin_id)
    linter.markup(0, len(html))
    return sorted(linter.findings.values(), key=lambda f: (f.line, f.column, f.rule))


def lint_plugin(plugin_dir: str | Path) -> list[Finding]:
    """Check every ``.html`` file of a plugin against the Hub's styling conventions.

    Args:
        plugin_dir: The directory holding the plugin's ``plugin.toml`` (e.g. ``src/tlc_plugin_x``).

    Returns:
        The findings, each with its file's ``path``.
    """
    from tlc_plugin_sdk.harness import read_manifest

    directory = Path(plugin_dir)
    plugin_id = read_manifest(directory).id
    findings: list[Finding] = []
    for path in sorted(_files(directory, lambda name: name.endswith(".html"))):
        text = path.read_text(encoding="utf-8")
        findings += [dataclasses.replace(f, path=path) for f in lint_fragment(text, plugin_id)]
    return findings


def main(argv: list[str] | None = None) -> int:
    """``python -m tlc_plugin_sdk.lint PATH... [--plugin-id ID]``.

    Each path is a plugin's directory, a folder of plugins (every ``plugin.toml`` under it), or one
    fragment (its plugin is the nearest ``plugin.toml`` above it, or ``--plugin-id``).

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        0 when nothing breaks a rule, 1 otherwise, 2 when a path holds no plugin.
    """
    parser = argparse.ArgumentParser(prog="python -m tlc_plugin_sdk.lint", description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="Plugin directories, folders of plugins or fragments")
    parser.add_argument("--plugin-id", default=None, help="The plugin id, for a fragment outside a plugin directory")
    args = parser.parse_args(argv)

    from tlc_plugin_sdk.harness import read_manifest

    findings: list[Finding] = []
    for path in args.paths:
        if path.is_file():
            plugin_id = args.plugin_id
            if plugin_id is None:
                owner = next((p for p in path.resolve().parents if (p / "plugin.toml").is_file()), None)
                if owner is None:
                    print(f"{path}: no plugin.toml above it; pass --plugin-id", file=sys.stderr)
                    return 2
                plugin_id = read_manifest(owner).id
            text = path.read_text(encoding="utf-8")
            findings += [dataclasses.replace(f, path=path) for f in lint_fragment(text, plugin_id)]
            continue
        plugins = sorted(p.parent for p in _files(path, lambda name: name == "plugin.toml"))
        if not plugins:
            print(f"{path}: no plugin.toml in it or under it", file=sys.stderr)
            return 2
        for plugin in plugins:
            findings += lint_plugin(plugin)
    for finding in findings:
        print(finding)
    print(f"{len(findings)} finding{'' if len(findings) == 1 else 's'}", file=sys.stderr)
    return 1 if findings else 0


# ── helpers ─────────────────────────────────────────────────────


def _files(root: Path, wanted: Callable[[str], bool]) -> Iterator[Path]:
    skip = {"node_modules", "__pycache__", "build", "dist"}
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in skip]
        yield from (Path(folder) / f for f in files if wanted(f))


def _bisect(starts: list[int], offset: int) -> int:
    """The 1-based line holding ``offset``, given each line's start offset."""
    lo, hi = 0, len(starts)
    while lo < hi:
        mid = (lo + hi) // 2
        if starts[mid] <= offset:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _blank(text: str, pattern: str) -> str:
    """``text`` with every match of ``pattern`` blanked, offsets kept."""
    return re.sub(pattern, lambda m: " " * len(m.group()), text, flags=re.DOTALL)


def _blank_var_fallbacks(text: str) -> str:
    """Blank the fallback of every ``var(--x, fallback)``: it applies only where the token is missing."""
    out = list(text)
    for m in re.finditer(r"var\(\s*--[\w-]+\s*,", text):
        close = _matching_paren(text, m.start() + 3)
        out[m.end() : close] = " " * max(0, close - m.end())
    return "".join(out)


def _matching_paren(text: str, open_index: int) -> int:
    depth = 0
    for k in range(open_index, len(text)):
        if text[k] == "(":
            depth += 1
        elif text[k] == ")":
            depth -= 1
            if depth == 0:
                return k
    return len(text)


def _scan_css(css: Sequence[str], i: int, end: int, stops: str) -> int:
    """Index of the first of ``stops`` outside brackets and strings, else ``end``."""
    depth = 0
    quote = ""
    while i < end:
        c = css[i]
        if quote:
            if c == "\\":
                i += 1
            elif c == quote:
                quote = ""
        elif c in "\"'":
            quote = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif depth == 0 and c in stops:
            return i
        i += 1
    return end


def _matching_brace(css: Sequence[str], open_index: int, end: int) -> int:
    depth = 0
    i = open_index
    while i < end:
        stop = _scan_css(css, i, end, "{}")
        if stop >= end:
            return end
        depth += 1 if css[stop] == "{" else -1
        if depth == 0:
            return stop
        i = stop + 1
    return end


def _split_top_level(text: Sequence[str], separator: str) -> Iterator[tuple[int, int]]:
    """The ``(start, end)`` of each non-blank part of ``text`` between top-level ``separator``s."""
    i = 0
    while i <= len(text):
        stop = _scan_css(text, i, len(text), separator)
        if "".join(text[i:stop]).strip():
            yield i, stop
        i = stop + 1


def _attribute_value(attributes: str, wanted: str) -> str:
    for m in _ATTRIBUTE.finditer(attributes):
        if m.group(1).lower() == wanted:
            return next((g for g in m.groups()[1:] if g is not None), "")
    return ""


def _scan_script(source: str, start: int, end: int) -> tuple[list[tuple[int, int, list[tuple[int, int]]]], str]:
    """The string literals of the script at ``source[start:end]``, and ``source`` with the script's code alone.

    Returns the ``(start, end, expressions)`` of each string's content, ``expressions`` being the
    ``${…}`` of a template literal, and a copy of ``source`` with the script's comments, string
    contents and regex literals blanked, so a pattern over it matches code only.
    """
    strings: list[tuple[int, int, list[tuple[int, int]]]] = []
    code = list(source)

    def blank(a: int, b: int) -> None:
        code[a:b] = ["\n" if ch == "\n" else " " for ch in source[a:b]]

    def scan(i: int, stop_at_brace: bool) -> int:
        previous = ""
        depth = 0
        while i < end:
            c = source[i]
            if c in "'\"":
                j = i + 1
                while j < end and source[j] != c and source[j] != "\n":
                    j += 2 if source[j] == "\\" else 1
                j = min(j, end)
                strings.append((i + 1, j, []))
                blank(i + 1, j)
                i, previous = j + 1, "a"
            elif c == "`":
                j = run = i + 1
                expressions: list[tuple[int, int]] = []
                while j < end and source[j] != "`":
                    if source[j] == "\\":
                        j += 2
                    elif source.startswith("${", j):
                        blank(run, j)
                        close = scan(j + 2, True)
                        expressions.append((j, min(close + 1, end)))
                        j = run = close + 1
                    else:
                        j += 1
                j = min(j, end)
                strings.append((i + 1, j, expressions))
                blank(run, j)
                i, previous = j + 1, "a"
            elif source.startswith("//", i):
                j = source.find("\n", i, end)
                j = end if j < 0 else j
                blank(i, j)
                i = j
            elif source.startswith("/*", i):
                j = source.find("*/", i + 2, end)
                j = end if j < 0 else j + 2
                blank(i, j)
                i = j
            elif c == "/" and previous in _REGEX_AFTER:
                j = i + 1
                in_class = False
                while j < end and source[j] != "\n" and (source[j] != "/" or in_class):
                    if source[j] == "\\":
                        j += 1
                    elif source[j] in "[]":
                        in_class = source[j] == "["
                    j += 1
                blank(i + 1, min(j, end))
                i, previous = j + 1, "a"
            elif c.isalnum() or c in "_$":
                word = _IDENTIFIER.match(source, i, end)
                previous = word.group() if word else c
                i += len(previous)
            else:
                if stop_at_brace and c == "{":
                    depth += 1
                elif stop_at_brace and c == "}":
                    if depth == 0:
                        return i
                    depth -= 1
                if not c.isspace():
                    previous = c
                i += 1
        return end

    scan(start, False)
    return strings, "".join(code)


if __name__ == "__main__":
    raise SystemExit(main())
