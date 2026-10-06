"""Credential pools remain private throughout their atomic replacement."""
import os
import stat

import pytest

from proxy_pool_sync import FileProxyPool
from proxy_pool_sync import pool as pool_module


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permission contract")
@pytest.mark.parametrize("method", ["replace_text", "replace_lines", "replace_raw_lines"])
def test_replacement_is_private_before_publish_and_preserves_owner_read_access(tmp_path, monkeypatch, method):
    path = tmp_path / "credentials.txt"
    path.write_text("old pool\n")
    path.chmod(0o644)
    payload = "http://fixture-user:fixture-password@127.0.0.1:12015"
    original_replace = os.replace
    published = []

    def verify_before_publish(source, target):
        source = type(path)(source)
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert source.read_text() == payload + "\n"
        assert path.read_text() == "old pool\n"
        published.append(source)
        return original_replace(source, target)

    monkeypatch.setattr(pool_module.os, "replace", verify_before_publish)
    pool = FileProxyPool(path)
    if method == "replace_text":
        result = pool.replace_text(payload)
    else:
        result = getattr(pool, method)([payload])
    assert result == [payload]
    assert path.read_text() == payload + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_uid == os.geteuid()
    assert published and all(not item.exists() for item in published)


@pytest.mark.parametrize("method", ["replace_text", "replace_raw_lines"])
def test_failed_atomic_publish_preserves_old_pool_and_cleans_only_its_temporary_file(tmp_path, monkeypatch, method):
    path = tmp_path / "pool.txt"
    path.write_text("http://127.0.0.1:12000\n")
    old_payload = path.read_bytes()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("preserve unrelated work")

    def fail_publish(_source, _target):
        raise OSError("fixture replace failure")

    monkeypatch.setattr(pool_module.os, "replace", fail_publish)
    pool = FileProxyPool(path)
    with pytest.raises(OSError, match="fixture replace failure"):
        if method == "replace_text":
            pool.replace_text("http://127.0.0.1:12015")
        else:
            pool.replace_raw_lines(["127.0.0.1:12015"])
    assert path.read_bytes() == old_payload
    assert unrelated.read_text() == "preserve unrelated work"
    assert set(tmp_path.iterdir()) == {path, unrelated}
