# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Resolve run inputs from explicit lineage or metrics-table foreign references."""

from __future__ import annotations

from typing import Any


def run_inputs(run: Any, record: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return exact input URLs, retaining explicit indices and deduplicating fallback references.

    A metrics table's value-map keys are local to that table: separate train and
    validation tables can both use key zero. Resolve their URLs relative to each
    metrics table before assigning fallback indices in discovery order.
    """
    import tlc
    from tlc.helpers.project_layout import ProjectLayout

    def describe(item: dict[str, Any], name: Any = None) -> dict[str, Any]:
        if isinstance(name, str) and name.strip():
            item["name"] = name
        parts = ProjectLayout.table_url_parts(item["table_url"])
        if parts is not None:
            item["dataset_name"] = parts[2]
        return item

    constants = record.get("constants", {}) if record is not None else run.constants
    result = []
    for index, entry in enumerate((constants or {}).get("inputs") or []):
        raw = entry.get("input_table_url") if isinstance(entry, dict) else str(entry)
        if raw:
            result.append(
                describe(
                    {
                        "index": index,
                        "table_url": str(tlc.Url(raw).to_absolute(run.url)),
                        "rows": entry.get("num_examples") if isinstance(entry, dict) else None,
                        "source": "run",
                    },
                    entry.get("name") if isinstance(entry, dict) else None,
                )
            )
    if result:
        return result
    if record is None:
        tables = run.metrics_tables or []
    else:
        tables = []
        for item in record.get("metrics", []):
            if isinstance(item, dict) and item.get("url"):
                try:
                    tables.append(tlc.Table.from_url(tlc.Url(item["url"]).to_absolute(run.url)))
                except FileNotFoundError:
                    # A running worker can publish the reference before the
                    # table becomes visible. Keep the rest of run_info usable.
                    continue
    seen: set[str] = set()
    for table in tables:
        try:
            values = table.get_value_map("input_table_id") or {}
        except (KeyError, ValueError):
            continue
        for entry in values.values():
            raw = entry.get("url") if isinstance(entry, dict) else getattr(entry, "url", None)
            if not raw:
                continue
            url = str(tlc.Url(str(raw)).to_absolute(table.url))
            if url not in seen:
                seen.add(url)
                result.append(describe({"index": len(result), "table_url": url, "rows": None, "source": "metrics"}))
    return result
