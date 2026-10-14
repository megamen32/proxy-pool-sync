import threading

import httpx

from proxy_pool_sync.binding_api import BindingFilePeer, BindingPeer, BindingPeerWithFileFallback, binding_server
from proxy_pool_sync.bindings import BindingJournal


def test_real_two_service_api_delivery_is_private_idempotent_and_not_an_apply(tmp_path):
    a = BindingJournal(tmp_path / 'a.db', scope='fixture', source='autosell')
    b = BindingJournal(tmp_path / 'b.db', scope='fixture', source='tgc')
    token = 'isolated-fixture-token-' + 'x' * 40
    server = binding_server(('127.0.0.1', 0), b, bearer_token=token, allowed_source='autosell')
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}/v1/bindings'
        event = a.observe(999991, 'http://198.51.100.1:8080', '198.51.100.1')
        assert httpx.post(url, content=event.encode()).status_code == 401
        peer = BindingPeer(url, token)
        assert token not in repr(peer)
        assert peer.deliver(event)
        assert peer.deliver(event)
        assert b.preferred(999991) == event
        assert peer.preferred(999991) == event
        assert peer.preferences() == [event]
        assert peer.preferred(42) is None
        response = httpx.get(url, headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200
        assert response.json()['bindings'][0]['telegram_user_id'] == 999991
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()


def test_same_receiver_journal_accepts_events_while_http_bridge_is_down(tmp_path, monkeypatch):
    sender = BindingJournal(tmp_path / 'sender.db', scope='fixture', source='autosell')
    receiver = BindingJournal(tmp_path / 'receiver.db', scope='fixture', source='tgc')
    event = sender.observe(999991, 'http://198.51.100.1:8080', '198.51.100.1')
    token = 'isolated-fixture-token-' + 'x' * 40
    primary = BindingPeer('http://127.0.0.1:17883/v1/bindings', token)
    file_peer = BindingFilePeer(receiver.path, 'fixture', 'tgc', 'autosell')
    peer = BindingPeerWithFileFallback(primary, file_peer)

    def disconnected(*args, **kwargs):
        raise httpx.ConnectError('bridge offline')

    monkeypatch.setattr(httpx, 'post', disconnected)
    monkeypatch.setattr(httpx, 'get', disconnected)
    assert peer.deliver(event)
    assert peer.deliver(event)  # Same idempotency behavior as HTTP.
    assert peer.preferred(999991) == event
    assert peer.preferences() == [event]
    assert receiver.preferred(999991) == event


def test_file_fallback_does_not_bypass_http_rejection_or_create_another_store(tmp_path, monkeypatch):
    sender = BindingJournal(tmp_path / 'sender.db', scope='fixture', source='autosell')
    receiver = BindingJournal(tmp_path / 'receiver.db', scope='fixture', source='tgc')
    event = sender.observe(999991, 'http://198.51.100.1:8080')
    token = 'isolated-fixture-token-' + 'x' * 40
    primary = BindingPeer('http://127.0.0.1:17883/v1/bindings', token)
    peer = BindingPeerWithFileFallback(primary, BindingFilePeer(receiver.path, 'fixture', 'tgc', 'autosell'))
    monkeypatch.setattr(httpx, 'post', lambda *args, **kwargs: httpx.Response(403))
    assert peer.deliver(event) is False
    assert receiver.preferred(999991) is None
    try:
        BindingFilePeer(tmp_path / 'missing.db', 'fixture', 'tgc', 'autosell')
    except FileNotFoundError:
        pass
    else:
        raise AssertionError('fallback must not create an independent journal')


def test_local_peer_endpoint_uses_same_api_after_remote_bridge_stops(tmp_path):
    a = BindingJournal(tmp_path / 'a.db', scope='fixture', source='autosell')
    b = BindingJournal(tmp_path / 'b.db', scope='fixture', source='tgc')
    event = a.observe(999991, 'http://198.51.100.1:8080', '198.51.100.1')
    token_a, token_b = 'a' * 40, 'b' * 40
    remote = binding_server(('127.0.0.1', 0), b, bearer_token=token_b, allowed_source='autosell')
    remote_thread = threading.Thread(target=remote.serve_forever)
    remote_thread.start()
    local = binding_server(
        ('127.0.0.1', 0), a, bearer_token=token_a, allowed_source='tgc',
        peer=BindingPeerWithFileFallback(
            BindingPeer(f'http://127.0.0.1:{remote.server_port}/v1/bindings', token_b),
            BindingFilePeer(b.path, 'fixture', 'tgc', 'autosell'),
        ),
    )
    local_thread = threading.Thread(target=local.serve_forever)
    local_thread.start()
    try:
        local_peer = BindingPeer(f'http://127.0.0.1:{local.server_port}/v1/peer-bindings', token_a)
        assert local_peer.deliver(event)
        assert b.preferred(999991) == event
        remote.shutdown()
        remote_thread.join(3)
        remote.server_close()
        assert local_peer.deliver(event)  # Existing receiver file remains authoritative.
        assert local_peer.preferred(999991) == event
        assert local_peer.preferences() == [event]
    finally:
        local.shutdown()
        local_thread.join(3)
        local.server_close()
        if remote_thread.is_alive():
            remote.shutdown()
            remote_thread.join(3)
            remote.server_close()
