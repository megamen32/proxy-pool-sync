# Shared transport and weak proxy binding exchange

Status: implementation and consumer integration in progress.

User decision: reuse one proxy exit for the same native Telegram identity where possible. Exchange assignment changes through authenticated APIs; verify an owned proxy and apply only at the next idle native connection. Keep credentials, session identity, account ownership and capacity policies local.

Implemented: common SSH forward and scoped redirect renderers, private atomic writer, bounded exit probe, durable idempotent binding journal/outbox, authenticated metadata API and checked next-connection preference helper. Database writes and native connection locks remain application responsibilities.

Integration blocker: TGC canonical working main contains unpublished work belonging to another owner. Its native consumer hook must be integrated with that owner; API acceptance alone is not proof of native rebind. No foreign work is removed or published.
