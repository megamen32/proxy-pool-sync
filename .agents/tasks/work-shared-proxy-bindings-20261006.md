# Shared transport and weak proxy binding exchange

Status: implementation and consumer integration in progress.

User decision: reuse one proxy exit for the same native Telegram identity where possible. Exchange assignment changes through authenticated APIs; verify an owned proxy and apply only at the next idle native connection. Keep credentials, session identity, account ownership and capacity policies local.

Implemented: common SSH forward and scoped redirect renderers, private atomic writer, bounded exit probe, durable idempotent binding journal/outbox, authenticated metadata API and checked next-connection preference helper. Database writes and native connection locks remain application responsibilities.

Current remaining work: connect the checked-preference helper to native account-locked connection factories and verify a coordinated application release. The earlier unpublished TGC ancestry has since been reconciled by its owner. API acceptance alone is not proof of native rebind. Foreign dependency and cabinet work stays preserved.

Shared-library source tests: 50 passed. Application-independent hop controllers use the shared transport renderer; the metadata exchange runs with separate read-only native adapters. Live native rebind is not claimed.
