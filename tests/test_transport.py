import pytest
from proxy_pool_sync.transport import LocalForward, public_proxy_ref, render_scoped_redirect, render_ssh_forwards
from proxy_pool_sync.transport import probe_exit_ip
from unittest.mock import patch


def test_ssh_config_is_loopback_only_with_distinct_listener_ports():
    one = LocalForward(18880, '198.51.100.1', 8080)
    text = render_ssh_forwards('isolated', '203.0.113.1', [one])
    assert 'GatewayPorts no' in text
    assert 'StrictHostKeyChecking yes' in text
    assert 'LocalForward 127.0.0.1:18880 198.51.100.1:8080' in text
    with pytest.raises(ValueError):
        render_ssh_forwards('isolated', '203.0.113.1', [one, one])


def test_public_reference_strips_auth_but_preserves_protocol_and_port():
    assert public_proxy_ref('http://secret-user:secret-password@198.51.100.1:8085') == 'http://198.51.100.1:8085'
    assert public_proxy_ref('198.51.100.1:1085') == 'http://198.51.100.1:1085'


def test_redirect_never_targets_whole_host_or_foreign_table():
    cg = 'system.slice/docker-' + 'a' * 64 + '.scope'
    text = render_scoped_redirect([{'cgroup': cg}], [8085], 17880, table='tgc_vusa', exists=True)
    assert text.startswith('delete table ip tgc_vusa\n')
    assert 'flush ruleset' not in text and 'skuid' not in text
    with pytest.raises(ValueError):
        render_scoped_redirect([{'cgroup': 'system.slice'}], [8085], 17880, table='tgc_vusa')
    with pytest.raises(ValueError):
        render_scoped_redirect([{'cgroup': cg}], [8085], 17880, table='x; flush ruleset')


def test_actual_exit_is_read_from_verified_tls_response_and_socket_closed():
    class Stream:
        def __init__(self):
            self.data = bytearray(b'HTTP/1.1 200 Connection established\r\n\r\n'
                                  b'HTTP/1.0 200 OK\r\n\r\n198.51.100.9')
            self.closed = False
        def recv(self, n):
            result = bytes(self.data[:n]); del self.data[:n]; return result
        def settimeout(self, n):
            assert n > 0
        def sendall(self, value):
            pass
        def close(self):
            self.closed = True
    stream = Stream()
    with patch('socket.create_connection', return_value=stream):
        with patch('ssl.SSLContext.wrap_socket', return_value=stream) as tls:
            assert probe_exit_ip('http://fixture:fixture-secret@198.51.100.1:8085') == '198.51.100.9'
            assert tls.call_args.kwargs['server_hostname'] == 'api.ipify.org'
    assert stream.closed


def test_exit_probe_failure_does_not_return_credentials_or_disable_proxy():
    with patch('socket.create_connection', side_effect=OSError('private credential in upstream error')):
        assert probe_exit_ip('http://fixture:fixture-secret@198.51.100.1:8085') is None
