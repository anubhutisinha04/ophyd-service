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

**Ephemeral state**: Locks are in-memory only (`DeviceLockManager`). On service restart, all locks are cleared. This is intentional — a restart means no plan is running, so no locks should exist.

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
