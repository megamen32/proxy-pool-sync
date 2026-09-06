from proxy_pool_sync import FileProxyPool, parse_proxy_export


def test_parse_normalizes_and_deduplicates():
    assert parse_proxy_export("# x\n1.2.3.4:80\nhttp://1.2.3.4:80\nsocks5://u:p@5.6.7.8:1080\ninvalid\n") == [
        "http://1.2.3.4:80",
        "socks5://u:p@5.6.7.8:1080",
    ]


def test_file_pool_atomic_and_refuses_empty(tmp_path):
    pool = FileProxyPool(tmp_path / "proxies.txt")
    pool.replace_text("1.1.1.1:8000\n2.2.2.2:9000\n")
    assert len(pool.load()) == 2
    try:
        pool.replace_text("# empty\n")
    except ValueError:
        pass
    else:
        raise AssertionError("empty export must fail")
    assert len(pool.load()) == 2


def test_sanitize_proxy_lines_preserves_legacy_scheme_inference_input():
    from proxy_pool_sync import sanitize_proxy_lines
    assert sanitize_proxy_lines("  1.2.3.4:1085  \n# x\n1.2.3.4:1085\nsocks5://5.6.7.8:1080\n") == [
        "1.2.3.4:1085",
        "socks5://5.6.7.8:1080",
    ]


def test_replace_raw_lines_keeps_legacy_no_scheme_form(tmp_path):
    pool = FileProxyPool(tmp_path / "legacy.txt")
    pool.replace_raw_lines(["1.2.3.4:1085", "socks5://5.6.7.8:1080"])
    assert (tmp_path / "legacy.txt").read_text() == "1.2.3.4:1085\nsocks5://5.6.7.8:1080\n"


def test_pool_update_policy_rejects_catastrophic_drop_and_keeps_old_file(tmp_path):
    from proxy_pool_sync import PoolUpdatePolicy

    pool = FileProxyPool(tmp_path / "safe.txt")
    pool.replace_lines([f"http://10.0.0.{i}:80" for i in range(1, 11)])
    try:
        pool.replace_lines(
            ["http://10.0.0.1:80", "http://10.0.0.2:80"],
            update_policy=PoolUpdatePolicy(min_count=1, max_drop_ratio=0.5),
        )
    except ValueError as exc:
        assert "shrank" in str(exc)
    else:
        raise AssertionError("catastrophic shrink must be rejected")
    assert len(pool.load()) == 10


def test_pool_update_policy_force_allows_intentional_drop(tmp_path):
    from proxy_pool_sync import PoolUpdatePolicy

    pool = FileProxyPool(tmp_path / "safe.txt")
    pool.replace_lines([f"http://10.0.0.{i}:80" for i in range(1, 11)])
    result = pool.replace_lines(
        ["http://10.0.0.1:80"],
        update_policy=PoolUpdatePolicy(max_drop_ratio=0.1),
        force=True,
    )
    assert result == ["http://10.0.0.1:80"]
