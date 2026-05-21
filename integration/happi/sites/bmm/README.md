# Site sandbox — NSLS-II BMM beamline (6BM)

Happi device DB + ophyd class shim rendered from
[`bmm-profile-collection`](https://github.com/NSLS-II/bmm-profile-collection)
(local clone at `/home/asligar/git_projects/bmm-profile-collection`) and
[`bmm_tools`](https://github.com/NSLS2/bmm_tools)
(local clone at `/home/asligar/git_projects/bmm_tools`).

This directory is **site-specific** — it bakes in NSLS-II BMM PV naming
(`XF:06BM*`, `XF:06BMA*`, `XF:06BMB*`, `FE:C06B*`) and references
NIST/NSLS-II detector hardware (Xspress3, Pilatus 100k, Eiger 1M, Dante
7-element SDD). Keep it under `sites/<name>/` so the top-level
`integration/happi/happi_db.json` stays facility-neutral
(community-not-NSLS-II).

Intended consumer: `configuration_service`. Load via:

```
CONFIG_LOAD_STRATEGY=happi
CONFIG_PROFILE_PATH=/path/to/integration/happi/sites/bmm
```

The loader auto-discovers `happi_db.json` under `CONFIG_PROFILE_PATH`.

## Files

| File | Purpose |
|---|---|
| `happi_db.json` | 124 happi entries — every top-level device variable in `bmm-profile-collection/startup/BMM/user_ns/*.py` |
| `bmm_devs.py` | Vanilla-ophyd port of every custom class referenced by the JSON |
| `bmm_tst3.py` | XRT physical/optical model of BMM (separate concern; not part of the happi registry — see "XRT simulation (future)" below) |
| `README.md` | this file |

## Validation status (2026-05-18)

**Shape-inductive only.** Validated against the profile-collection source;
not yet cross-checked against live IOCs. The port pattern follows the
IOS sandbox in `../ios/` which *was* live-IOC validated and confirmed
the AST-walk + 1:1 Component-port approach holds end-to-end.

Verification when live IOCs become accessible should focus on:

- DCM compound motor PVs (`XF:06BMA-OP{Mono:DCM1-Ax:Bragg}Mtr` etc.)
- 5-jack mirror motors (m1 / m2 / m3 — each with yu/ydo/ydi/xu/xd; m2 also has a bender on a separate prefix)
- Xspress3 detector — 3 element-count variants (1 / 4 / 7) built dynamically via `nslsii.areadetector.xspress3.build_xspress3_class`
- Dante SDD MCAs (mca1..mca7; channel 8 disabled per profile)
- Wheel motor (`xafs_wheel`/`xafs_rotb`/`xafs_ref`) — 48 slots, 2 rings
- LakeShore 331 + Linkam T96 (RS232 via Moxa terminal server)

## How the inventory was built

Same AST-walk strategy as IOS: parse every `.py` file under
`bmm-profile-collection/startup/BMM/user_ns/`, recurse through `if/try/with`
branches at module scope (since BMM gates detector instantiation behind
`if with_pilatus is True:` style flags), and emit every top-level
`name = Cls(prefix_arg, ..., name=...)` assignment whose right-hand side is
a known device class or one of the BMM factory functions
(`define_XAFSEpicsMotor`, `define_EndStationEpicsMotor`,
`define_EncodedEndStationEpicsMotor`, `define_EpicsMotor`) which return
either the concrete class or a `SynAxis` fallback.

Classes that *look* like devices but are plain Python helpers were filtered
out: `BMM_User` (Borg singleton), `BMMTelemetry` / `BMMDataEvaluation`
(plain classes), `Wafer` / `DetectorMount` (geometry helpers), and the
six `*MacroBuilder` classes (plan helpers, not ophyd Devices).

The extractor isn't committed here — re-run it from `/tmp/bmm_extract.py`
or rebuild from the IOS one. Manual cross-check confirmed agreement.

## Device inventory

**124** happi entries by class:

| Class | Count | Notes |
|---|---:|---|
| `bmm_devs.XAFSEpicsMotor` | 31 | DCM x/y, mirror jacks, dm1/dm2/dm3, xafs_ref/x/y/refx/refy/rots/garot/roll/pitch/dety/detz/spare/bsy/bsx, slits3 blades |
| `bmm_devs.EndStationEpicsMotor` | 14 | XAFS table jacks, table detx, additional MC09 stages |
| `ophyd.EpicsSignalRO` | 10 | FE slit sizes/centers, `m1_xafs` etc. read-only beam-position signals |
| `ophyd.EpicsMotor` | 8 | FE slits (raw EpicsMotor, no FMBO subtype) |
| `bmm_devs.EncodedEndStationEpicsMotor` | 7 | MC09 encoded XAFS axes |
| `bmm_devs.BMMUVCSingleTrigger` | 5 | USB cameras (usb1/usb2/cam7/cam8/cam9) |
| `bmm_devs.OneWireTC` | 4 | DI-water and one-wire thermocouples |
| `bmm_devs.EPS_Shutter` | 4 | sha, shb, fs1, ln2 |
| `bmm_devs.WheelMotor` | 3 | `xafs_wheel`/`xafs_rotb`/`xafs_ref` |
| `bmm_devs.StandardSlits` | 3 | sl (slits3) + slits2 + dm2_slits compound |
| `bmm_devs.Mirrors` | 3 | m1 / m2 / m3 |
| `bmm_devs.IntegratedIC` | 3 | ic0 / ic1 / ic2 (BMMDualEM subclass) |
| `bmm_devs.XAFSTable` | 2 | xt / xafs_table |
| `ophyd.sim.SynAxis` | 2 | `xafs_xu` / `xafs_xd` (intentionally stubbed in profile) |
| `bmm_devs.FEBPM` | 2 | bpm_upstream / bpm_downstream |
| `bmm_devs.AxisCaprotoCam` | 2 | xascam / xrdcam (caproto-IOC fronted cameras) |
| `bmm_devs.{DCM,GonioSlits,GlancingAngle,Linkam,LakeShore,KillSwitch,FEVac,TCG,Busy,BMM_DIWater,BMMSnapshot}` | 1 each | singleton compound devices |
| `bmm_devs.{BMMQuadEM,IDPS_Shutter,BMPS_Shutter}` | 1 each | front-end / electrometer |
| `bmm_devs.BMMXspress3Detector_{1,4,7}Element` | 1 each | xs1 / xs4 / xs7 — nslsii dynamic-class via `build_xspress3_class` |
| `bmm_devs.BMMPilatusSingleTrigger` | 1 | pilatus |
| `bmm_devs.BMMEigerSingleTrigger` | 1 | eiger |
| `bmm_devs.BMMDanteSingleTrigger` | 1 | dante (7-channel) |
| `bmm_devs.LockedDwellTimes` | 1 | `_locked_dwell_time` (dwti) — pseudo-positioner syncing dwell-time across all detectors |

### Profile quirks preserved

- Some python variables are aliases: `xafs_detx = xafs_det = EndStationEpicsMotor(...)`. Each alias gets its own happi entry.
- `xafs_xu` / `xafs_xd` are intentionally stubbed as `SynAxis` in the profile (commented-out `EndStationEpicsMotor` lines). The happi entries reflect the SynAxis form — they are simulator devices, not EPICS-backed.
- `sl` is the python variable but its `name=` kwarg is `slits3`. Happi `_id` follows the python variable; `name` kwarg is preserved in the kwargs dict.
- `_locked_dwell_time` takes an empty-string prefix and the device-level `name` kwarg is `dwti`. The pseudo-positioner's Components carry their own absolute PVs.

### Detector classes (BMM-internal, nslsii-dependent)

- **BMMXspress3Detector_1/4/7Element**: dynamically built via
  `nslsii.areadetector.xspress3.build_xspress3_class()` (BMM's profile
  does this in `BMM/xspress3_{1,4,7}element.py`). The shim mirrors the
  same call with identical args (channel_numbers tuple, `mcaroi_numbers=range(1, 21)`,
  `image_data_key="xrf"`). The import is `try/except ImportError`-guarded
  so `bmm_devs.py` still loads in environments without `nslsii` — the
  affected classes are `None` and the happi entries fail loudly on
  instantiation.
- **BMMPilatusSingleTrigger / BMMUVCSingleTrigger / BMMEigerSingleTrigger / BMMDanteSingleTrigger**: subclass `nslsii.ad33.SingleTriggerV33` mixin; same try/except-guard pattern.

### Custom classes in `bmm_devs.py`

Ported 1:1 from the profile / bmm_tools, vanilla-ophyd only (no bluesky /
kafka / databroker / pyOlog / amostra / redis / nslsii — except the
xspress3 and SingleTriggerV33 imports, which are guarded):

| From | Class(es) |
|---|---|
| `bmm_tools/devices/motors.py` | `DeadbandMixin`, `DeadbandEpicsMotor`, `EpicsMotorWithDial`, `FMBOEpicsMotor`, `FMBOThinEpicsMotor`, `XAFSEpicsMotor`, `BMMDeadBandMotor`, `VacuumEpicsMotor`, `EndStationEpicsMotor`, `EncodedEndStationEpicsMotor`, `Mirrors`, `XAFSTable` |
| `bmm_tools/devices/slits.py` | `StandardSlits`, `GonioSlits` |
| `bmm_tools/devices/actuators.py` | `EPS_Shutter`, `BMPS_Shutter`, `IDPS_Shutter` |
| `bmm_tools/devices/frontend.py` | `FEBPM` |
| `bmm_tools/devices/busy.py` | `Busy`, `BusyStatus` |
| `bmm_tools/devices/utilities.py` | `TCG`, `FEVac`, `OneWireTC`, `BMM_DIWater` |
| `bmm_tools/devices/dcm.py` | `DCM` |
| `bmm_tools/devices/usb_camera.py` | `BMMUVC`, `BMMUVCSingleTrigger`, `BMMFileStoreJPEG`, `BMMJPEGPlugin` |
| `bmm_tools/devices/axis_webcam.py` | `AxisCaprotoCam`, `ExternalFileReference` |
| `bmm_tools/devices/pilatus.py` | `BMMFileStoreHDF5`, `BMMHDF5Plugin`, `BMMPilatus`, `BMMPilatusSingleTrigger` |
| `bmm_tools/tools/killswitch.py` | `KillSwitch` |
| `BMM/electrometer.py` | `Nanoize`, `BMMQuadEM`, `BMMDualEM`, `IntegratedIC` |
| `BMM/lakeshore.py` | `LSatSetPoint`, `LakeShore` |
| `BMM/linkam.py` | `AtSetpoint`, `Linkam` |
| `BMM/glancing_angle.py` | `GlancingAngle` |
| `BMM/camera_device.py` | `BMMSnapshot` |
| `BMM/wheel.py` | `WheelMotor` |
| `BMM/eiger.py` | `BMMEiger`, `BMMEigerSingleTrigger` |
| `BMM/dante.py` | `DanteCamBase`, `BMMDanteFileStoreHDF5`, `BMMDanteHDF5Plugin`, `BMMDante`, `BMMDanteSingleTrigger` |
| `BMM/xspress3*.py` (dynamic) | `BMMXspress3DetectorBase`, `BMMXspress3Detector_1Element`, `BMMXspress3Detector_4Element`, `BMMXspress3Detector_7Element` |
| `BMM/dwelltime.py` | `QuadEMDwellTime`, `StruckDwellTime`, `IC0DwellTime`, `IC1DwellTime`, `IC2DwellTime`, `Xspress3DwellTime`, `PilatusDwellTime`, `EigerDwellTime`, `DanteDwellTime`, `LockedDwellTimes` |

### What was intentionally omitted

The shim describes **device structure** (Components + base-class chain),
not runtime behavior. The following were dropped:

- Plan-only methods (`open_plan`, `close_plan`, `dark_current`, `to`,
  `auto_align`, `measure_xrf`, `plot`, `recover`, etc.) — these require
  bluesky's `mv` / `sleep` / `mvr` and a RunEngine.
- Display helpers (`status`, `wh`, `where`, `_state`, `dossier_entry`) —
  these use `rich.print` / `boxedtext` and produce text output, not
  ophyd protocol.
- The `*MacroBuilder` classes (plan-generation helpers).
- The profile-runtime globals (`PROPOSALS`, `md['cycle']`,
  `md['data_session']`, `BMMuser`, `user_ns`, `kafka`, `rkvs`, redis
  connection objects).

Filestore plugin path templates were replaced with placeholder paths
under `/tmp/bmm/assets/`. Override at deployment if you actually trigger
acquisition.

## XRT simulation (future)

`bmm_tst3.py` is the **physical/optical** model of BMM authored in
[xrt](https://github.com/kklmn/xrt) — describing the wiggler source,
mirrors (M1_VCM / M2_TFM / M3_HRM), DCM, slits, and sample positions in
3D space. It is **not** part of the happi/ophyd device registry. The
intent is to eventually drive an XRT-backed simulation that produces
synthetic data through the same configuration_service / direct_control
surface as the real beamline. That work is a separate task from this
device-registry port.
