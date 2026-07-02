# Device Locking

## The problem

Two services can command hardware: Experiment Execution (runs Bluesky plans) and Direct Control (manual PV writes from a UI). If both try to move the same motor simultaneously, the results are undefined and potentially dangerous.

## The solution

Before a Bluesky plan starts, Experiment Execution acquires locks on all devices the plan will use. While locked, Direct Control checks each PV's status before every write and refuses to command locked devices. When the plan finishes, Experiment Execution releases the locks.

The Configuration Service holds the lock state because it is the shared registry that both services already query. It does not enforce locks — it is a coordination point. Enforcement happens on the consumer side.

## Lock semantics

**All-or-nothing acquisition**: A lock request names multiple devices. Either all are locked or none are. If any device is already locked, not found, or disabled, the entire request fails and no locks are acquired.

**Owner-only release**: Locks are keyed by `item_id` (the queue item running the plan). Only that `item_id` can release the lock. This prevents one plan from accidentally releasing another plan's locks.

**Force-unlock**: An admin endpoint that clears locks regardless of ownership. Used when Experiment Execution crashes mid-plan and leaves orphaned locks.

**Ephemeral state**: Locks are in-memory only (`DeviceLockManager`). On service restart, all locks are cleared. A restart usually means no plan is running — but it can also happen *mid-plan*, which would silently drop a running plan's locks. Two mechanisms bound that exposure: **lock leases** and the **lock-authority epoch** (below).

## Two lock variants

There are two operator-facing ways to answer "what does a running plan lock?":

1. **Lock everything (Variant 1)** — while *any* plan is running, *no* device is commandable out of band, even devices the plan doesn't touch. Enabled by the **lock_all** policy below.
2. **Lock only what's used (Variant 2, default)** — a plan using devices A and B leaves C and D available to Direct Control.

Both are *plan-scoped*: queueserver acquires the lock when a plan starts and releases it when the plan ends, so an idle environment leaves every device free. (Queueserver's `lock_scope` defaults to `plan`; an alternative `environment` scope locks the whole device set for the environment's lifetime, including while idle.)

## The lock_all policy

The **lock_all** availability policy selects Variant 1. When enabled, the moment
any lock is held, every registered device reports locked/unavailable, with
`locked_by_plan` attributed to the plan holding the (earliest) lock.
Acquisition and release semantics are unchanged — this is purely a change in
how availability is derived from lock state, so the lock writer (queueserver
/ Experiment Execution) needs no changes: it still acquires only the plan's
devices, and the policy widens availability.

Configuration:

- `CONFIG_LOCK_ALL` (default `false`) — boot default.
- `GET /api/v1/devices/lock/policy` / `PUT /api/v1/devices/lock/policy`
  with `{"lock_all": true|false}` — read or change at runtime. Like the
  locks themselves, the runtime value is in-memory; a restart returns to
  the boot default.

Standalone PVs are not affected (see below) — they have no device-level
lock concept.

## Lock leases and heartbeat

By default locks are held until explicitly released (or force-unlocked). A lock
holder that crashes without releasing therefore blocks its devices until an
admin force-unlocks or the service restarts. **Lock leases** bound that: when
`CONFIG_LOCK_LEASE_TTL_SECONDS` > 0, every acquired lock carries an `expires_at`
and lapses if not renewed. The holder must heartbeat via
`POST /api/v1/devices/lock/renew` before the lease elapses; a crashed holder's
lock then self-heals after at most the TTL.

Leases default to disabled (`0`) because they are only safe when the holder is
heartbeat-capable — otherwise a long plan would lose its lock mid-run.
Queueserver's coordinator renews on a timer (~1/3 of the TTL) and re-acquires
if a renew reports the lock lost.

## The lock-authority epoch

Because lock state is in-memory, a configuration_service restart rebuilds an
empty lock table. The DB-backed registry `service_epoch` does *not* change on a
plain restart, so it cannot signal this. The **lock-authority epoch** — a fresh
id generated every process start — does: it is returned on lock / unlock /
renew responses and on `GET /.../status` as `lock_epoch`. A holder that sees the
epoch change re-acquires its locks; a reader (Direct Control) that sees it
change logs that the lock table was reset (every device briefly reports
available until holders re-acquire).

## PV-level resolution

Locks are stored at the device level. Direct Control operates at the PV level. The `GET /api/v1/pvs/status` endpoint bridges this: given a PV name, it resolves to the owning device and returns the device's lock and enabled state.

Standalone PVs (not bound to any device) are always available — they cannot be locked.

## Lock lifecycle

```
Experiment Execution                Configuration Service
        │                                    │
        │  POST /devices/lock                │
        │  {devices: [A, B], item_id: X}     │
        │ ─────────────────────────────────► │
        │                                    │  validate all devices exist, are enabled, are unlocked
        │  200 {locked_devices: [A, B]}      │  acquire locks atomically
        │ ◄───────────────────────────────── │  write audit log
        │                                    │
        │  ... plan runs ...                 │
        │                                    │
        │  POST /devices/unlock              │
        │  {devices: [A, B], item_id: X}     │
        │ ─────────────────────────────────► │
        │                                    │  verify item_id matches
        │  200 {unlocked_devices: [A, B]}    │  release locks
        │ ◄───────────────────────────────── │  write audit log
```

Meanwhile, Direct Control checks before each PV write:

```
Direct Control                       Configuration Service
        │                                    │
        │  GET /pvs/status?pv_name=A:RBV     │
        │ ─────────────────────────────────► │
        │                                    │  resolve PV → device A → lock state
        │  200 {available: false,            │
        │       locked_by_plan: "rel_scan"}  │
        │ ◄───────────────────────────────── │
        │                                    │
        │  (refuses to write)                │
```

## Version counter

The lock manager maintains a monotonic `version` counter incremented on every lock or unlock operation. This is returned in lock/unlock responses as `registry_version`. Clients can use it to detect stale lock state without polling every device individually.

## Audit log

Lock, unlock, and force-unlock events are written to the `device_audit_log` table with JSON details (plan name, item ID, service name, reason). This provides a forensic trail when diagnosing lock-related issues.

## What locking does not do

- It does not prevent CRUD operations on devices. The Configuration Service accepts device changes at any time.
- It does not enforce locks — enforcement is the consumer's responsibility.
- It does not persist locks across restarts.
- It does not implement timeouts or TTLs. Locks are held until explicitly released or force-unlocked.
