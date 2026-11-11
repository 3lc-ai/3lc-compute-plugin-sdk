# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Column-only sample weight reads, shared by summaries and derived table jobs."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np
    import numpy.typing as npt
    from tlc import Table


def sample_weights(table: Table) -> tuple[str | None, npt.NDArray[np.float64]]:
    """Read the schema-declared weight column without decoding sample images.

    Missing columns mean unit weights. Sparse null cells use the declared schema
    default, as the row view does. Other null, negative and nonfinite values fail.
    """
    import numpy as np
    import pyarrow as pa

    column = table.weights_column_name
    if column is None:
        return None, np.ones(len(table), dtype=np.float64)
    array = table.get_column_as_pyarrow_array(column).cast(pa.float64())
    default = None
    if array.null_count:
        value_schema = table.rows_schema.values[column].value
        default = value_schema.default_value if value_schema is not None else None
    filled = array.fill_null(default) if default is not None else array
    values = np.asarray(filled.to_numpy(zero_copy_only=False), dtype=np.float64)
    if values.ndim != 1 or len(values) != len(table) or not np.all(np.isfinite(values)) or np.any(values < 0):
        msg = "Sample weights must be finite, nonnegative numbers for every row"
        raise ValueError(msg)
    return column, values


def weight_summary(table: Table) -> dict[str, object]:
    """Return bounded counts for this exact revision; never a row list or latest revision."""
    import numpy as np

    column, weights = sample_weights(table)
    positive = int(np.count_nonzero(weights > 0))
    return {
        "url": str(table.url),
        "total_rows": len(table),
        "weight_column": column,
        "positive_weight_rows": positive,
        "zero_weight_rows": len(table) - positive,
        "weight_sum": float(weights.sum()),
        "implicit_unit_weights": column is None,
    }
