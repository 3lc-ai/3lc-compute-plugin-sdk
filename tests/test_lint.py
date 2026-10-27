# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The UI fragment lint: what each rule reports, and what it leaves alone.

A fragment is injected into the Hub page as it is, so an inline surface style or a hardcoded colour
fights the theme and an unscoped selector reaches the rest of the page. The lint reports those; it
must not report a link, an id selector, a ``var()`` fallback or a commented-out colour, or a plugin
author stops trusting it.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from tlc_plugin_sdk.lint import Finding, lint_fragment, lint_plugin, main


def _rules(html: str, plugin_id: str = "demo") -> list[tuple[str, str]]:
    return [(f.rule, f.text) for f in lint_fragment(textwrap.dedent(html), plugin_id)]


def _style(css: str) -> str:
    return f"<style>{css}</style>"


# ── hardcoded-color ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("css", "colour"),
    [
        (".demo-a { color: #fff; }", "#fff"),
        (".demo-a { border: 1px solid #12345678; }", "#12345678"),
        (".demo-a { box-shadow: 0 1px 2px rgba(0,0,0,.04); }", "rgba(0,0,0,.04)"),
        (".demo-a { background: hsl(200 50% 50%); }", "hsl(200 50% 50%)"),
        (".demo-a { color: white; }", "white"),
        (".demo-a { border-color: Tomato; }", "Tomato"),
    ],
)
def test_a_colour_in_the_style_block_is_reported(css: str, colour: str) -> None:
    assert _rules(_style(css)) == [("hardcoded-color", colour)]


@pytest.mark.parametrize(
    "css",
    [
        ".demo-a { color: var(--text-muted); }",
        ".demo-a { background: var(--bg, #f3f4f6); }",
        ".demo-a { background: rgb(var(--accent-rgb) / 0.1); }",
        ".demo-a { color: currentColor; border-color: transparent; }",
        "#demo-panel { color: var(--text); }",
        ".demo-a { animation: demo-pulse 1s; font-family: Orange, sans-serif; }",
        ".demo-a { background: url(#fff); }",
        ".demo-a { /* was #fff, now a token */ color: var(--text); }",
        ".demo-a { content: '#fff'; }",
    ],
)
def test_a_token_a_fallback_or_a_word_that_is_not_a_colour_passes(css: str) -> None:
    assert _rules(_style(css)) == []


def test_a_colour_attribute_is_reported_and_current_color_passes() -> None:
    html = '<svg><path fill="#4e79a7" stroke="currentColor"/><circle fill="none"/></svg>'
    assert _rules(html) == [("hardcoded-color", "#4e79a7")]


def test_a_link_and_a_comment_are_not_colours() -> None:
    html = """
        <!-- the old badge was <div style="color:#fff"> -->
        <a href="#add">Add</a>
        <p>Issue #12 is fixed</p>
    """
    assert _rules(html) == []


def test_a_colour_in_a_script_string_is_reported() -> None:
    html = """
        <script>
          ctx.fillStyle = '#ff0000';
          var shadow = "0 1px 2px rgba(0,0,0,.2)";
        </script>
    """
    assert _rules(html) == [("hardcoded-color", "#ff0000"), ("hardcoded-color", "rgba(0,0,0,.2)")]


def test_a_selector_a_link_a_regex_or_a_comment_in_a_script_is_not_a_colour() -> None:
    html = """
        <script>
          document.querySelector('#add').hidden = true;
          el.innerHTML = '<a href="#bed">Bed</a>';
          var hex = /#fff/g;  // was '#fff'
          /* ctx.fillStyle = '#000'; */
          var n = 4 / 2, m = n / 2;
        </script>
    """
    assert _rules(html) == []


# ── surface-inline-style ────────────────────────────────────────


def test_an_inline_surface_style_is_reported_and_inline_layout_passes() -> None:
    html = '<div style="display:grid; gap:10px; margin-top:14px; color:var(--text-muted); background:var(--bg)"></div>'
    assert _rules(html) == [
        ("surface-inline-style", "color:var(--text-muted)"),
        ("surface-inline-style", "background:var(--bg)"),
    ]


def test_box_shadow_inline_is_reported_with_its_colour() -> None:
    assert _rules('<div style="box-shadow: 0 1px 2px #000"></div>') == [
        ("surface-inline-style", "box-shadow: 0 1px 2px #000"),
        ("hardcoded-color", "#000"),
    ]


def test_a_style_attribute_a_script_builds_is_linted() -> None:
    html = """
        <script>
          html += '<span class="dot" style="background:' + colour + '"></span>';
          html += "<b style=\\"color: var(--error)\\">x</b>";
          html += `<i style="width:${pct}%; color:${c}">y</i>`;
        </script>
    """
    assert _rules(html) == [
        ("surface-inline-style", "background:"),
        ("surface-inline-style", "color: var(--error)"),
        ("surface-inline-style", "color:"),
    ]


def test_element_style_writes_are_linted() -> None:
    html = """
        <script>
          hint.style.color = 'var(--error)';
          card.style.backgroundColor = c;
          box.style.setProperty('box-shadow', 'none');
          bar.style.cssText = 'width:10%;background:var(--accent)';
          bar.style.width = '10%';
          if (hint.style.color === '') {}
          var c = hint.style.color;
        </script>
    """
    assert _rules(html) == [
        ("surface-inline-style", ".style.color"),
        ("surface-inline-style", ".style.backgroundColor"),
        ("surface-inline-style", ".style.setProperty('box-shadow'"),
        ("surface-inline-style", "background:var(--accent)"),
    ]


def test_a_surface_property_in_the_style_block_is_fine() -> None:
    assert _rules(_style(".demo-card { background: var(--bg-card); color: var(--text); }")) == []


# ── important / fixed-position ──────────────────────────────────


def test_important_is_reported_wherever_it_is() -> None:
    html = """
        <style>.demo-a { margin: 0 !important; }</style>
        <div style="padding: 0 !important"></div>
        <script>el.style.cssText = 'gap: 0 !important';</script>
    """
    assert [rule for rule, _ in _rules(html)] == ["important", "important", "important"]


def test_position_fixed_is_reported_and_other_positions_pass() -> None:
    html = """
        <style>.demo-modal { position: fixed; } .demo-tip { position: absolute; }</style>
        <div style="position:sticky"></div>
        <script>overlay.style.position = 'fixed'; tip.style.position = 'relative';</script>
    """
    assert [rule for rule, _ in _rules(html)] == ["fixed-position", "fixed-position"]


# ── unscoped-selector / shared-class-restyled ───────────────────


@pytest.mark.parametrize(
    "selector",
    [
        ".demo-chip",
        ".demo",
        "#demo-panel",
        ".plugin-page .demo-chip",
        ".demo-row label",
        ".demo-row .dot",
        ".demo-row input[type=range]",
        ".demo-chip:hover > .x",
        ".merge-chip .demo",
    ],
)
def test_a_selector_scoped_by_a_class_or_id_of_the_plugin_passes(selector: str) -> None:
    assert _rules(_style(selector + " { margin: 0; }")) == []


@pytest.mark.parametrize("selector", [".chip", ".demonstration", "table td", ":root", "body", "#panel"])
def test_a_selector_without_a_class_or_id_of_the_plugin_is_unscoped(selector: str) -> None:
    assert _rules(_style(selector + " { margin: 0; }")) == [("unscoped-selector", selector)]


def test_each_selector_of_a_list_is_scoped_on_its_own() -> None:
    assert _rules(_style(".a .b, .demo-c .d { margin: 0; }")) == [("unscoped-selector", ".a .b")]


def test_the_plugin_id_is_matched_in_kebab_case() -> None:
    assert _rules(_style(".image-metrics-card { margin: 0; }"), plugin_id="image_metrics") == []


def test_a_shared_class_as_the_subject_is_restyled_and_as_the_context_is_not() -> None:
    css = (
        ".demo-wrap .plugin-progress-bar { height: 4px; } .plugin-page .demo-x { height: 4px; }"
        " .plugin-hero { margin: 0; }"
    )
    assert _rules(_style(css)) == [
        ("shared-class-restyled", ".demo-wrap .plugin-progress-bar"),
        ("shared-class-restyled", ".plugin-hero"),
        ("unscoped-selector", ".plugin-hero"),
    ]


def test_keyframes_are_named_after_the_plugin() -> None:
    css = "@keyframes demo-pulse { 0%,100% { opacity: 1 } } @keyframes pulse { from { color: #fff } }"
    assert _rules(_style(css)) == [
        ("unscoped-selector", "@keyframes pulse"),
        ("hardcoded-color", "#fff"),
    ]


def test_rules_inside_media_queries_and_nesting_are_linted() -> None:
    css = """
        @media (max-width: 600px) { .chip { color: red; } }
        .demo-card { margin: 0; &:hover { box-shadow: 0 0 2px #000; } }
    """
    assert _rules(_style(css)) == [
        ("unscoped-selector", ".chip"),
        ("hardcoded-color", "red"),
        ("hardcoded-color", "#000"),
    ]


# ── positions, files and the CLI ────────────────────────────────


def test_a_finding_points_at_its_line_and_column() -> None:
    html = '<p>\n  <span style="gap:2px;color:#fff">x</span>\n</p>'
    findings = lint_fragment(html, "demo")
    assert [(f.rule, f.line, f.column) for f in findings] == [
        ("surface-inline-style", 2, 24),
        ("hardcoded-color", 2, 30),
    ]
    assert str(findings[1]).startswith("2:30: hardcoded-color: ")


def _plugin(tmp_path: Path, plugin_id: str, html: str) -> Path:
    directory = tmp_path / f"tlc_plugin_{plugin_id}"
    directory.mkdir()
    (directory / "plugin.toml").write_text(
        f'id = "{plugin_id}"\n\n[runtime]\nentrypoint = "tlc_plugin_{plugin_id}:Plugin"\n', encoding="utf-8"
    )
    (directory / "ui.html").write_text(html, encoding="utf-8")
    return directory


def test_lint_plugin_reads_the_id_from_the_manifest_and_names_the_file(tmp_path: Path) -> None:
    directory = _plugin(tmp_path, "demo", _style(".demo-a { color: #fff; } .x { margin: 0; }"))
    findings = lint_plugin(directory)
    assert [(f.rule, f.path) for f in findings] == [
        ("hardcoded-color", directory / "ui.html"),
        ("unscoped-selector", directory / "ui.html"),
    ]


def test_main_exits_0_when_clean_1_on_findings_and_2_without_a_plugin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = _plugin(tmp_path, "clean", _style(".clean-a { color: var(--text); }"))
    dirty = _plugin(tmp_path, "dirty", '<div style="color:#fff"></div>')
    assert main([str(clean)]) == 0
    assert main([str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "tlc_plugin_dirty/ui.html:1:13: surface-inline-style" in out and "tlc_plugin_clean" not in out
    assert main([str(dirty / "ui.html")]) == 1
    assert main([str(dirty / "ui.html"), "--plugin-id", "dirty"]) == 1
    (tmp_path / "loose.html").write_text("<p></p>", encoding="utf-8")
    assert main([str(tmp_path / "loose.html")]) == 2
    assert main([str(tmp_path / "loose.html"), "--plugin-id", "x"]) == 0
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main([str(empty)]) == 2


def test_finding_is_a_value() -> None:
    assert Finding("important", 1, 2, "!important", "m") == Finding("important", 1, 2, "!important", "m")
