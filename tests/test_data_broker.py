# Copyright 2026 3LC Inc.
# SPDX-License-Identifier: Apache-2.0
"""Real file-boundary checks for the isolated Python storage bridge."""

import base64
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tlc_plugin_sdk import data_broker as storage


@pytest.fixture
def broker(tmp_path, monkeypatch):
    class Url(str):
        def expand_aliases(self, **kwargs):
            return self

        def to_absolute(self):
            return self

        def exists(self):
            return Path(self).exists()

    def entry(path):
        return SimpleNamespace(name=path.name, path=str(path), size=path.stat().st_size, is_dir=path.is_dir)

    aliases = SimpleNamespace(get_registered_url_aliases=dict)
    registry = SimpleNamespace(
        get_file_size=lambda url: Path(url).stat().st_size,
        list_dir=lambda url: (entry(p) for p in Path(url).iterdir()),
        stat=lambda url: entry(Path(url)),
        is_dir=lambda url: Path(url).is_dir(),
    )
    monkeypatch.setitem(sys.modules, "tlc", SimpleNamespace(Url=Url))
    monkeypatch.setitem(sys.modules, "tlc.helpers.url_aliases", aliases)
    monkeypatch.setitem(sys.modules, "tlcurl.url_adapters._registry", SimpleNamespace(UrlAdapterRegistry=registry))
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setattr(storage, "_trusted_aliases", dict)
    return storage.DataBroker([str(root)]), root


def test_read_chunks_preserve_real_data_and_do_not_allow_writes(broker):
    service, root = broker
    data = b"a" * (storage.CHUNK_BYTES + 17)
    path = root / "rows.parquet"
    path.write_bytes(data)
    first = service.request({"operation": "read", "url": str(path)})
    second = service.request({"operation": "read", "url": str(path), "offset": storage.CHUNK_BYTES})
    assert base64.b64decode(first["data"]) + base64.b64decode(second["data"]) == data
    assert first["size"] == len(data)
    for operation in ("write", "delete", "execute", "model"):
        with pytest.raises(PermissionError, match="read-only"):
            service.request({"operation": operation, "url": str(path), "data": "overwrite"})
    assert path.read_bytes() == data


def test_outside_private_symlink_and_hardlink_reads_are_refused(broker):
    service, root = broker
    outside = root.parent / "outside.txt"
    outside.write_text("private")
    (root / "secret.env").write_text("private")
    (root / "config.yaml").write_text("private")
    (root / "link.txt").symlink_to(outside)
    os.link(outside, root / "hardlink.txt")
    for path in (outside, root / "../outside.txt", root / "secret.env", root / "config.yaml", root / "link.txt"):
        with pytest.raises(PermissionError):
            service.request({"operation": "read", "url": str(path)})
    with pytest.raises(ValueError, match="regular file"):
        service.request({"operation": "read", "url": str(root / "hardlink.txt")})
    with pytest.raises(ValueError, match="configured storage root"):
        service.request({"operation": "read", "url": "http://169.254.169.254/latest/meta-data/"})


def test_cloud_project_names_with_spaces_preserve_exact_object_keys(broker):
    root = "s3://datasets/projects/Flow BB Loop 8"
    service = storage.DataBroker([root], aliases={})
    image = root + "/source/images/image 1.jpg"
    assert service.checked(image) == image
    with pytest.raises(PermissionError, match="outside"):
        service.checked("s3://datasets/projects/Flow BB Loop 80/image.jpg")
    for invalid in (
        "s3://bad bucket/image.jpg",
        root + "/../private.jpg",
        root + "/a%2fb.jpg",
        root + "/im\tage.jpg",
        root + "/im%0aage.jpg",
        " " + image,
    ):
        with pytest.raises(ValueError):
            service.checked(invalid)


def test_large_files_and_turn_budget_are_bounded(broker, monkeypatch):
    service, root = broker
    path = root / "data.bin"
    path.write_bytes(b"abcdefgh")
    monkeypatch.setattr(storage, "MAX_FILE_BYTES", 4)
    with pytest.raises(ValueError, match="read limit"):
        service.request({"operation": "read", "url": str(path)})
    monkeypatch.setattr(storage, "MAX_FILE_BYTES", 64)
    monkeypatch.setattr(storage, "MAX_TURN_BYTES", 4)
    with pytest.raises(ValueError, match="sandbox read limit"):
        service.request({"operation": "read", "url": str(path)})


def test_bootstrap_refuses_a_handoff_containing_credentials(broker, monkeypatch):
    import _tlc_auth_state
    from _tlcnative import _core

    service, _ = broker
    claims = {
        "tenant_id": "org",
        "user_id": "user",
        "expires_at": 2000000000,
        "auth_mode": "api_key",
        "authorization_context": "compute-service",
    }
    monkeypatch.setattr(_tlc_auth_state, "ensure_key_activated", lambda: None)
    monkeypatch.setattr(_core, "_emit_inherited_token", lambda: base64.b64encode(json.dumps(claims).encode()).decode())
    assert set(json.loads(base64.b64decode(service.bootstrap()["identity"]))) == set(claims)
    claims["api_key"] = "must-not-cross"
    with pytest.raises(PermissionError, match="Unsupported SDK identity"):
        service.bootstrap()


def test_listing_omits_private_files_and_broad_aliases_do_not_grant_home(broker):
    service, root = broker
    (root / "image.png").write_bytes(b"image")
    (root / ".env").write_text("private")
    result = service.request({"operation": "list", "url": str(root)})
    assert [entry["name"] for entry in result["value"]] == ["image.png"]
    assert not storage._safe_root(str(storage.Path.home()))
    assert not storage._safe_root(str(storage.Path.home() / ".3lc-compute"))


def test_dataset_aliases_cannot_grant_new_host_roots(tmp_path, monkeypatch):
    values = {"<DATA>": str(tmp_path / "data"), "<EVIL>": str(tmp_path / "private")}
    origins = {
        "<DATA>": SimpleNamespace(source=SimpleNamespace(name="CONFIG_SERVICE_AXIOM"), source_info="service"),
        "<EVIL>": SimpleNamespace(
            source=SimpleNamespace(name="DATA_CONFIG_FILE"), source_info="s3://data/default_aliases.3lc.yaml"
        ),
    }
    monkeypatch.setitem(
        sys.modules, "tlc.helpers.url_aliases", SimpleNamespace(get_registered_url_aliases=lambda: values)
    )
    monkeypatch.setitem(
        sys.modules,
        "tlcconfig",
        SimpleNamespace(
            ConfigStore=SimpleNamespace(instance=lambda: SimpleNamespace(get_provenance=lambda key: origins))
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "tlcconfig.default_locations",
        SimpleNamespace(get_default_root_dir=lambda: str(tmp_path / "config")),
    )
    assert storage._trusted_aliases() == {"<DATA>": values["<DATA>"]}


def test_cloud_reads_enforce_size_during_io_without_an_unbounded_fallback(broker, monkeypatch):
    service, _ = broker
    calls = []
    base = sys.modules["tlc"].Url

    class Cloud(base):
        def read_bytes(self):
            msg = "A stat must never authorize an unbounded cloud read"
            raise AssertionError(msg)

        def read_bytes_bounded(self, limit):
            calls.append(limit)
            msg = "object grew beyond its read bound"
            raise OSError(msg)

    monkeypatch.setitem(sys.modules, "tlc", SimpleNamespace(Url=Cloud))
    registry = sys.modules["tlcurl.url_adapters._registry"].UrlAdapterRegistry
    monkeypatch.setattr(registry, "get_file_size", lambda _: 1)
    service = storage.DataBroker(["s3://bucket/data"])
    with pytest.raises(OSError, match="grew"):
        service.request({"operation": "read", "url": "s3://bucket/data/image.png"})
    assert calls == [storage.MAX_FILE_BYTES] and not service.cache
