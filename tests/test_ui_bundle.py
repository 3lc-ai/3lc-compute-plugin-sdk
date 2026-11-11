# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0

import pytest

from tlc_plugin_sdk.ui_bundle import build_fragment, form_values, render_fragment


def test_sources_generate_the_same_fragment_and_detect_drift(tmp_path):
    (tmp_path / "index.html").write_text("<style>{{asset:style.css}}</style><script>{{asset:app.js}}</script>")
    (tmp_path / "style.css").write_text("button { color: red; }")
    (tmp_path / "app.js").write_text('let label = "{{not_a_build_binding}}";')
    target = tmp_path / "built.html"
    build_fragment(tmp_path, target)
    build_fragment(tmp_path, target, check=True)
    assert "{{not_a_build_binding}}" in target.read_text()
    (tmp_path / "app.js").write_text("changed();")
    with pytest.raises(ValueError, match="Regenerate"):
        build_fragment(tmp_path, target, check=True)


@pytest.mark.parametrize("asset", ["../secret", "escape"])
def test_assets_cannot_escape_the_source_directory(tmp_path, asset):
    root = tmp_path / "ui"
    root.mkdir()
    (tmp_path / "secret").write_text("private")
    (root / "escape").symlink_to(tmp_path / "secret")
    (root / "index.html").write_text("{{asset:" + asset + "}}")
    with pytest.raises(ValueError, match="inside"):
        render_fragment(root)


def test_form_bindings_escape_values_and_require_resolution(tmp_path):
    fields = [
        {
            "id": "model",
            "default": "<unsafe>",
            "options": [{"value": "<unsafe>", "label": "<Model>", "title": 'A "quoted" hint <here>'}],
        }
    ]
    (tmp_path / "index.html").write_text("<select>{{field:model.options}}</select>")
    with pytest.raises(ValueError, match="Unresolved"):
        render_fragment(tmp_path)
    html = render_fragment(tmp_path, values=form_values(fields))
    assert "&lt;Model&gt;" in html and " selected>" in html
    assert "<unsafe>" not in html
    assert 'title="A &quot;quoted&quot; hint &lt;here&gt;"' in html
