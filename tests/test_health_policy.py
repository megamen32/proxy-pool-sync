from proxy_pool_sync import ProxyHealthPolicy, ProxyHealthState


def test_success_resets_failures_and_preserves_import_disable_marker():
    policy=ProxyHealthPolicy(max_failures_before_disable=5)
    state=ProxyHealthState(is_active=False,fail_count=4,last_error='x',disabled_reason='absent_from_import_123',reserved_until=50)
    policy.mark_success(state,now=100)
    assert state.is_active is True
    assert state.fail_count == 0
    assert state.last_error is None
    assert state.disabled_reason == 'absent_from_import_123'
    assert state.reserved_until == 0
    assert state.last_ok == state.last_check == state.last_used == 100


def test_tcp_failure_disables_at_threshold_but_account_failure_can_be_non_counting():
    policy=ProxyHealthPolicy(max_failures_before_disable=2)
    state=ProxyHealthState()
    policy.mark_failure(state,now=1,error='session revoked',counts_toward_disable=False)
    assert state.fail_count == 0 and state.is_active
    policy.mark_failure(state,now=2,error='tcp timeout',counts_toward_disable=True)
    assert state.fail_count == 1 and state.is_active
    policy.mark_failure(state,now=3,error='tcp timeout',counts_toward_disable=True)
    assert state.fail_count == 2 and not state.is_active


def test_candidate_eligibility_and_order_match_rotation_policy():
    policy=ProxyHealthPolicy(max_failures_before_disable=5)
    healthy_old=ProxyHealthState(fail_count=0,last_used=1,last_check=5)
    healthy_new=ProxyHealthState(fail_count=0,last_used=9,last_check=1)
    flaky=ProxyHealthState(fail_count=1,last_used=0,last_check=0)
    busy=ProxyHealthState(uses=1)
    reserved=ProxyHealthState(reserved_until=100)
    assert policy.eligible(healthy_old,now=10)
    assert not policy.eligible(busy,now=10)
    assert not policy.eligible(reserved,now=10)
    ordered=sorted([flaky,healthy_new,healthy_old],key=policy.candidate_sort_key)
    assert ordered == [healthy_old,healthy_new,flaky]


def test_reserve_sets_cooldown_window():
    state=ProxyHealthState()
    ProxyHealthPolicy().reserve(state,now=10,seconds=30)
    assert state.reserved_until == 40


def test_structured_verdict_can_disable_immediately_and_set_cooldown():
    from proxy_pool_sync import FailureKind, FailureVerdict

    policy = ProxyHealthPolicy(max_failures_before_disable=5)
    auth_state = ProxyHealthState()
    policy.mark_verdict(
        auth_state,
        now=10,
        verdict=FailureVerdict(FailureKind.PROXY_AUTH, "407", disable_immediately=True),
    )
    assert not auth_state.is_active
    assert auth_state.disabled_reason == "proxy_auth_failed"

    timeout_state = ProxyHealthState()
    policy.mark_verdict(
        timeout_state,
        now=20,
        verdict=FailureVerdict(FailureKind.DESTINATION_TIMEOUT, "timeout", cooldown_seconds=30),
    )
    assert timeout_state.is_active
    assert timeout_state.fail_count == 1
    assert timeout_state.reserved_until == 50


def test_account_verdict_does_not_poison_proxy_health():
    from proxy_pool_sync import FailureVerdict

    state = ProxyHealthState()
    ProxyHealthPolicy().mark_verdict(state, now=1, verdict=FailureVerdict.account_error("revoked"))
    assert state.fail_count == 0
    assert state.is_active
