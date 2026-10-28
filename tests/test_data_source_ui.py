# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The shared data-source widget: its locations follow the run target, and its bucket calls name a Connection.

A host with a Config Service refuses a storage call that names no Connection, so the widget must keep
the ``connection_id`` the ``/api/infra/storage`` listing tags each bucket with, and forward it to the
provider's ``/list`` route.

The run target (``PLUGIN_API.getRunTarget``) decides what the picker offers: a node run browses the
node's disk through the host and says which buckets the node can read; a run on the compute host offers
its disk as "This computer" only when it is this browser's machine. A Hub without ``getRunTarget`` gets
the widget as it was before run targets.

The behaviour tests run the shipped script in node against ``fixtures/fake_dom.js``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.shared.data_source_ui import DATA_SOURCE_UI_JS, data_source_ui_script

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_FAKE_DOM = (Path(__file__).parent / "fixtures" / "fake_dom.js").read_text(encoding="utf-8")

_STORAGE = {
    "providers": [{"plugin_id": "aws", "browse": True, "label": "S3"}],
    "storage": [
        {"kind": "bucket", "plugin_id": "aws", "url": "s3://proj", "name": "proj", "project_root": True},
        {"kind": "bucket", "plugin_id": "aws", "url": "s3://locked", "name": "locked", "connection_id": "c1"},
        {"kind": "bucket", "plugin_id": "aws", "url": "s3://other", "name": "other"},
    ],
}
_STATUS = {
    "nodes": [
        {
            "id": "n1",
            "label": "godfire",
            "health": {
                "files_root": "/home/g",
                "storage": {
                    "s3://proj/projects": {"state": "ok", "detail": "The node's own identity can list it."},
                    "s3://locked": {"state": "denied", "detail": "The node's own identity was denied listing it."},
                },
            },
        }
    ]
}
_NODE_FILES = {
    "path": "/home/g",
    "root": "/home/g",
    "dirs": [{"name": "data", "path": "/home/g/data"}],
    "files": [{"name": "a.yaml", "path": "/home/g/a.yaml", "size": 10}],
}
_LOCAL_BROWSE = {"path": "/Users/me", "root": "/", "parent": "/Users", "entries": [{"name": "data", "type": "dir"}]}
_LOCAL_BROWSE_MIXED = {
    "path": "/Users/me",
    "root": "/",
    "parent": "/Users",
    "entries": [
        {"name": "data", "type": "dir"},
        {"name": "locked", "type": "dir", "writable": False},
        {"name": "a.yaml", "type": "file", "size": 10},
    ],
}
# What /browse?purpose=output answers: the folder's own writability and each folder's.
_LOCAL_BROWSE_OUT = {
    "path": "/Users/me",
    "root": "/",
    "parent": "/Users",
    "writable": True,
    "entries": [
        {"name": "data", "type": "dir", "writable": True},
        {"name": "locked", "type": "dir", "writable": False},
        {"name": "report.csv", "type": "file", "size": 10},
    ],
}
_LOCAL_BROWSE_DATA = {
    "path": "/Users/me/data",
    "root": "/",
    "parent": "/Users/me",
    "entries": [{"name": "x.yaml", "type": "file", "size": 1}],
}
_NO_BUCKETS: dict[str, Any] = {"/api/infra/storage": {"storage": [], "providers": []}}


def _run(tmp_path: Path, body: str, *, routes: dict[str, Any] | None = None, target: Any = "absent") -> Any:
    """Run ``body`` (an async JS function body) with the widget bound as ``f`` and return what it returns.

    ``target`` is what ``getRunTarget()`` answers (a mutable ``window.TARGET``); ``"absent"`` is a Hub
    without the member.
    """
    routes = routes or {}
    run_target = (
        ""
        if target == "absent"
        else (
            "window.TARGET = "
            + json.dumps(target)
            + ";\n"
            + "api.getRunTarget = function () { return window.TARGET; };\n"
            + "api.onRunTargetChange = function (fn) { api.listeners.push(fn); };\n"
        )
    )
    harness = tmp_path / "harness.js"
    harness.write_text(
        _FAKE_DOM
        + "\n"
        + data_source_ui_script()
        + "\nvar api = fakeApi("
        + json.dumps(routes)
        + ");\n"
        + run_target
        + "window.PLUGIN_API = api;\n"
        + "['f', 'f-ds-browse-btn', 'f-ds-panel', 'f-ds-upload-btn', 'f-ds-upload-zone', 'f-ds-reach',"
        + " 'f-ds-file-input', 'f-ds-upload-info'].forEach(addEl);\n"
        # The field sits in its wrapper, which is what the outside-click check measures a click against.
        + "var wrapper = new FakeEl('wrapper'); wrapper.setAttribute('class', 'tlc-ds-wrapper');"
        + " _els['f'].parentNode = wrapper; _els['f-ds-panel'].parentNode = wrapper;\n"
        + "function retarget(t) { window.TARGET = t; api.listeners.forEach(function (fn) { fn(); }); }\n"
        + "function els() { return { panel: _els['f-ds-panel'].innerHTML, reach: _els['f-ds-reach'].textContent,"
        + " reachShown: _els['f-ds-reach'].style.display !== 'none', upload: _els['f-ds-upload-btn'].style.display,"
        + " fetched: api.fetched.slice(), open: _els['f-ds-panel'].classList.contains('open'),"
        + " value: _els['f'].value }; }\n"
        # A row of the listing, built by hand (innerHTML is never parsed): `rowWith({'data-ds-nav': '/x'})`, or
        # the name inside one, `rowWith({}, row)`. `clickOn` sends the panel a click that landed on it and returns
        # the event: `stopped` says the widget ended it there (the fake document has no listeners of its own).
        + "function rowWith(attrs, parent) { var el = new FakeEl(''); Object.keys(attrs).forEach(function (k) {"
        + " el.setAttribute(k, attrs[k]); }); el.parentNode = parent || null; return el; }\n"
        + "function clickOn(el) { var ev = new Event('click'); ev.target = el;"
        + " _els['f-ds-panel'].dispatchEvent(ev); return ev; }\n"
        + "function dblOn(el) { var ev = new Event('dblclick'); ev.target = el;"
        + " _els['f-ds-panel'].dispatchEvent(ev); return ev; }\n"
        + "function keyOn(key) { var ev = new Event('keydown'); ev.key = key; ev.target = _els['f-ds-panel'];"
        + " _els['f-ds-panel'].dispatchEvent(ev); return ev; }\n"
        # Typing in the footer's name box: an input event whose target is the box.
        + "function typeName(v) { var ev = new Event('input'); ev.target = rowWith({'data-ds-name': ''});"
        + " ev.target.value = v; _els['f-ds-panel'].dispatchEvent(ev); return ev; }\n"
        + "var primary = function () { return rowWith({'data-ds-primary': ''}); };\n"
        + "(async function () {\n"
        + body
        + "\n})().then(function (out) { process.stdout.write(JSON.stringify(out)); });\n"
    )
    done = subprocess.run(["node", str(harness)], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


def _pure(tmp_path: Path, expr: str) -> Any:
    """Evaluate one JS expression against the shipped script (no DOM needed) and return its JSON value."""
    harness = tmp_path / "pure.js"
    harness.write_text(
        _FAKE_DOM + "\n" + data_source_ui_script() + "\nprocess.stdout.write(JSON.stringify(" + expr + "));\n"
    )
    done = subprocess.run(["node", str(harness)], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


def test_locations_keep_the_connection_tag() -> None:
    assert "connection_id: s.connection_id || ''" in DATA_SOURCE_UI_JS
    assert "connection_name: s.connection_name || ''" in DATA_SOURCE_UI_JS


def test_bucket_browse_names_its_connection() -> None:
    assert "'&connection_id=' + encodeURIComponent(loc.connection_id)" in DATA_SOURCE_UI_JS


@needs_node
def test_script_parses(tmp_path: Path) -> None:
    js = tmp_path / "ds.js"
    js.write_text(data_source_ui_script())
    subprocess.run(["node", "--check", str(js)], check=True, capture_output=True)


@needs_node
def test_a_node_target_lists_the_node_first_then_the_buckets_it_can_or_cannot_read(tmp_path: Path) -> None:
    target = {"target": "node", "node_id": "n1", "ready": True, "label": "godfire", "files_root": "/home/g"}
    buckets = [
        {"kind": "bucket", "url": "s3://proj", "name": "proj"},
        {"kind": "bucket", "url": "s3://locked", "name": "locked"},
        {"kind": "bucket", "url": "s3://other", "name": "other"},
    ]
    node = _STATUS["nodes"][0]["health"]
    locs = _pure(
        tmp_path,
        "_tlcDsLocationList("
        + json.dumps(target)
        + ", "
        + json.dumps(buckets)
        + ", true, "
        + json.dumps({"label": "godfire", **node})
        + ")",
    )
    assert [loc["kind"] for loc in locs] == ["node", "bucket", "bucket", "bucket"]  # never "This computer"
    assert locs[0]["label"] == "godfire (node)" and locs[0]["files_root"] == "/home/g"
    assert [loc["note"] for loc in locs[1:]] == ["readable from godfire", "godfire cannot read this", ""]
    # A label-less Hub still names the node, from the host's own record.
    bare = _pure(tmp_path, "_tlcDsLocationList({target: 'node', node_id: 'n1'}, [], false, {label: 'godfire'})")
    assert bare[0]["label"] == "godfire (node)"


@needs_node
def test_a_node_target_browses_the_node_disk_through_the_host(tmp_path: Path) -> None:
    target = {"target": "node", "node_id": "n1", "ready": True, "label": "godfire", "files_root": "/home/g"}
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://localhost:5020', 'p', {accept: '*.yaml'});\n"
        "_els['f-ds-browse-btn'].click(); await tick(); return els();",
        routes={
            "/api/infra/storage": _STORAGE,
            "/api/infra/status": _STATUS,
            "/api/infra/nodes/n1/files": _NODE_FILES,
        },
        target=target,
    )
    assert "http://localhost:5020/api/infra/nodes/n1/files?path=%2Fhome%2Fg" in out["fetched"]
    assert not any("/browse?" in url for url in out["fetched"])  # the controller's disk is not the node's
    panel = out["panel"]
    assert "godfire (node)" in panel and "This computer" not in panel
    # Several places: the switcher is the path bar's first crumb, with the node's place selected.
    assert '<div class="tlc-ds-place"><label>Location</label><select class="form-select" data-ds-loc>' in panel
    assert '<option value="0" selected>godfire (node)</option>' in panel
    assert '<span data-ds-nav="/home/g">g</span>' in panel  # the path starts from the place's root
    assert 'class="ds-up" title="Up" disabled>' in panel  # at the node's root, nothing is above
    assert "S3 · proj (project root) — readable from godfire" in panel
    assert "S3 · locked — godfire cannot read this" in panel
    assert 'data-ds-row="0"' in panel and "data/" in panel and 'data-ds-row="1"' in panel and "a.yaml" in panel
    assert out["upload"] == "none"  # an upload lands on the compute host, which the node cannot read


@needs_node
def test_a_node_browse_url_and_a_bucket_browse_url(tmp_path: Path) -> None:
    urls = _pure(
        tmp_path,
        "[_tlcDsBrowseUrl({kind: 'node', node_id: 'n 1'}, 'https://c', 'p', '/home/g/x'),"
        " _tlcDsBrowseUrl({kind: 'bucket', plugin_id: 'aws', url: 's3://b', connection_id: 'c1'}, 'https://c', 'p',"
        " 's3://b/x'),"
        " _tlcDsBrowseUrl({kind: 'local'}, 'https://c', 'p', '~', '*.yaml', 'output')]",
    )
    assert urls == [
        "https://c/api/infra/nodes/n%201/files?path=%2Fhome%2Fg%2Fx",
        "https://c/api/infra/storage/aws/list?url=s3%3A%2F%2Fb%2Fx%2F&connection_id=c1",
        "https://c/api/plugins/p/browse?path=~&glob=*.yaml&purpose=output",
    ]


@needs_node
def test_a_remote_compute_host_is_offered_under_the_hubs_name(tmp_path: Path) -> None:
    target = {"target": "local", "ready": True, "label": "Your deployment (small)"}
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'https://compute.example', 'p');\n"
        "_els['f'].value = '/srv/data/x.yaml';\n"
        "_els['f-ds-browse-btn'].click(); await tick(); return els();",
        routes={"/api/infra/storage": _STORAGE, "/browse": _LOCAL_BROWSE},
        target=target,
    )
    assert "https://compute.example/api/plugins/p/browse?path=%2Fsrv%2Fdata&purpose=input" in out["fetched"]
    assert "Your deployment (small)" in out["panel"] and "This computer" not in out["panel"]
    assert out["upload"] == ""


@needs_node
def test_a_loopback_compute_is_this_computer(tmp_path: Path) -> None:
    locs = _pure(tmp_path, "_tlcDsLocationList({target: 'local', label: 'This machine'}, [], true, null)")
    assert locs == [{"kind": "local", "name": "This computer", "label": "This computer"}]


@needs_node
def test_a_hub_without_run_targets_gets_the_old_picker(tmp_path: Path) -> None:
    here = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p');\n"
        "_els['f-ds-browse-btn'].click(); await tick(); return els();",
        routes={"/api/infra/storage": _STORAGE, "/browse": _LOCAL_BROWSE},
    )
    assert "http://127.0.0.1:5020/api/plugins/p/browse?path=~&purpose=input" in here["fetched"]
    assert "This computer" in here["panel"] and "S3 · proj (project root)" in here["panel"]
    assert "readable from" not in here["panel"] and not here["reachShown"] and here["upload"] == ""
    assert not any("/api/infra/status" in url for url in here["fetched"])
    # A compute elsewhere, with no bucket: nothing of the person's to browse, as before.
    away = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'https://compute.example', 'p');\n"
        "_els['f-ds-browse-btn'].click(); await tick(); return els();",
        routes={"/api/infra/storage": {"storage": [], "providers": []}},
    )
    assert "No folders or buckets are offered" in away["panel"]


@needs_node
def test_switching_to_a_node_hides_upload_and_flags_a_folder_on_this_computer(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://localhost:5020', 'p');\n"
        # Picked on this computer while the run went to the compute host (what the picker records).
        "var f = _els['f']; f.value = '/Users/me/data';\n"
        "f.dataset.dsPicked = '/Users/me/data'; f.dataset.dsWhere = 'local';\n"
        "f.dataset.dsWhereLabel = 'this computer';\n"
        "f.dispatchEvent(new Event('change')); await tick();\n"
        "var before = els();\n"
        "retarget({target: 'node', node_id: 'n1', ready: true, label: 'godfire'}); await tick();\n"
        "var after = els();\n"
        "retarget({target: 'local', ready: true, label: 'This machine'}); await tick();\n"
        "return [before, after, els()];",
        routes={"/api/infra/status": _STATUS},
        target={"target": "local", "ready": True, "label": "This machine"},
    )
    before, after, back = out
    assert not before["reachShown"] and before["upload"] == ""
    assert after["reachShown"] and after["upload"] == "none"
    assert after["reach"].startswith("This is on this computer, which godfire cannot read")
    assert "Put the data in a bucket, or choose a folder on godfire." in after["reach"]
    assert not back["reachShown"] and back["upload"] == ""


@needs_node
def test_a_typed_path_is_checked_on_the_node(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://localhost:5020', 'p');\n"
        "var f = _els['f']; f.value = '/data/missing'; f.dispatchEvent(new Event('change')); await tick();\n"
        "return els();",
        routes={"/api/infra/status": _STATUS, "/path-check": {"checked": True, "exists": False, "ok": False}},
        target={"target": "node", "node_id": "n1", "ready": True, "label": "godfire"},
    )
    assert "http://localhost:5020/api/infra/nodes/n1/path-check?path=%2Fdata%2Fmissing" in out["fetched"]
    assert out["reach"] == (
        "godfire has no /data/missing. A run there is refused; choose a folder on godfire, or a bucket."
    )


@needs_node
def test_reach_notes(tmp_path: Path) -> None:
    node = {"target": "node", "node_id": "n1"}
    notes = _pure(
        tmp_path,
        "["
        # A bucket the node's probe says it cannot read.
        "_tlcDsReachNote('s3://locked/x', {target: " + json.dumps(node) + ", nodeLabel: 'godfire',"
        " probe: {state: 'denied', detail: 'denied'}}),"
        # Readable, or not probed: nothing to say.
        "_tlcDsReachNote('s3://proj/x', {target: " + json.dumps(node) + ", probe: {state: 'ok'}}),"
        # A folder on another node.
        "_tlcDsReachNote('/home/g/x', {target: " + json.dumps(node) + ", nodeLabel: 'godfire', where: 'node:n2',"
        " whereLabel: 'storeola'}),"
        # Picked on the node, then the run moved to the compute host.
        "_tlcDsReachNote('/home/g/x', {target: {target: 'local'}, hostLabel: 'this computer', where: 'node:n1',"
        " whereLabel: 'godfire'}),"
        # A Hub without run targets: never a note.
        "_tlcDsReachNote('/Users/me', {target: null, where: 'local'})"
        "]",
    )
    assert notes[0].startswith("godfire cannot read this bucket (denied), so a run there can neither stream nor copy")
    assert notes[1] == ""
    assert notes[2].startswith("This is on storeola, not on godfire.")
    assert notes[3].startswith("This is on godfire, not on this computer. A run on this computer looks for it")
    assert notes[4] == ""


@needs_node
def test_dir_mode_is_a_folder_picker(tmp_path: Path) -> None:
    for mode in ("dir", "folder"):
        out = _run(
            tmp_path,
            "_tlcBindDataSource('f', 'http://localhost:5020', 'p', {mode: '" + mode + "'});\n"
            "_els['f-ds-browse-btn'].click(); await tick(); return els();",
            routes={"/api/infra/storage": {"storage": [], "providers": []}, "/browse": _LOCAL_BROWSE},
        )
        assert "Select this folder" in out["panel"], mode


@needs_node
def test_a_file_picker_picks_a_file_on_a_click_and_opens_a_folder_on_one(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p', {accept: '*.yaml'});\n"
        "var fired = []; ['input', 'change'].forEach(function (t) {"
        " _els['f'].addEventListener(t, function () { fired.push(t); }); });\n"
        "_els['f-ds-browse-btn'].click(); await tick();\n"
        "var listed = els();\n"
        # The click lands on the name inside a.yaml's row, as a real one does.
        "var ev = clickOn(rowWith({}, rowWith({'data-ds-row': '2'})));\n"
        "var picked = els(); picked.fired = fired.slice(); picked.stopped = ev.stopped;\n"
        # Reopened: a click on a folder opens it; the arrow keys highlight, and Enter picks a highlighted file.
        "_els['f-ds-browse-btn'].click(); await tick();\n"
        "clickOn(rowWith({'data-ds-row': '0'})); var during = els().panel; await tick();\n"
        "var opened = els(); opened.during = during;\n"
        "keyOn('ArrowDown'); var dirHl = els(); keyOn('ArrowDown'); keyOn('ArrowDown'); var fileHl = els();\n"
        "keyOn('Enter'); var byKey = els();\n"
        "return [listed, picked, opened, dirHl, fileHl, byKey];",
        routes={**_NO_BUCKETS, "/browse": _LOCAL_BROWSE_MIXED},
    )
    listed, picked, opened, dir_hl, file_hl, by_key = out
    panel = listed["panel"]
    # The place line names the one place; the path bar runs from its root ('/' here) to the folder shown.
    assert '<div class="tlc-ds-place"><label>Location</label><span class="ds-place-one">This computer</span>' in panel
    assert '<span data-ds-nav="/">/</span><span class="sep">/</span><span data-ds-nav="/Users">Users</span>' in panel
    assert '<span class="sep">/</span><span data-ds-nav="/Users/me">me</span>' in panel
    assert 'class="ds-up" title="Up" data-ds-nav="/Users">' in panel
    assert ">..<" not in panel  # the way up is the arrow, not a row
    # Rows, and a footer with the filter only: a file picker has no buttons, the click is the pick.
    assert '<div class="tlc-ds-entry" data-ds-row="2">' in panel and "a.yaml" in panel
    assert '<span class="ds-note">Showing *.yaml</span>' in panel
    assert "data-ds-cancel" not in panel and "data-ds-primary" not in panel
    assert picked["value"] == "/Users/me/a.yaml" and not picked["open"] and picked["stopped"]
    assert picked["fired"] == ["input", "change"]
    # The rows stay while the folder's listing loads: no flash through "Loading…" on a quick listing.
    assert 'data-ds-row="0"' in opened["during"] and "Loading" not in opened["during"]
    assert opened["open"] and any("/browse?path=%2FUsers%2Fme%2Fdata" in url for url in opened["fetched"])
    assert '<div class="tlc-ds-entry is-selected" data-ds-row="0">' in dir_hl["panel"]
    assert '<div class="tlc-ds-entry is-selected" data-ds-row="2">' in file_hl["panel"]
    assert by_key["value"] == "/Users/me/a.yaml" and not by_key["open"]


@needs_node
def test_a_folder_picker_opens_a_folder_on_a_click_and_selects_the_one_shown(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p', {mode: 'folder'});\n"
        "_els['f-ds-browse-btn'].click(); await tick(); var listed = els();\n"
        "clickOn(primary()); var here = els();\n"
        "_els['f-ds-browse-btn'].click(); await tick();\n"
        "var navEv = clickOn(rowWith({'data-ds-row': '0'})); var during = els().panel; await tick();\n"
        "var opened = els(); opened.during = during; opened.stopped = navEv.stopped;\n"
        "opened.idle = clickOn(rowWith({})).stopped;\n"
        # The second click of a double-click does nothing more.
        "var second = new Event('click'); second.target = rowWith({'data-ds-row': '1'}); second.detail = 2;\n"
        "_els['f-ds-panel'].dispatchEvent(second); await tick(); var twice = els();\n"
        "keyOn('ArrowDown'); var hl = els(); keyOn('Enter'); await tick(); var byKey = els();\n"
        "return [listed, here, opened, twice, hl, byKey];",
        routes={**_NO_BUCKETS, "/browse": _LOCAL_BROWSE_MIXED},
    )
    listed, here, opened, twice, hl, by_key = out
    panel = listed["panel"]
    assert "data-ds-primary>Select this folder</button>" in panel
    assert '<div class="tlc-ds-entry" data-ds-row="0">' in panel  # data/
    # A file is no choice here: dimmed, and not a row one can highlight.
    assert '<div class="tlc-ds-entry tlc-ds-entry-disabled" title="Choose a folder">' in panel
    assert 'data-ds-row="2"' not in panel
    assert here["value"] == "/Users/me" and not here["open"]
    # A click opens the folder. The rows stay while its listing loads: no flash through "Loading…"; and
    # opening replaces the panel's contents, so the click ends in the panel, or the document's outside-click
    # check would take the detached target for outside and close the panel.
    assert 'data-ds-row="0"' in opened["during"] and "Loading" not in opened["during"]
    assert opened["open"] and any("/browse?path=%2FUsers%2Fme%2Fdata" in url for url in opened["fetched"])
    assert opened["stopped"] and opened["idle"] is False
    assert not any("%2Flocked" in url for url in twice["fetched"])
    assert '<div class="tlc-ds-entry is-selected" data-ds-row="0">' in hl["panel"]
    assert "data-ds-primary>Select this folder</button>" in hl["panel"]  # the highlight is for Enter
    assert by_key["open"] and len([u for u in by_key["fetched"] if "%2Fme%2Fdata" in u]) == 2


@needs_node
def test_the_keyboard_moves_the_highlight_picks_goes_up_and_closes(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p');\n"
        "_els['f-ds-browse-btn'].click(); await tick();\n"
        "keyOn('ArrowDown'); var first = els();\n"
        "keyOn('ArrowDown'); keyOn('ArrowDown'); keyOn('ArrowDown'); var last = els();\n"
        "keyOn('Enter'); var picked = els();\n"
        "_els['f-ds-browse-btn'].click(); await tick(); keyOn('Backspace'); await tick(); var up = els();\n"
        "keyOn('Escape'); var closed = els();\n"
        "return [first, last, picked, up, closed];",
        routes={**_NO_BUCKETS, "/browse": _LOCAL_BROWSE_MIXED},
    )
    first, last, picked, up, closed = out
    assert 'is-selected" data-ds-row="0">' in first["panel"]
    assert 'is-selected" data-ds-row="2">' in last["panel"]  # stops at the last row
    assert picked["value"] == "/Users/me/a.yaml" and not picked["open"]
    assert up["open"] and any("/browse?path=%2FUsers&" in url for url in up["fetched"])
    assert not closed["open"]


@needs_node
def test_an_output_file_picker_saves_under_a_name(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "window.SUG = 'TestCoco-train.csv';\n"
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p', {mode: 'file', purpose: 'output', accept: '*.csv',"
        " suggestedName: function () { return window.SUG; }});\n"
        "_els['f-ds-browse-btn'].click(); await tick(); var listed = els();\n"
        "clickOn(primary()); var saved = els();\n"
        "_els['f-ds-browse-btn'].click(); await tick(); typeName('summary'); clickOn(primary()); var typed = els();\n"
        "_els['f-ds-browse-btn'].click(); await tick(); var kept = els();\n"
        "clickOn(rowWith({'data-ds-row': '2'})); var replacing = els();\n"
        "typeName(''); clickOn(primary()); var still = els();\n"
        "return [listed, saved, typed, kept, replacing, still];",
        routes={**_NO_BUCKETS, "/browse": _LOCAL_BROWSE_OUT},
    )
    listed, saved, typed, kept, replacing, still = out
    assert any("purpose=output" in url for url in listed["fetched"])
    assert '<label class="ds-name-label">File name</label>' in listed["panel"]
    assert 'data-ds-name autocomplete="off" spellcheck="false" value="TestCoco-train.csv">' in listed["panel"]
    assert "data-ds-primary>Save</button>" in listed["panel"]
    assert saved["value"] == "/Users/me/TestCoco-train.csv" and not saved["open"]
    assert typed["value"] == "/Users/me/summary.csv"  # the extension accept names, when the name has none
    assert 'value="summary"' in kept["panel"]  # a typed name survives a reopen; a suggested one would follow it
    assert 'value="report.csv"' in replacing["panel"]
    assert "data-ds-note>Replaces report.csv</span>" in replacing["panel"]
    assert still["open"] and still["value"] == "/Users/me/summary.csv"  # an empty name saves nothing


@needs_node
def test_an_output_folder_picker_names_a_new_folder_or_adds_to_one(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "window.SUG = 'TestCoco-yolo';\n"
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p', {mode: 'folder', purpose: 'output',"
        " suggestedName: function () { return window.SUG; }});\n"
        "_els['f-ds-browse-btn'].click(); await tick(); var listed = els();\n"
        "clickOn(primary()); var fresh = els();\n"
        # A suggestion that names a folder already there: the pick adds to it.
        "window.SUG = 'data'; _els['f-ds-browse-btn'].click(); await tick(); var into = els();\n"
        "clickOn(primary()); var added = els();\n"
        # A click on a folder opens it; the arrow keys highlight one, which names it in the box.
        "_els['f-ds-browse-btn'].click(); await tick(); clickOn(rowWith({'data-ds-row': '0'})); await tick();\n"
        "var opened = els();\n"
        "keyOn('ArrowDown'); keyOn('ArrowDown'); var locked = els();\n"
        "clickOn(primary()); var refused = els();\n"
        "typeName(''); clickOn(primary()); var here = els();\n"
        "return [listed, fresh, into, added, opened, locked, refused, here];",
        routes={**_NO_BUCKETS, "/browse": _LOCAL_BROWSE_OUT},
    )
    listed, fresh, into, added, opened, locked, refused, here = out
    panel = listed["panel"]
    assert '<label class="ds-name-label">New folder</label>' in panel and 'value="TestCoco-yolo"' in panel
    assert "data-ds-note>A new folder in me</span>" in panel and "data-ds-primary>Select</button>" in panel
    assert 'data-ds-row="2"' not in panel  # report.csv is no choice in a folder picker
    assert fresh["value"] == "/Users/me/TestCoco-yolo" and not fresh["open"]
    assert 'value="data"' in into["panel"] and "data-ds-note>Adds to data</span>" in into["panel"]
    assert added["value"] == "/Users/me/data"
    assert opened["open"] and any("/browse?path=%2FUsers%2Fme%2Fdata" in url for url in opened["fetched"])
    assert 'is-selected" data-ds-row="1"' in locked["panel"] and 'value="locked"' in locked["panel"]
    assert 'data-ds-primary disabled title="Not writable' in locked["panel"]
    assert refused["open"] and refused["value"] == "/Users/me/data"
    assert here["value"] == "/Users/me" and not here["open"]  # an empty box: the folder shown


@needs_node
def test_a_click_in_the_hubs_dropdown_list_does_not_close_the_panel(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p');\n"
        "_els['f-ds-browse-btn'].click(); await tick();\n"
        # The Hub's picker shows a select's options in a div.tlc-picker on the body: outside the field, yet ours.
        "var inList = new Event('click');"
        " inList.target = rowWith({'class': 'tlc-picker-row'}, rowWith({'class': 'tlc-picker'}));\n"
        "document.dispatchEvent(inList); var afterList = els();\n"
        # A pick removes that list before the click reaches the document: a detached target is not outside.
        "var gone = new Event('click'); gone.target = rowWith({}); gone.target.isConnected = false;\n"
        "document.dispatchEvent(gone); var afterGone = els();\n"
        "var outside = new Event('click'); outside.target = rowWith({});\n"
        "document.dispatchEvent(outside); var afterOutside = els();\n"
        "return [afterList, afterGone, afterOutside];",
        routes={**_NO_BUCKETS, "/browse": _LOCAL_BROWSE_MIXED},
    )
    after_list, after_gone, after_outside = out
    assert after_list["open"] and after_gone["open"] and not after_outside["open"]


@needs_node
def test_the_panel_reopens_where_the_browsing_was(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p');\n"
        "_els['f-ds-browse-btn'].click(); await tick(); clickOn(rowWith({'data-ds-row': '0'})); await tick();\n"
        "clickOn(rowWith({'data-ds-cancel': ''})); var closed = els();\n"
        "_els['f-ds-browse-btn'].click(); await tick(); var again = els();\n"
        "return [closed, again];",
        # The first matching route wins: the folder's own listing before the general one.
        routes={**_NO_BUCKETS, "/browse?path=%2FUsers%2Fme%2Fdata": _LOCAL_BROWSE_DATA, "/browse": _LOCAL_BROWSE_MIXED},
    )
    closed, again = out
    assert not closed["open"]
    assert again["open"] and "/browse?path=%2FUsers%2Fme%2Fdata" in again["fetched"][-1]
    assert '<span data-ds-nav="/Users/me/data">data</span>' in again["panel"] and "x.yaml" in again["panel"]


@needs_node
def test_mode_and_accept_are_read_on_every_open(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "window.ACCEPT = '*.csv'; window.MODE = 'file';\n"
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p', {mode: function () { return window.MODE; },"
        " accept: function () { return window.ACCEPT; }});\n"
        "_els['f-ds-browse-btn'].click(); await tick(); var csv = els();\n"
        "_els['f-ds-browse-btn'].click(); window.ACCEPT = '*.xlsx'; window.MODE = 'folder';\n"
        "_els['f-ds-browse-btn'].click(); await tick(); var xlsx = els();\n"
        "return [csv, xlsx];",
        routes={**_NO_BUCKETS, "/browse": _LOCAL_BROWSE_MIXED},
    )
    csv, xlsx = out
    assert any("glob=*.csv" in url for url in csv["fetched"]) and "Select this folder" not in csv["panel"]
    assert any("glob=*.xlsx" in url for url in xlsx["fetched"]) and "Select this folder" in xlsx["panel"]


@needs_node
def test_a_double_click_in_the_empty_field_opens_the_panel(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p');\n"
        "var f = _els['f'];\n"
        "f.dispatchEvent(new Event('dblclick')); await tick(); var opened = els();\n"
        "f.dispatchEvent(new Event('dblclick')); await tick(); var again = els();\n"
        "_els['f-ds-browse-btn'].click(); var closed = els();\n"
        "f.value = '/Users/me/a.yaml'; f.dispatchEvent(new Event('dblclick')); await tick(); var filled = els();\n"
        "f.value = ''; f.disabled = true; f.dispatchEvent(new Event('dblclick')); await tick();\n"
        "return [opened, again, closed, filled, els()];",
        routes={**_NO_BUCKETS, "/browse": _LOCAL_BROWSE_MIXED},
    )
    opened, again, closed, filled, disabled = out
    assert opened["open"] and len(opened["fetched"]) == 2  # the storage list, then the folder
    assert again["open"] and len(again["fetched"]) == 2  # a second double-click neither closes nor lists again
    assert not closed["open"]
    assert not filled["open"] and len(filled["fetched"]) == 2  # a filled field keeps the word selection
    assert not disabled["open"]


@needs_node
def test_bucket_browse_on_a_node_target_names_its_connection(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://localhost:5020', 'p');\n"
        "_els['f'].value = 's3://locked/images';\n"
        "_els['f-ds-browse-btn'].click(); await tick(); return els();",
        routes={
            "/api/infra/storage/aws/list": {"prefixes": [], "objects": []},
            "/api/infra/storage": _STORAGE,
            "/api/infra/status": _STATUS,
        },
        target={"target": "node", "node_id": "n1", "ready": True, "label": "godfire"},
    )
    assert (
        "http://localhost:5020/api/infra/storage/aws/list?url=s3%3A%2F%2Flocked%2F&connection_id=c1" in out["fetched"]
    )
    assert "godfire cannot read this: The node's own identity was denied listing it." in out["panel"]


@needs_node
def test_node_roots_are_distinct_locations_and_explicit_empty_is_not_legacy(tmp_path: Path) -> None:
    target = {"target": "node", "node_id": "n", "label": "godfire", "browse_roots": ["/home/g/Data", "/mnt/data"]}
    locations = _pure(tmp_path, "_tlcDsLocationList(" + json.dumps(target) + ", [], false, null)")
    assert [loc["label"] for loc in locations] == ["godfire · /home/g/Data", "godfire · /mnt/data"]
    assert [loc["files_root"] for loc in locations] == target["browse_roots"]
    target["browse_roots"] = []
    assert _pure(tmp_path, "_tlcDsLocationList(" + json.dumps(target) + ", [], false, null)") == []


@needs_node
@pytest.mark.parametrize("here", [True, False])
def test_explicit_host_roots_override_location_inference(tmp_path: Path, here: bool) -> None:
    target = {"target": "local", "label": "shared-host", "browse_roots": ["/data/a", "/data/b"]}
    locs = _pure(tmp_path, "_tlcDsLocationList(" + json.dumps(target) + ", [], " + json.dumps(here) + ", null)")
    assert [loc["label"] for loc in locs] == ["shared-host · /data/a", "shared-host · /data/b"]
    target["browse_roots"] = []
    assert _pure(tmp_path, "_tlcDsLocationList(" + json.dumps(target) + ", [], " + json.dumps(here) + ", null)") == []


@needs_node
def test_host_selected_root_reopens_without_escaping_to_parent(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p');\n"
        "_els['f'].value = '/data/b'; _els['f-ds-browse-btn'].click(); await tick(); return els();",
        routes={"/api/infra/storage": _STORAGE, "/browse": _LOCAL_BROWSE},
        target={"target": "local", "label": "host", "browse_roots": ["/data/a", "/data/b"]},
    )
    assert any("/browse?path=%2Fdata%2Fb" in url for url in result["fetched"])


@needs_node
def test_single_explicit_host_root_keeps_machine_and_folder_label(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "_tlcBindDataSource('f', 'http://127.0.0.1:5020', 'p');\n"
        "_els['f-ds-browse-btn'].click(); await tick(); return els();",
        routes={"/api/infra/storage": {"storage": [], "providers": []}, "/browse": _LOCAL_BROWSE},
        target={"target": "local", "label": "host", "browse_roots": ["/data"]},
    )
    assert "host · /data" in out["panel"]


_NO_STORAGE: dict[str, Any] = {"/api/infra/storage": {"storage": [], "providers": []}}
_OPEN = (
    "_tlcBindDataSource('f', 'https://compute.example', 'p');\n"
    "_els['f-ds-browse-btn'].click(); await tick(); return els();"
)


@needs_node
def test_a_host_that_offers_no_folders_names_the_setting_that_offers_them(tmp_path: Path) -> None:
    target = {"target": "local", "label": "shared-host", "browse_roots": []}
    out = _run(tmp_path, _OPEN, routes=_NO_STORAGE, target=target)
    assert "No folders on shared-host are offered for browsing." in out["panel"]
    assert "<code>TLC_DATA_SOURCE_ROOTS</code> on the compute host" in out["panel"]
    assert "No folders or buckets are offered for this target" not in out["panel"]


@needs_node
@pytest.mark.parametrize(
    "target",
    [
        {"target": "local"},  # a host that predates browse_roots: it never said what it offers
        {"target": "node", "node_id": "n9", "label": "gpu", "browse_roots": []},  # a node's roots are its own
    ],
)
def test_an_older_host_or_a_node_keeps_the_general_empty_state(tmp_path: Path, target: dict[str, Any]) -> None:
    out = _run(tmp_path, _OPEN, routes={**_NO_STORAGE, "/api/infra/status": {"nodes": []}}, target=target)
    assert "No folders or buckets are offered for this target" in out["panel"]
    assert "TLC_DATA_SOURCE_ROOTS" not in out["panel"]
