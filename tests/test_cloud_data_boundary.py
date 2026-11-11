# Copyright 2026 3LC Inc.
import sys
from types import SimpleNamespace

import pytest

from tlc_plugin_sdk import data_broker as storage


@pytest.fixture
def service(monkeypatch):
    class Url(str):
        def expand_aliases(self, **kwargs):
            return self

        def to_absolute(self):
            return self

    monkeypatch.setitem(sys.modules, "tlc", SimpleNamespace(Url=Url))
    return storage.DataBroker(["/safe/data"], aliases={}, allow_node_cloud_storage=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "https://example.test/data",
        "ftp://example.test/file",
        "file:///etc/passwd",
        "/etc/passwd",
        "/safe/data/../../etc/passwd",
        "file://remote/safe/data/image.jpg",
        "s3://access:secret@bucket/key",
        "s3://bucket/key?token=secret",
        "s3://bucket:443/key",
        "s3://bucket/root/../secret",
        "s3://bucket/root/%2e%2e/secret",
        "s3://bucket/root%2fsecret",
        "abfs://secret:password@account.dfs.core.windows.net/data",
        "abfs://container@evil.test/data",
        "s3://bucket/.aws/credentials",
        "s3://bucket/keys.env",
        "https://bucket.s3.amazonaws.com/data?X-Amz-Signature=x",
    ],
)
def test_only_native_node_managed_storage_is_admitted(service, url):
    with pytest.raises((PermissionError, ValueError)):
        service.checked(url)


@pytest.mark.parametrize(
    "url",
    [
        "s3://authorized-bucket/data/image.jpg",
        "gs://authorized-bucket/data/table.json",
        "abfss://container@account.dfs.core.windows.net/data/image.png",
    ],
)
def test_native_cloud_urls_reach_adapter_authorization(service, url):
    assert service.checked(url) == url


def test_default_bridge_still_requires_configured_roots(service):
    strict = storage.DataBroker(["s3://bucket/project"], aliases={})
    with pytest.raises(PermissionError):
        strict.checked("s3://other-bucket/data")
