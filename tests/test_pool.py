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
