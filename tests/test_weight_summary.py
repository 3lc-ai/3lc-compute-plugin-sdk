# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
from types import SimpleNamespace

import numpy as np
import pyarrow as pa
import pytest

from tlc_plugin_sdk.shared.weights import sample_weights, weight_summary


class Table:
    url = "s3://bucket/exact-revision"

    def __init__(self, weights, column="selection", default=None):
        self.weights = weights
        self.weights_column_name = column
        self.reads = []
        self.rows_schema = SimpleNamespace(
            values={column: SimpleNamespace(value=SimpleNamespace(default_value=default))}
        )

    def __len__(self):
        return len(self.weights)

    def get_column_as_pyarrow_array(self, name):
        self.reads.append(name)
        assert name == "selection"
        return pa.array(self.weights)

    @property
    def table_rows(self):
        msg = "Must not iterate rows or load images"
        raise AssertionError(msg)


def test_physical_rows_positive_samples_and_weight_sum_are_distinct():
    table = Table([2.0] * 5000 + [0.0] * 24537)
    result = weight_summary(table)
    assert result == {
        "url": table.url,
        "total_rows": 29537,
        "positive_weight_rows": 5000,
        "zero_weight_rows": 24537,
        "weight_sum": 10000.0,
        "weight_column": "selection",
        "implicit_unit_weights": False,
    }
    assert table.reads == ["selection"]


def test_implicit_unit_weights_and_empty_table():
    table = Table([None] * 3, column=None)
    assert weight_summary(table)["positive_weight_rows"] == 3
    assert table.reads == []
    assert weight_summary(Table([]))["positive_weight_rows"] == 0


@pytest.mark.parametrize("weights", [[1, -1], [None, 1], [np.nan], [np.inf]])
def test_invalid_weights_fail_instead_of_becoming_excluded(weights):
    with pytest.raises(ValueError, match="finite, nonnegative"):
        sample_weights(Table(weights))


def test_sparse_null_weights_use_schema_default_without_row_iteration():
    table = Table([None, 0, 2, None], default=1.0)
    result = weight_summary(table)
    assert result["positive_weight_rows"] == 3
    assert result["zero_weight_rows"] == 1
    assert result["weight_sum"] == 4.0
    assert table.reads == ["selection"]
    assert weight_summary(Table([None, 2], default=0.0))["positive_weight_rows"] == 1
    with pytest.raises(ValueError):
        sample_weights(Table([float("nan")], default=1.0))
