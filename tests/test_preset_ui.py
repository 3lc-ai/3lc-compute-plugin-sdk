# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""The shared preset bar: pick a saved setup, change it, save it under a name.

The behaviour tests run the shipped script in node against ``fixtures/fake_dom.js``, with an in-memory
preset store behind ``PLUGIN_API.authFetch`` and a plain object as the form.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tlc_plugin_sdk.shared.preset_ui import PRESET_UI_JS, preset_ui_script

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

_FAKE_DOM = (Path(__file__).parent / "fixtures" / "fake_dom.js").read_text(encoding="utf-8")

P1 = {
    "id": "p1",
    "name": "Small objects 1280",
    "created": "2026-10-25T10:00:00Z",
    "model": "yolo11n",
    "epochs": 100,
    "train_table_url": "t1",
    "val_table_url": "v1",
}
P2 = {
    "id": "p2",
    "name": "Baseline",
    "created": "2026-10-17T10:00:00Z",
    "model": "yolo11s",
    "epochs": 10,
    "train_table_url": "t2",
    "val_table_url": "",
}
DEFAULTS = {"model": "yolo11n", "epochs": 10, "train_table_url": "", "val_table_url": ""}

_HARNESS = r"""
var form = {};
var DEFAULTS = %(defaults)s;
function collect() { return JSON.parse(JSON.stringify(form)); }
var applied = [];
function applyFn(values) {
  form = JSON.parse(JSON.stringify(values));
  applied.push(form);
  if (window.ASYNC_APPLY) return new Promise(function (r) { setTimeout(r, 5); });
}
var store = %(store)s;
var calls = [];
var api = { toasts: [] };
function ok(body) {
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(body); } });
}
api.authFetch = function (url, o) {
  o = o || {};
  var method = o.method || 'GET';
  var path = url.replace('http://c', '');
  calls.push(method + ' ' + path);
  if (window.LIST_ERROR && method === 'GET') return Promise.reject(new Error('HTTP 500'));
  if (method === 'GET' && path === '/api/plugins/x/presets') return ok(store.slice());
  if (method === 'POST' && path === '/api/plugins/x/presets') {
    if (window.SAVE_ERROR) return ok({ error: window.SAVE_ERROR });
    var body = JSON.parse(o.body);
    var rec;
    if (body.id) {
      rec = store.filter(function (p) { return p.id === body.id; })[0];
      Object.keys(body).forEach(function (k) { rec[k] = body[k]; });
      if (!rec.created) rec.created = 'now';
    } else {
      rec = body;
      rec.id = 'p' + (store.length + 1);
      rec.created = '2026-10-27T12:00:00Z';
      store.unshift(rec);
    }
    return ok({ id: rec.id, created: rec.created });
  }
  var del = path.match(/^\/api\/plugins\/x\/presets\/([^/]+)\/delete$/);
  if (method === 'POST' && del) {
    var id = decodeURIComponent(del[1]);
    if (!store.some(function (p) { return p.id === id; })) return ok({ error: 'Not found' });
    store = store.filter(function (p) { return p.id !== id; });
    return ok({ deleted: true });
  }
  return Promise.reject(new Error('HTTP 404 ' + method + ' ' + path));
};
api.getConfig = function (k) { return k === 'compute_service_url' ? 'http://c' : ''; };
api.showToast = function (m, t) { api.toasts.push(t + ': ' + m); };
window.PLUGIN_API = api;
var LS = %(ls)s;
window.localStorage = {
  getItem: function (k) { return k in LS ? LS[k] : null; },
  setItem: function (k, v) { LS[k] = String(v); },
  removeItem: function (k) { delete LS[k]; },
};
['m', 'm-bar', 'm-select', 'm-name', 'm-mode-pick', 'm-saveas', 'm-delete', 'm-note', 'm-mode-save', 'm-save-btn',
 'm-cancel-btn', 'm-save-note', 'root'].forEach(addEl);
function options() {
  var out = [];
  _els['m-select'].options.forEach(function (o) {
    if (o.options && o.options.length) o.options.forEach(function (c) { out.push([c.value, c.textContent]); });
    else out.push([o.value, o.textContent]);
  });
  return out;
}
// The data-* attributes of each option, by value.
function optionAttrs() {
  var out = {};
  _els['m-select'].options.forEach(function (o) {
    (o.options && o.options.length ? o.options : [o]).forEach(function (c) { out[c.value] = c._attrs; });
  });
  return out;
}
function make(extra) {
  var opts = {
    pluginId: 'x', apiPath: '/api/plugins/x/presets', mount: 'm', formRoot: 'root',
    collect: collect, apply: applyFn, defaults: function () { return JSON.parse(JSON.stringify(DEFAULTS)); },
    inputs: ['train_table_url', 'val_table_url'],
    autoName: function (v) { return v.model + ' today'; },
    summary: function (v) { return v.model + ' · ' + v.epochs; },
    draftDelay: 0,
  };
  Object.keys(extra || {}).forEach(function (k) { opts[k] = extra[k]; });
  return _tlcPluginPresets(opts);
}
function state(p) {
  return {
    options: options(), value: _els['m-select'].value, origin: p.origin(), modified: p.modified(),
    presetId: p.presetId(), presetName: p.presetName(), form: collect(),
    deleteShown: _els['m-delete'].style.display !== 'none', barShown: _els['m-mode-pick'].style.display !== 'none',
    saveShown: _els['m-mode-save'].style.display === '',
    selectShown: _els['m-select'].style.display !== 'none', nameShown: _els['m-name'].style.display === '',
    name: _els['m-name'].value, saveBtn: _els['m-save-btn'].textContent, saveNote: _els['m-save-note'].textContent,
    saveNoteError: _els['m-save-note'].classList.contains('is-error'),
    note: _els['m-note'].textContent, attrs: optionAttrs(),
    toasts: api.toasts.slice(), calls: calls.slice(), store: JSON.parse(JSON.stringify(store)),
    ls: JSON.parse(JSON.stringify(LS)), applied: applied.slice(),
  };
}
function key(k) {
  var ev = { type: 'keydown', key: k, prevented: false, stopped: false };
  ev.preventDefault = function () { this.prevented = true; };
  ev.stopPropagation = function () { this.stopped = true; };
  _els['m-name'].dispatchEvent(ev);
  return { prevented: ev.prevented, stopped: ev.stopped };
}
function edit(values) {
  Object.keys(values).forEach(function (k) { form[k] = values[k]; });
  _els['root'].dispatchEvent(new Event('input'));
}
async function settle() { for (var i = 0; i < 8; i++) await tick(); }
"""


def _run(
    tmp_path: Path,
    body: str,
    *,
    store: list[dict[str, Any]] | None = None,
    ls: dict[str, str] | None = None,
) -> Any:
    """Run ``body`` (an async JS function body) against the shipped script and return what it returns."""
    harness = tmp_path / "harness.js"
    harness.write_text(
        _FAKE_DOM
        + "\n"
        + preset_ui_script()
        + "\n"
        + _HARNESS
        % {
            "defaults": json.dumps(DEFAULTS),
            "store": json.dumps(store if store is not None else [dict(P1), dict(P2)]),
            "ls": json.dumps(ls or {}),
        }
        + "(async function () {\n"
        + body
        + "\n})().then(function (out) { process.stdout.write(JSON.stringify(out)); });\n",
        encoding="utf-8",
    )
    done = subprocess.run(["node", str(harness)], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


def _pure(tmp_path: Path, expr: str) -> Any:
    harness = tmp_path / "pure.js"
    harness.write_text(_FAKE_DOM + "\n" + PRESET_UI_JS + "\nprocess.stdout.write(JSON.stringify(" + expr + "));\n")
    done = subprocess.run(["node", str(harness)], check=True, capture_output=True, text=True)
    return json.loads(done.stdout)


def _draft(origin: dict[str, Any], origin_values: dict[str, Any], values: dict[str, Any]) -> dict[str, str]:
    return {"tlc-presets:http://c:x": json.dumps({"origin": origin, "originValues": origin_values, "values": values})}


def _values(preset: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in preset.items() if k not in {"id", "name", "created", "last_run"}}


@needs_node
def test_script_parses(tmp_path: Path) -> None:
    js = tmp_path / "preset_ui.js"
    js.write_text(preset_ui_script(), encoding="utf-8")
    subprocess.run(["node", "--check", str(js)], check=True, capture_output=True)


@needs_node
def test_age_labels(tmp_path: Path) -> None:
    now = "Date.parse('2026-10-27T12:00:00Z')"
    assert _pure(tmp_path, f"_tlcPresetAge('2026-10-27T11:59:40Z', {now})") == "just now"
    assert _pure(tmp_path, f"_tlcPresetAge('2026-10-27T11:35:00Z', {now})") == "25 min ago"
    assert _pure(tmp_path, f"_tlcPresetAge('2026-10-27T09:00:00Z', {now})") == "3 h ago"
    assert _pure(tmp_path, f"_tlcPresetAge('2026-10-26T10:00:00Z', {now})") == "yesterday"
    assert _pure(tmp_path, f"_tlcPresetAge('2026-10-25T10:00:00Z', {now})") == "2 days ago"
    assert _pure(tmp_path, f"_tlcPresetAge('2026-10-17T10:00:00Z', {now})") == "last week"
    assert _pure(tmp_path, f"_tlcPresetAge('2026-10-03T10:00:00Z', {now})") == "3 weeks ago"
    assert _pure(tmp_path, f"_tlcPresetAge('2026-08-03T10:00:00Z', {now})") == "Aug 3"
    assert _pure(tmp_path, f"_tlcPresetAge('2025-08-03T10:00:00Z', {now})") == "Aug 3 2025"
    assert _pure(tmp_path, f"_tlcPresetAge('', {now})") == ""
    assert _pure(tmp_path, f"_tlcPresetAge('not a date', {now})") == ""


@needs_node
def test_unique_names(tmp_path: Path) -> None:
    assert _pure(tmp_path, "_tlcPresetUniqueName('Run', ['Other'])") == "Run"
    assert _pure(tmp_path, "_tlcPresetUniqueName('Run', ['run'])") == "Run (2)"
    assert _pure(tmp_path, "_tlcPresetUniqueName('Run', ['Run', 'Run (2)'])") == "Run (3)"
    assert _pure(tmp_path, "_tlcPresetUniqueName('  Run ', [])") == "Run"


@needs_node
def test_opens_bare_on_default_settings(tmp_path: Path) -> None:
    out = _run(tmp_path, "var p = make(); await p.init(); await settle(); return state(p);")
    assert out["form"] == DEFAULTS
    assert out["origin"] == {"kind": "defaults", "id": "", "name": ""}
    assert out["value"] == "" and out["modified"] is False and out["deleteShown"] is False
    assert out["options"][0] == ["", "Default settings"]
    assert out["options"][1][0] == "p1" and out["options"][1][1].startswith("Small objects 1280 — yolo11n · 100 · ")
    assert out["options"][2][0] == "p2" and out["options"][2][1].startswith("Baseline — yolo11s · 10 · ")
    assert out["note"] == ""
    assert out["presetId"] == "" and out["presetName"] == ""


@needs_node
def test_opens_on_the_draft(tmp_path: Path) -> None:
    origin = {"kind": "preset", "id": "p1", "name": "Small objects 1280"}
    draft = _draft(origin, _values(P1), {**_values(P1), "epochs": 120})
    out = _run(tmp_path, "var p = make(); await p.init(); await settle(); return state(p);", ls=draft)
    assert out["form"]["epochs"] == 120 and out["form"]["train_table_url"] == "t1"
    assert out["origin"]["id"] == "p1" and out["value"] == "p1"
    assert out["modified"] is True
    assert out["options"][1][1].endswith(" · modified")
    assert out["deleteShown"] is True
    assert out["presetName"] == ""  # changed from the preset, so a run is not "it"


@needs_node
def test_a_draft_whose_preset_is_gone_compares_with_the_defaults(tmp_path: Path) -> None:
    draft = _draft({"kind": "preset", "id": "gone", "name": "Gone"}, _values(P1), {**_values(P1)})
    out = _run(tmp_path, "var p = make(); await p.init(); await settle(); return state(p);", ls=draft)
    assert out["form"] == _values(P1)
    assert out["origin"]["kind"] == "defaults" and out["value"] == ""
    assert out["modified"] is True
    assert out["options"][0] == ["", "Default settings · modified"]


@needs_node
def test_the_old_config_bar_key_is_honoured_once(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        "var p = make({legacyKey: 'x_selected'}); await p.init(); await settle(); return state(p);",
        ls={"x_selected": "p1"},
    )
    assert out["form"] == _values(P1)
    assert out["origin"]["id"] == "p1" and out["modified"] is False
    assert "x_selected" not in out["ls"]
    assert "tlc-presets:http://c:x" in out["ls"]  # the draft takes over


@needs_node
def test_context_inputs_replace_the_input_set_and_stay(tmp_path: Path) -> None:
    draft = _draft({"kind": "preset", "id": "p1", "name": "Small objects 1280"}, _values(P1), _values(P1))
    out = _run(
        tmp_path,
        """
        var p = make({contextInputs: function () { return {train_table_url: 'ctx-train'}; }});
        await p.init(); await settle();
        var opened = state(p);
        await p.pick('p2'); await settle();
        var picked = state(p);
        edit({epochs: 11});
        await settle();
        var edited = state(p);
        return {opened: opened, picked: picked, edited: edited};
        """,
        ls=draft,
    )
    opened = out["opened"]
    assert opened["form"]["train_table_url"] == "ctx-train"
    assert opened["form"]["val_table_url"] == ""  # not supplied by the context, so cleared
    assert opened["form"]["epochs"] == 100
    assert opened["modified"] is False and opened["origin"]["id"] == "p1"
    assert opened["note"].startswith("Opened from a table")
    picked = out["picked"]
    assert picked["form"]["train_table_url"] == "ctx-train" and picked["form"]["val_table_url"] == ""
    assert picked["form"]["model"] == "yolo11s" and picked["origin"]["id"] == "p2"
    assert picked["modified"] is False and picked["presetName"] == "Baseline"
    assert out["edited"]["modified"] is True


@needs_node
def test_picking_applies_editing_marks_and_picking_again_reverts(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        var p = make(); await p.init(); await settle();
        _els['m-select'].value = 'p1'; _els['m-select'].dispatchEvent(new Event('change')); await settle();
        var picked = state(p);
        edit({epochs: 120}); await settle();
        var edited = state(p);
        await p.pick('p1'); await settle();
        var reverted = state(p);
        return {picked: picked, edited: edited, reverted: reverted};
        """,
    )
    picked = out["picked"]
    assert picked["form"] == _values(P1) and picked["origin"]["id"] == "p1" and picked["value"] == "p1"
    assert picked["deleteShown"] is True and picked["presetId"] == "p1" and picked["presetName"] == "Small objects 1280"
    edited = out["edited"]
    assert edited["modified"] is True and edited["presetId"] == "" and edited["presetName"] == ""
    assert edited["options"][1][1].endswith(" · modified")
    assert edited["ls"]["tlc-presets:http://c:x"]
    assert json.loads(edited["ls"]["tlc-presets:http://c:x"])["values"]["epochs"] == 120
    reverted = out["reverted"]
    assert reverted["form"]["epochs"] == 100 and reverted["modified"] is False
    assert not reverted["options"][1][1].endswith(" · modified")


@needs_node
def test_changed_reports_js_state_the_dom_does_not(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        var p = make({formRoot: ''}); await p.init(); await settle();
        await p.pick('p1'); await settle();
        form.epochs = 3; p.changed(); await settle();
        return state(p);
        """,
    )
    assert out["modified"] is True


@needs_node
def test_save_as_prefills_a_free_name_and_enter_saves(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        var p = make(); await p.init(); await settle();
        edit({epochs: 42}); await settle();
        p.openSave();
        var opened = state(p);
        _els['m-name'].value = 'My run';
        var enter = key('Enter'); await settle();
        return {opened: opened, enter: enter, after: state(p)};
        """,
    )
    opened = out["opened"]
    assert opened["saveShown"] is True and opened["barShown"] is False
    assert opened["nameShown"] is True and opened["selectShown"] is False  # the field takes the select's slot
    assert opened["name"] == "yolo11n today" and opened["saveBtn"] == "Save"
    assert out["enter"] == {"prevented": True, "stopped": True}
    after = out["after"]
    assert after["selectShown"] is True and after["nameShown"] is False
    saved = [p for p in after["store"] if p["name"] == "My run"]
    assert len(saved) == 1 and saved[0]["epochs"] == 42 and saved[0]["id"] == "p3"
    assert after["origin"] == {"kind": "preset", "id": "p3", "name": "My run"}
    assert after["value"] == "p3" and after["modified"] is False and after["presetName"] == "My run"
    assert after["saveShown"] is False and after["barShown"] is True
    assert after["toasts"] == ["success: Saved preset “My run”"]


@needs_node
def test_save_as_from_a_preset_suggests_its_name_with_a_suffix(tmp_path: Path) -> None:
    body = "var p = make(); await p.init(); await p.pick('p1'); await settle(); p.openSave(); return state(p);"
    out = _run(tmp_path, body)
    assert out["name"] == "Small objects 1280 (2)"


@needs_node
def test_an_empty_name_saves_under_the_auto_name(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        var p = make(); await p.init(); await settle();
        p.openSave(); _els['m-name'].value = '   ';
        await p.save(); await settle();
        return state(p);
        """,
    )
    assert out["store"][0]["name"] == "yolo11n today" and out["origin"]["name"] == "yolo11n today"


@needs_node
def test_a_taken_name_asks_before_replacing(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        var p = make(); await p.init(); await p.pick('p1'); await settle();
        edit({epochs: 5}); await settle();
        p.openSave(); _els['m-name'].value = 'baseline';
        var first = await p.save(); await settle();
        var asked = state(p);
        _els['m-name'].dispatchEvent(new Event('input'));
        var typed = state(p);
        var second = await p.save(); await settle();
        var again = state(p);
        var third = await p.save(); await settle();
        return {first: first, asked: asked, typed: typed, second: second, again: again, third: third, after: state(p)};
        """,
    )
    assert out["first"] is False
    assert out["asked"]["saveBtn"] == "Replace" and out["asked"]["saveNote"] == "Replace “Baseline”?"
    assert out["asked"]["saveShown"] is True
    # Typing withdraws the question; the next Save asks again, and the one after replaces.
    assert out["typed"]["saveBtn"] == "Save" and out["typed"]["saveNote"] == ""
    assert out["second"] is False and out["again"]["saveBtn"] == "Replace"
    assert out["third"] is True
    after = out["after"]
    p2 = next(p for p in after["store"] if p["id"] == "p2")
    assert p2["epochs"] == 5 and p2["name"] == "Baseline" and p2["created"] == P2["created"]
    assert after["origin"] == {"kind": "preset", "id": "p2", "name": "Baseline"} and after["modified"] is False
    assert after["toasts"][-1] == "success: Replaced preset “Baseline”"
    assert len(after["store"]) == 2


@needs_node
def test_escape_and_cancel_close_the_name_field(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        var p = make(); await p.init(); await settle();
        p.openSave(); var esc = key('Escape');
        var closed = state(p);
        p.openSave(); _els['m-cancel-btn'].click();
        return {esc: esc, closed: closed, cancelled: state(p)};
        """,
    )
    assert out["esc"] == {"prevented": True, "stopped": True}
    assert out["closed"]["saveShown"] is False and out["closed"]["barShown"] is True
    assert out["cancelled"]["saveShown"] is False and out["cancelled"]["barShown"] is True


@needs_node
def test_a_failed_save_says_so_in_the_bar(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        var p = make(); await p.init(); await settle();
        window.SAVE_ERROR = 'disk full';
        p.openSave(); var ok = await p.save(); await settle();
        return {ok: ok, state: state(p)};
        """,
    )
    assert out["ok"] is False
    assert out["state"]["saveNote"] == "Could not save: disk full" and out["state"]["saveNoteError"] is True
    assert out["state"]["saveShown"] is True
    assert out["state"]["toasts"] == ["error: Could not save the preset: disk full"]


@needs_node
def test_on_an_older_hub_the_bars_trash_deletes_the_selected_preset_at_once(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        var p = make(); await p.init(); await settle();
        var onDefaults = state(p);
        await p.pick('p1'); await settle();
        var picked = state(p);
        _els['m-delete'].click(); await settle();
        return {onDefaults: onDefaults, picked: picked, after: state(p)};
        """,
    )
    assert out["onDefaults"]["deleteShown"] is False
    picked = out["picked"]
    assert picked["deleteShown"] is True
    assert picked["options"][1][1].startswith("Small objects 1280 \u2014 yolo11n · 100 · "), "the age is in the label"
    assert "data-removable" not in picked["attrs"]["p1"]
    after = out["after"]
    assert [p["id"] for p in after["store"]] == ["p2"], "deleted at once, nothing asked"
    assert [o[0] for o in after["options"]] == ["", "p2"]
    assert after["form"] == _values(P1), "the form is left as it was"
    assert after["origin"]["kind"] == "defaults" and after["modified"] is True and after["deleteShown"] is False
    assert after["toasts"] == []


@needs_node
def test_with_the_hubs_list_presets_are_deleted_in_it_and_the_age_is_a_hint(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        window.TlcPicker = { onRemove: function (sel, run) { window.REMOVE = { sel: sel, run: run }; } };
        var p = make(); await p.init(); await p.pick('p1'); await settle();
        var opened = state(p);
        var registered = window.REMOVE.sel === _els['m-select'];
        await window.REMOVE.run({ value: 'p2' }); await settle();
        var other = state(p);
        await window.REMOVE.run({ value: 'p1' }); await settle();
        var own = state(p);
        var failed = null;
        try { await window.REMOVE.run({ value: 'gone' }); } catch (e) { failed = e.message; }
        return {opened: opened, registered: registered, other: other, own: own, failed: failed};
        """,
    )
    opened = out["opened"]
    assert out["registered"] is True
    assert opened["deleteShown"] is False, "no trash beside the select: it is in the list"
    assert opened["options"][1] == ["p1", "Small objects 1280 \u2014 yolo11n · 100"], "no age in the label"
    assert opened["attrs"]["p1"]["data-removable"] == ""
    assert opened["attrs"]["p1"]["data-hint"].startswith("saved "), "never used: when it was saved"
    assert "data-removable" not in opened["attrs"][""], "Default settings cannot be deleted"
    other = out["other"]
    assert [p["id"] for p in other["store"]] == ["p1"]
    assert other["origin"]["id"] == "p1" and other["modified"] is False, "deleting another preset changes nothing else"
    own = out["own"]
    assert own["store"] == []
    assert own["origin"]["kind"] == "defaults" and own["modified"] is True and own["value"] == ""
    assert own["form"] == _values(P1)
    assert own["toasts"] == [], "nothing is announced"
    assert out["failed"] == "Not found", "a failure reaches the list, which shows it"


@needs_node
def test_a_summary_given_as_parts_leaves_out_the_empty_ones(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        var parts = function (v) { return [v.model === 'yolo11s' ? '' : v.model, null, v.epochs + ' epochs']; };
        var p = make({summary: parts});
        await p.init(); await settle();
        return state(p);
        """,
    )
    assert out["options"][1][1].startswith("Small objects 1280 \u2014 yolo11n · 100 epochs · ")
    assert out["options"][2][1].startswith("Baseline \u2014 10 epochs · ")


@needs_node
def test_an_asynchronous_apply_is_awaited_before_the_form_is_compared(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        window.ASYNC_APPLY = true;
        var p = make(); await p.init(); await settle();
        await p.pick('p1');
        return state(p);
        """,
    )
    assert out["form"] == _values(P1) and out["modified"] is False and out["origin"]["id"] == "p1"


@needs_node
def test_a_list_that_cannot_be_loaded_still_opens_the_form(tmp_path: Path) -> None:
    out = _run(
        tmp_path,
        """
        window.LIST_ERROR = true;
        var p = make(); await p.init(); await settle();
        return state(p);
        """,
    )
    assert out["form"] == DEFAULTS and out["options"] == [["", "Default settings"]]
    assert out["toasts"] == ["error: Could not load presets: HTTP 500"]


def test_script_is_the_module_constant() -> None:
    assert preset_ui_script() is PRESET_UI_JS
    assert "function _tlcPluginPresets(" in PRESET_UI_JS


@needs_node
def test_the_time_is_the_last_use_else_the_save_and_replace_keeps_both(tmp_path: Path) -> None:
    used = {**P1, "last_run": "2026-10-26T10:00:00Z"}
    out = _run(
        tmp_path,
        """
        var p = make(); await p.init(); await p.pick('p1'); await settle();
        var opened = state(p);
        edit({epochs: 7}); await settle();
        p.openSave(); _els['m-name'].value = 'Small objects 1280';
        await p.save(); await p.save(); await settle();
        return {opened: opened, after: state(p)};
        """,
        store=[used, dict(P2)],
    )
    assert out["opened"]["options"][1][1].endswith(" · used yesterday") or " · used " in out["opened"]["options"][1][1]
    assert " · saved " in out["opened"]["options"][2][1], "never used: when it was saved"
    replaced = next(p for p in out["after"]["store"] if p["id"] == "p1")
    assert replaced["epochs"] == 7
    assert replaced["created"] == P1["created"] and replaced["last_run"] == used["last_run"]
