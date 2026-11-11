# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Metrics-table references remain usable when a run has no explicit input list."""

from types import SimpleNamespace

from tlc_plugin_sdk.shared.run_inputs import run_inputs


def test_metric_local_zero_keys_do_not_merge_different_inputs():
    def table(name, reference):
        return SimpleNamespace(url=f"/projects/P/runs/R/{name}", get_value_map=lambda _: {0.0: {"url": reference}})

    run = SimpleNamespace(
        url="/projects/P/runs/R",
        constants={},
        metrics_tables=[
            table("train_metrics", "../../../datasets/train/tables/initial"),
            table("val_metrics", "../../../datasets/val/tables/initial"),
            table("another_train_epoch", "../../../datasets/train/tables/initial"),
        ],
    )
    result = run_inputs(run)
    assert [entry["index"] for entry in result] == [0, 1]
    assert [entry["table_url"] for entry in result] == [
        "/projects/P/datasets/train/tables/initial",
        "/projects/P/datasets/val/tables/initial",
    ]
    assert all(entry["source"] == "metrics" for entry in result)


def test_current_record_lineage_wins_over_cached_constants():
    run = SimpleNamespace(url="s3://bucket/projects/P/runs/R", constants={"inputs": []})
    result = run_inputs(
        run,
        {
            "constants": {
                "inputs": [
                    {"input_table_url": "../../datasets/val/tables/initial", "num_examples": 4},
                ]
            }
        },
    )
    assert result == [
        {
            "index": 0,
            "table_url": "s3://bucket/projects/P/datasets/val/tables/initial",
            "rows": 4,
            "source": "run",
            "dataset_name": "val",
        }
    ]


def test_recorded_input_names_and_dataset_identity_are_not_discarded():
    run = SimpleNamespace(
        url="s3://bucket/projects/P/runs/R",
        constants={
            "inputs": [
                {"input_table_url": "../../datasets/Train images/tables/revision-2", "name": "training-input"},
                {"input_table_url": "../../datasets/val/tables/initial"},
            ]
        },
    )
    items = run_inputs(run)
    assert items[0]["name"] == "training-input"
    assert items[0]["dataset_name"] == "Train images"
    assert "name" not in items[1]  # Never invent a recorded input name.
    assert items[1]["dataset_name"] == "val"
