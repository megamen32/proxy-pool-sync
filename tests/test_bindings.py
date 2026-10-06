import json
from pathlib import Path
import tempfile

import pytest

from proxy_pool_sync.bindings import BindingEvent, BindingJournal, may_apply_preference


def pair(tmp_path):
    return (
        BindingJournal(tmp_path / 'a/state.db', scope='operator', source='autosell'),
        BindingJournal(tmp_path / 'b/state.db', scope='operator', source='tgc'),
    )


def test_peer_retry_deduplicates_and_survives_restart(tmp_path):
    a, b = pair(tmp_path)
    event = a.observe(123456, 'http://name:private-password@198.51.100.1:8085', '198.51.100.1')
    assert 'private-password' not in event.encode()
    assert b.receive(event, allowed_source='autosell') == 'accepted'
    b = BindingJournal(tmp_path / 'b/state.db', scope='operator', source='tgc')
    assert b.receive(event, allowed_source='autosell') == 'duplicate'
    assert b.preferred(123456) == event
    assert len(a.pending()) == 1
    a.acknowledge(event.event_id)
    assert a.pending() == []


def test_applying_peer_exit_through_different_frontend_does_not_echo(tmp_path):
    a, b = pair(tmp_path)
    event = a.observe(999, 'http://198.51.100.1:8080', '198.51.100.1')
    b.receive(event, allowed_source='autosell')
    assert b.observe(999, 'http://198.51.100.1:8085', '198.51.100.1') is None
    assert b.pending() == []


def test_reordered_delivery_preserves_newest_working_preference(tmp_path):
    a, b = pair(tmp_path)
    first = a.observe(999, 'http://198.51.100.1:8080')
    second = a.observe(999, 'http://198.51.100.2:8080')
    b.receive(second, allowed_source='autosell')
    assert b.receive(first, allowed_source='autosell') == 'stale'
    assert b.preferred(999) == second


def test_concurrent_origin_tie_is_deterministic_without_shared_db(tmp_path):
    a, b = pair(tmp_path)
    one = a.observe(999, 'http://198.51.100.1:8080')
    two = b.observe(999, 'http://198.51.100.2:8080')
    a.receive(two, allowed_source='tgc')
    b.receive(one, allowed_source='autosell')
    assert a.preferred(999) == b.preferred(999) == two


def test_local_row_id_is_not_used_for_other_telegram_identity(tmp_path):
    a, b = pair(tmp_path)
    event = a.observe(999, 'http://198.51.100.1:8080')
    b.receive(event, allowed_source='autosell')
    assert b.preferred(42) is None


def test_busy_or_unverified_or_occupied_exit_never_rebinds():
    assert may_apply_preference(same_account=True, idle=True, checked_working=True, capacity_available=True)
    for bad in ['same_account', 'idle', 'checked_working', 'capacity_available']:
        flags = dict(same_account=True, idle=True, checked_working=True, capacity_available=True)
        flags[bad] = False
        assert not may_apply_preference(**flags)


def test_incoming_wire_ref_cannot_include_credentials(tmp_path):
    a, _ = pair(tmp_path)
    event = a.observe(999, 'http://198.51.100.1:8080')
    body = json.loads(event.encode())
    body['proxy_ref'] = 'http://name:password@198.51.100.1:8080'
    with pytest.raises(ValueError, match='Credential-free'):
        BindingEvent.decode(json.dumps(body))


def test_journal_is_private_and_cannot_be_reused_for_other_project(tmp_path):
    a, _ = pair(tmp_path)
    assert a.path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match='different service'):
        BindingJournal(a.path, scope='operator', source='tgc')


def test_untrusted_peer_does_not_change_preference(tmp_path):
    a, b = pair(tmp_path)
    event = a.observe(999, 'http://198.51.100.1:8080')
    with pytest.raises(ValueError, match='Untrusted'):
        b.receive(event, allowed_source='unknown')
    assert b.preferred(999) is None


def test_temporary_failed_exit_probe_does_not_publish_a_binding_change(tmp_path):
    a, _ = pair(tmp_path)
    event = a.observe(999, 'http://198.51.100.1:8080', '198.51.100.1')
    assert a.observe(999, 'http://198.51.100.1:8080', None) is None
    assert a.preferred(999) == event


def test_offline_peer_coalesces_obsolete_changes_per_account(tmp_path):
    a, _ = pair(tmp_path)
    for number in range(1, 20):
        latest = a.observe(999, f'http://198.51.100.{number}:8080')
    assert a.pending() == [latest]
