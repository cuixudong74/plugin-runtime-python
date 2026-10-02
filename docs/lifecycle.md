# Lifecycle Semantics

This document describes the plugin lifecycle states, the enable/disable/
upgrade flow, draining, failure isolation, and concurrency guarantees.

## States

| State        | Description                                              |
|--------------|----------------------------------------------------------|
| `installed`  | The manifest is stored but the plugin is not serving.   |
| `enabled`    | The plugin is active and serving requests.              |
| `disabled`   | The plugin was explicitly disabled (drained).           |
| `isolated`   | The plugin reached the failure threshold and is not serving. |

## Enable

1. The operator requests `enable`.
2. The runtime resolves a consistent version set over all enabled plugins
   plus the new one.
3. The selected versions are **loaded** (entrypoint imported and called).
   Loading happens *before* any state change, so a load failure leaves the
   current serving set untouched.
4. The active-version pointer is **swapped atomically**.  From this instant,
   every new request is served by exactly one complete version.
5. Displaced old versions are **drained** (see below).

Enable is **idempotent**: enabling an already-enabled plugin is a no-op.

## Disable

1. The operator requests `disable`.
2. The runtime resolves the enabled set *without* the disabled plugin.  If
   another enabled plugin requires it, resolution fails and the disable is
   rejected.
3. The plugin is removed from the active set and drained.

## Upgrade

1. The operator provides a new manifest (and code).
2. The new version is installed.
3. The runtime resolves over the enabled set, **pinning** the upgraded plugin
   to the new version.  If the new version cannot be part of a consistent set,
   the upgrade is rejected.
4. The new version is loaded and the active pointer is swapped atomically.
5. The old version is drained.

Upgrade is **idempotent** for the same version: re-installing the same
manifest is a no-op.

## Draining

When a plugin version is displaced (by disable or upgrade), it enters
**draining** mode:

- No *new* requests are routed to it.
- *In-flight* requests (already being processed) are allowed to finish.
- The runtime waits for all in-flight requests to complete, up to a
  configurable timeout (`drain_timeout`, default 30 seconds).

If the drain timeout expires, the swap is **rolled back**: the old version is
reactivated and the new version is deactivated.  The operation fails with
`DrainTimeoutError`.

Drain waits are **async and cancellable**: they do not block the event loop,
and cancelling the operation during drain triggers a rollback.

## Failure Isolation

Each plugin has a **consecutive failure counter**:

- A successful invocation resets the counter to zero.
- A failed invocation (or load) increments the counter.
- When the counter reaches `failure_threshold` (default 5), the plugin is
  **isolated**: it stops serving and its state becomes `isolated`.

Isolation is a **safety valve**: a single misbehaving plugin cannot affect
other plugins or the host.  Isolation state, the last failure reason, and a
failure history are queryable via `GET /plugins/{id}/isolation`.

An operator can **recover** an isolated plugin via `POST /plugins/{id}/recover`.
Recovery clears the failure counter and re-enables the plugin (through the
normal resolution and loading flow).

## Concurrency

All lifecycle mutations (enable, disable, upgrade, uninstall, recover) are
**serialized** by a single `asyncio.Lock`.  This guarantees:

- **Convergence**: concurrent requests to the same plugin produce a
  deterministic result.
- **No duplicate installs**: duplicate requests do not create duplicate
  versions or state drift.
- **Atomicity**: the active-version pointer is swapped atomically, so a
  request never sees a mix of old and new versions.
- **Cancellation safety**: if an operation is cancelled (or times out) before
  the swap, no state changes.  If cancelled during drain, the swap is rolled
  back.

## Capability Declaration and Audit

A plugin must **declare** a capability in its manifest's `requires` list
before calling it via `ctx.call()`.  Undeclared calls are:

1. **Rejected** with `UndeclaredCapabilityError` (propagated to the caller).
2. **Recorded** in the audit log (never silently ignored).

The audit log records every capability call (allowed and rejected) with the
caller, capability, provider, timestamp, and reason.  It is queryable via
`GET /audit` and `GET /audit/rejections`.
