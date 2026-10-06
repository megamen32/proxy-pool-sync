import asyncio
from types import SimpleNamespace

from proxy_pool_sync.binding_consumer import checked_preference
from proxy_pool_sync.bindings import BindingJournal


def test_busy_connection_defers_without_api_or_probe(tmp_path):
    calls = []
    peer = SimpleNamespace(preferred=lambda _: calls.append('api'))
    result = asyncio.run(checked_preference(peer, 999, [], scope='operator', expected_source='autosell', idle=False))
    assert result is None and calls == []


def test_free_connection_verifies_owned_credentials_and_native_identity(tmp_path):
    journal = BindingJournal(tmp_path / 'a.db', scope='operator', source='autosell')
    event = journal.observe(999, 'http://198.51.100.1:8080', '198.51.100.1')
    peer = SimpleNamespace(preferred=lambda identity: event if identity == 999 else None)
    owned = 'http://local:secret@198.51.100.1:8085'
    calls = []
    def probe(url):
        calls.append(url)
        return '198.51.100.1'
    async def run(identity=999):
        return await checked_preference(peer, identity, [owned], scope='operator',
                                        expected_source='autosell', idle=True, probe=probe)
    assert asyncio.run(run()) == owned
    assert calls == [owned]
    assert asyncio.run(run(42)) is None


def test_failed_or_different_exit_and_other_scope_preserve_assignment(tmp_path):
    journal = BindingJournal(tmp_path / 'a.db', scope='operator', source='autosell')
    event = journal.observe(999, 'http://198.51.100.1:8080', '198.51.100.1')
    peer = SimpleNamespace(preferred=lambda _: event)
    for value in [None, '198.51.100.2']:
        result = asyncio.run(checked_preference(peer, 999, ['http://198.51.100.1:8085'],
            scope='operator', expected_source='autosell', idle=True, probe=lambda _: value))
        assert result is None
    result = asyncio.run(checked_preference(peer, 999, ['http://198.51.100.1:8085'],
        scope='other', expected_source='autosell', idle=True, probe=lambda _: '198.51.100.1'))
    assert result is None
