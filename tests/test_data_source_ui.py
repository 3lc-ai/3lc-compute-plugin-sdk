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
        + "function retarget(t) { window.TARGET = t; api.listeners.forEach(function (fn) { fn(); }); }\n"
        + "function els() { return { panel: _els['f-ds-panel'].innerHTML, reach: _els['f-ds-reach'].textContent,"
        + " reachShown: _els['f-ds-reach'].style.display !== 'none', upload: _els['f-ds-upload-btn'].style.display,"
        + " fetched: api.fetched }; }\n"
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
    assert "S3 · proj (project root) — readable from godfire" in panel
    assert "S3 · locked — godfire cannot read this" in panel
    assert 'data-ds-select="/home/g/a.yaml"' in panel and 'data-ds-nav="/home/g/data"' in panel
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
        assert "Select This Folder" in out["panel"], mode


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
