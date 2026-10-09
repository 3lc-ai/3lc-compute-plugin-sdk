# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Model uploads use the public Url API and report success only after writing."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tlc_plugin_sdk.shared.model_storage import save_model_to_run


@pytest.mark.parametrize("fail", [False, True])
def test_cloud_checkpoint_upload_uses_url_write_bytes(monkeypatch, tmp_path: Path, fail: bool) -> None:
    writes: dict[str, bytes] = {}

    class Url(str):
        def to_absolute(self):
            return self

        def __truediv__(self, part):
            return Url(str(self).rstrip("/") + "/" + str(part))

        def write_bytes(self, data):
            if fail:
                message = "upload refused"
                raise OSError(message)
            writes[str(self)] = data

    monkeypatch.setitem(sys.modules, "tlc", SimpleNamespace(Url=Url))
    source = tmp_path / "best.pt"
    source.write_bytes(b"checkpoint contents")
    messages: list[str] = []
    args = {
        "run_url": "s3://bucket/project/runs/test",
        "model_data": None,
        "filename": "best.pt",
        "source_file": source,
        "on_status": messages.append,
    }
    if fail:
        with pytest.raises(OSError, match="upload refused"):
            save_model_to_run(**args)
        assert not any("Uploaded model" in line for line in messages)
    else:
        assert save_model_to_run(**args) == "model/best.pt"
        assert writes == {"s3://bucket/project/runs/test/model/best.pt": b"checkpoint contents"}
        assert source.read_bytes() == b"checkpoint contents"
