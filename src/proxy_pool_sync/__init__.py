"""Public exports load on demand so transport controllers stay lightweight."""
from importlib import import_module

_EXPORTS = {
    'callback_is_authorized': ('auth', 'callback_is_authorized'),
    'FailureKind': ('health', 'FailureKind'),
    'FailureVerdict': ('health', 'FailureVerdict'),
    'ProxyHealthPolicy': ('health', 'ProxyHealthPolicy'),
    'ProxyHealthState': ('health', 'ProxyHealthState'),
    'TunnelCheckResult': ('health', 'TunnelCheckResult'),
    'tcp_tunnel_check': ('health', 'tcp_tunnel_check'),
    'tcp_tunnel_probe': ('health', 'tcp_tunnel_probe'),
    'HealthStore': ('manager', 'HealthStore'),
    'InMemoryHealthStore': ('manager', 'InMemoryHealthStore'),
    'InMemoryLeaseStore': ('manager', 'InMemoryLeaseStore'),
    'LeaseStore': ('manager', 'LeaseStore'),
    'ProxyLease': ('manager', 'ProxyLease'),
    'ProxyPoolManager': ('manager', 'ProxyPoolManager'),
    'ProxySyncResult': ('manager', 'ProxySyncResult'),
    'NotificationResult': ('notifications', 'NotificationResult'),
    'NotificationTarget': ('notifications', 'NotificationTarget'),
    'normalize_notification_targets': ('notifications', 'normalize_notification_targets'),
    'notify_targets': ('notifications', 'notify_targets'),
    'FileProxyPool': ('pool', 'FileProxyPool'),
    'PoolUpdatePolicy': ('pool', 'PoolUpdatePolicy'),
    'normalize_proxy_line': ('pool', 'normalize_proxy_line'),
    'parse_proxy_export': ('pool', 'parse_proxy_export'),
    'sanitize_proxy_lines': ('pool', 'sanitize_proxy_lines'),
    'HttpProxySource': ('source', 'HttpProxySource'),
    'BindingEvent': ('bindings', 'BindingEvent'),
    'BindingJournal': ('bindings', 'BindingJournal'),
    'may_apply_preference': ('bindings', 'may_apply_preference'),
    'LocalForward': ('transport', 'LocalForward'),
    'public_proxy_ref': ('transport', 'public_proxy_ref'),
    'render_scoped_redirect': ('transport', 'render_scoped_redirect'),
    'render_ssh_forwards': ('transport', 'render_ssh_forwards'),
    'write_private_text': ('storage', 'write_private_text'),
}
__all__ = list(_EXPORTS)


def __getattr__(name: str):
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    value = getattr(import_module('.' + module, __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
