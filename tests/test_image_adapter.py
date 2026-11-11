# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""File references must honor a registered storage adapter, including in a sandbox."""

from io import BytesIO

from PIL import Image

from tlc_plugin_sdk.shared import images


def test_file_images_and_dimensions_are_read_through_the_adapter(monkeypatch):
    content = BytesIO()
    Image.new("RGB", (19, 13), "red").save(content, format="PNG")
    calls = []

    class DataUrl:
        scheme = "file"

        def to_str(self):
            return "file:///inaccessible/remote/image.png"

        def read_bytes(self):
            calls.append("read")
            return content.getvalue()

    monkeypatch.setattr(images, "resolve_image_url", lambda *args: DataUrl())
    assert images.load_image("<DATA>/image.png").size == (19, 13)
    assert images.read_image_size("<DATA>/image.png") == (19, 13)
    assert calls == ["read", "read"]
