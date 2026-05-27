# role_families fixture pack

PV-shape fixtures modeled on representative device roles from the
[`nsls2.ioc_deploy`](https://github.com/NSLS-II/ioc_deploy) ansible
catalog, mapped to upstream `ophyd` primitives only. The goal is
realistic compound-device coverage for `_walk_class_for_pvs` (and the
direct-control PV-validation gate that depends on its output) beyond
the small synthetic Device subclasses defined inline in `test_loader.py`.

## What's here

| Entry | Source role | Device shape |
|---|---|---|
| `sim_detector`     | `adsimdetector`  | AreaDetector camera + HDF5 plugin |
| `sim_motor`        | `motorsim`       | Top-level `ophyd.EpicsMotor` (4-key shortcut path) |
| `dual_axis_stage`  | `axis_caproto`   | Multi-axis stage with `EpicsMotor` sub-components (walker path) |
| `quad_em`          | `nsls2em`        | Quad electrometer: 4 channels × 4 stats each |
| `temp_controller`  | `lakeshore336`   | Multi-channel temperature controller: 4 inputs × 3 PVs each |
| `soft_ioc_base`    | `base_soft_ioc`  | Soft-IOC heartbeat/uptime baseline |

## What it deliberately is NOT

* **Not NSLS-II.** The Device class shims live entirely in upstream
  ophyd (no `nslsii`, no beamline-profile imports). Prefixes are
  sanitized to generic `SIM:<FAMILY>:01:` patterns — no `XF:…{…}`
  conventions, no facility names. Per `feedback_community_not_nsls`.
* **Not exhaustive.** One representative role per device family. The
  source catalog has 64 roles; consuming the rest is a follow-on.
* **Not a runtime seed.** These fixtures live under `tests/` and are
  loaded only by `test_loader_role_fixtures.py`. The community-facing
  runtime seed (`integration/happi/happi_db.json`) is mini_beamline-only
  by design — see `project_demo_happi_seed`.

## Adding a new family

1. Pick a role from `nsls2.ioc_deploy/roles/device_roles/`. Skim its
   `schema.yml` + `example.yml` (or `examples/*/config.yml`).
2. Add a `Device` subclass to `role_classes.py` mirroring the PV shape
   with upstream ophyd primitives. Sanitize all prefixes — no `XF:…`,
   no `{…}` braces unless you're deliberately exercising the
   `_has_format_placeholder` false-positive case.
3. Add a happi entry to `role_families.json` pointing at the new class
   path with a `SIM:…` prefix.
4. Extend the parametrized test in
   `tests/test_loader_role_fixtures.py` with the expected dotted-key
   set.
