import threading

import httpx

from proxy_pool_sync.binding_api import BindingPeer, binding_server
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
        assert peer.preferred(42) is None
        response = httpx.get(url, headers={'Authorization': 'Bearer ' + token})
        assert response.status_code == 200
        assert response.json()['bindings'][0]['telegram_user_id'] == 999991
    finally:
        server.shutdown()
        thread.join(3)
        server.server_close()
