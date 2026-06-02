# ophyd-service ↔ N3XTware mapping

This document maps the requirements of the NSLS-II N3XTware project (specifically
Deliverable 1, *Configuration and Management of Devices*) onto the components
of ophyd-service, so the two efforts converge rather than drift.

The framing: **N3XTware describes a need**; **ophyd-service is intended as
part of the solution**. Decisions about scope, schemas, and deployment shape
in this repo should be informed by where N3XTware is going.

## Source documents

The authoritative N3XTware references are:

- *Final Design Review*: `n3xtware-docs/fdr/final-design-review.md`
  (currently a skeleton; only the project-overview section is filled in)
- *Deliverable 1 PDR — Leveraging Infrastructure as Code for Deployment of
  Controls and Orchestration Software*:
  `n3xtware-docs/pdr/configuration-and-management-of-devices.md`
  (dated 2025-09-02; the substantive content this document maps against)

Where this document refers to "PDR §…" or to requirement IDs
(`FR-…`, `NFR-…`, `SEC-…`), the IDs come from the PDR's traceability matrix.

## Scope of this mapping

N3XTware has five deliverables. Only Deliverable 1 directly overlaps with
ophyd-service. Deliverable 2 (*Data Reduction and Analysis Services*) is
adjacent — its services would be a downstream consumer of ophyd-service —
but is out of scope here.

Within Deliverable 1, this mapping covers:

- **configuration_service** (`backend/configuration_service/`) — the
  device-and-PV registry backend.
- **direct_control_service** (`backend/direct_control_service/`) —
  the runtime device-control surface, including the device-monitor
  components under `direct_control/monitoring/`.
- The shared compose / pods / shared-schema artifacts at the repo root.

The ophyd-service frontend (`frontend/`) is owned externally and is
referenced here only where it relates to N3XTware's UI requirements.

## Mapping intent and standing guardrails

Three principles shape every entry below:

1. **Convergence, not divergence.** Where N3XTware names a capability, the
   solution shape in this repo should match what N3XTware will deploy. New
   features should pass through a check: does an N3XTware requirement
   already describe this capability? If yes, conform. If no, document the
   extension point so an N3XTware-flavored deployment can configure it.
2. **Community-neutral codebase.** ophyd-service targets any Bluesky
   facility. NSLS-II- or NEXT-III-specific values (hostnames, VLANs,
   identity providers, site packages, site device catalogs) belong in a
   deployment-time overlay — Ansible role variables, site-private repos,
   environment injection — not in this codebase. Convergence with N3XTware
   shapes the *extension points*, not their contents.
3. **Fail hard, no silent fallbacks.** Configuration and registry behavior
   should surface misconfiguration as an error rather than mask it with
   defaults. This is a project-wide stance and intersects N3XTware's
   reliability and accuracy NFRs.

## Requirement mapping

The status column reflects ophyd-service's coverage of the requirement, not
the PDR's own status field.

| Req ID | Requirement (paraphrased) | Where it lives in ophyd-service | Coverage | Gap to close |
| --- | --- | --- | --- | --- |
| FR-001 | Single reference definitions for mature IOCs | Out of scope (IOC code lives in NSLS-II Ansible/Galaxy collections, not here) | n/a | Document that ophyd-service consumes IOC PVs but does not define them |
| FR-002 | Single reference definitions for engineering screens | Out of scope (Phoebus screens are not in this repo) | n/a | Same as FR-001 |
| FR-003 | Single reference definitions for Python device abstractions | `configuration_service` stores `DeviceInstantiationSpec` records pointing at concrete ophyd / ophyd-async classes; `direct_control/monitoring/describers.py` describes both ophyd and ophyd-async devices | Partial | Treat ophyd-async as the primary target; threaded ophyd as compat. Land new device-capability features in ophyd-async first |
| FR-004 | Central source of configuration for IOCs, screens, and ophyd | `configuration_service` is the ophyd-side central source via REST + PostgreSQL-backed registry; OpenAPI is published to `shared-schema/` | Partial | Define a stable cross-reference so IOC and screen config in the NSLS-II Ansible repos can name the same device identity that lives in `configuration_service` |
| FR-005 | Generate engineering screens from configuration | Out of scope (screen generation belongs to a Phoebus-side tool) | n/a | Confirm at FDR review that screen generation does not require new endpoints from `configuration_service` |
| FR-006 | Deploy IOCs to hosts and register them with EPICS services | Out of scope (handled by Ansible roles) | n/a | None — see FR-009 deployment note below |
| FR-007 | Deploy engineering screens to workstation hosts | Out of scope | n/a | None |
| FR-008 | Python device abstraction deployment logic — Ansible playbook + ophyd-async classes + **Object instantiation tool** | `configuration_service` records `DeviceInstantiationSpec`; runtime ingest via `POST /api/v1/devices` (see `integration/happi/runtime_seed.json` for ready-to-paste request bodies); HAPPI export at `GET /api/v1/registry/export?format=happi` round-trips through `HappiProfileLoader` | Partial | Position `configuration_service` as N3XTware's "Object instantiation tool". Ship a documented, tested call-site that turns a registry contents into instantiated objects suitable for BSUI or a service. Acceptance test: Mock-backend instantiation from a registry export |
| FR-009 | Containerization of supported IOCs | `ioc/` ships a containerized caproto sim IOC; `pods/full` and `pods/minimal` are containerized integration environments | Partial | Confirm whether ophyd-service's compose / Quadlet shape is acceptable to AAP, or whether we generate Quadlets to match N3XTware's preferred unit format |
| FR-010 | Standardize controls software configuration | `DeviceInstantiationSpec` schema, OpenAPI in `shared-schema/`, OpenAPI drift CI | Partial | Co-design `DeviceInstantiationSpec` with N3XTware's central-config schema rather than letting them evolve independently |
| FR-011 | Allow customization of definitions for beamline-specific requirements | `configuration_service` registers arbitrary args/kwargs per device; site overlays in `integration/happi/sites/<site>/` | Partial | Document the override boundary explicitly — what is per-device-class vs per-instance vs per-site |
| FR-012 | UI to manage configuration and deployment | N3XTware target is AAP + GitHub. ophyd-service has its own configuration UI in `frontend/` | Conflict | Decide: AAP+GitHub is deploy-time, our UI is runtime read-mostly. Or commit to merging. Letting both grow in parallel is the worst outcome — see "divergence risks" |
| FR-013 | Hardware test environment | Indirect — ophyd-service is what such an environment would run | n/a | None |
| FR-014 | Software test environment | `pods/full`, `pods/minimal`, `pods/dev`; CI runs full integration | Strong | Make pods deployable from an Ansible role so AAP can stand the same environment up on `runansible*` |
| FR-015 | Tutorials | Per-service `docs/` directories exist | Partial | Add a "deploying ophyd-service alongside N3XTware-managed IOCs" tutorial once the deployment role lands |
| NFR-001 | Maintainability | CI, schema drift checks, peer review | Strong | None specific |
| NFR-002 | Reliability | systemd / podman restart, healthcheck endpoints | Partial | Add Prometheus-style metrics endpoints if N3XTware deployments expect Grafana scrape |
| NFR-003 | Accuracy (IOC ↔ screen ↔ ophyd alignment) | `direct_control` describers verify connectivity against the live device | Partial | Add an end-state CI job that exports the registry, instantiates against the Mock backend, and asserts it matches the source-of-truth IOC PVs |
| NFR-004 | Standardization | Pydantic models, OpenAPI, shared-schema | Strong | None specific |
| NFR-005 | Idempotency | CRUD endpoints are idempotent by device name; seed sidecars are restartable | Partial | Audit seed pipeline for double-seed safety in the presence of partial failures |
| NFR-006 | Recoverability | PostgreSQL registry can be backed up / restored (pg_dump); HAPPI export is the offline portable form | Partial | Document a recovery runbook: snapshot → restore via HAPPI ingest |
| SEC-001 | Sharing of definitions, but not configurations | The upstream repo (`github.com/nsls2/ophyd-service`) is currently private to the NSLS-II GitHub org, which satisfies SEC-001 for the site-specific JSON under `integration/happi/sites/`. The development-only sandbox there is fine while the repo stays private | Aligned (conditional) | Becomes a real SEC-001 conflict only if this repo is ever made public. Trigger to revisit: any move toward open-sourcing `github.com/nsls2/ophyd-service`. At that point, site-specific configurations must move to a separate, access-controlled repo |
| SEC-002 | Network isolation of IOCs | Out of scope (network policy) | n/a | None |
| SEC-003 | Review process for definition changes | GitHub PR review on this repo | Strong | None specific |
| SEC-004 | Change traceability | Git history | Strong | None specific |

## Architecture-component mapping

The PDR's "Key Components & Interactions" list (PDR §System Architecture)
identifies 18 components. The ones that touch ophyd-service:

| PDR Arch # | Component | ophyd-service correspondence |
| --- | --- | --- |
| 7 | Pythonic Device Abstraction | `configuration_service` stores the metadata that points at these classes; `direct_control` instantiates and inspects them |
| 8 | Instance Configuration Repository | The intended *source* that seeds `configuration_service` at deploy time; currently `integration/happi/` plays this role for the integration environment, and a per-site private repo would replace it for production |
| 13 | Quadlets (systemd units for Podman) | `pods/full` / `pods/minimal` compose files are the upstream shape; Quadlets can be generated from them |
| 14 | BSUI (NSLS-II Bluesky CLI launcher) | Downstream consumer of `configuration_service`. Convergence move: BSUI profile reads from `configuration_service` at startup instead of HAPPI JSON, while the HAPPI export endpoint remains as a fallback / portability surface |
| 15 | HAPPI database | `configuration_service` is the intended **replacement** per PDR Risk-#6 mitigation ("create our own Ophyd device configuration service"). Bidirectional HAPPI bridge stays in place for compat |
| 16 | Profile Collection (IPython profile for BSUI) | See BSUI above. Open question for the FDR: is the profile the source of truth, or is `configuration_service` the source of truth and the profile a generated artifact? |
| 17 | Runansible hosts | Where the Ansible role wrapping our podman stack would be exercised |
| 18 | Ansible Automation Platform (AAP) | The deployment driver for ophyd-service in N3XTware contexts. Requires an Ansible role we do not yet ship |

## Where direct_control and the device-monitor live in N3XTware

This is the largest scope gap. None of N3XTware's five deliverables, as
currently described, covers a *runtime device-control surface* — that is, a
REST + WebSocket service that lets external clients read PVs, command
motors, subscribe to monitor streams, and observe lock state.

`direct_control_service` (REST + WebSocket on port 8003, with the
`monitoring/` device-monitor sub-package) is exactly that surface. It does
not fit FR-001…FR-015, all of which are about deploying IOC, screen, and
ophyd-class artifacts rather than serving them at runtime.

Two reasonable homes for this capability in the FDR:

1. **A sub-section of Deliverable 1** titled (for example) "Runtime device
   control surface", named alongside `configuration_service` as the pair
   that completes the IaC-deployed device story.
2. **A new deliverable**, parallel to the existing five, scoped to the
   runtime control plane that downstream services (Deliverable 2) and UIs
   rely on.

Option 1 is the lower-friction move during FDR drafting. Option 2 may end
up being correct longer-term but is harder to add once the five
deliverables are locked.

Recommendation: propose Option 1 at FDR review so this capability is
named, scoped, and owned rather than left implicit.

## Active convergence levers

These are the highest-leverage moves where being the obvious answer shapes
how N3XTware's solution lands.

1. **Claim FR-008 explicitly.** Land a documented, tested "Object
   instantiation tool" surface — given a site, return ophyd objects
   suitable for BSUI or downstream services. The pieces exist
   (`load_via_api.py`, the HAPPI export, the registry CRUD, the
   `direct_control` factory); the missing thing is the named, contracted
   call-site with N3XTware-style acceptance tests.
2. **Ship an Ansible role for the backends.** AAP-fronted deployment is
   the PDR's assumed deployment substrate. A thin role wrapping the podman
   compose stack closes the strongest single argument for someone else
   building a parallel service.
3. **Scope direct_control into the FDR (see previous section).** Without
   this, direct_control evolves without N3XTware coverage.
4. **Co-design DeviceInstantiationSpec with N3XTware's central-config
   schema.** If the central NSLS-II Ansible repo grows its own
   ophyd-config schema separately from ours, we become a translation
   layer. If we get involved while the schema is still forming, our spec
   *is* the schema.

## Divergence risks

These are the places ophyd-service will drift away from N3XTware if no one
is watching.

1. **Two control planes.** N3XTware names AAP + GitHub as the management
   UI (FR-012). `frontend/` is also a management UI. Pick: AAP is
   deploy-time, our UI is runtime read-mostly. Or commit to merging.
   Letting both grow in parallel is the worst outcome.
2. **ophyd-async vs threaded ophyd drift.** The PDR is ophyd-async-first
   (FR-003). Treat threaded ophyd as compat: every new feature lands in
   ophyd-async first and reaches threaded ophyd only if it is required
   for an existing deployment.
3. **HAPPI bridge bit-rot.** The bridge is bidirectional today. N3XTware
   acceptance tests will depend on it. Don't let it degrade silently —
   cover it with a CI job that round-trips registry → HAPPI → Mock-backend
   instantiation.
4. **Site-config leakage on visibility change.** Site-specific data under
   `integration/happi/sites/` is acceptable while the upstream repo
   (`github.com/nsls2/ophyd-service`) is private to the NSLS-II GitHub
   org, which it is today. SEC-001 only becomes a real problem if the
   repo is ever made public — at which point site configurations must
   move to a separate, access-controlled repo. Treat any open-sourcing
   discussion as the trigger to revisit.
5. **Unannounced schema breaks.** Once N3XTware deployments depend on
   `configuration_service`, every breaking change to
   `DeviceInstantiationSpec` or the OpenAPI surface is their problem too.
   Strengthen schema-drift CI and treat the OpenAPI in `shared-schema/`
   as a contract.

## What this document is not

- Not a deployment guide. Deployment to a NEXT-III beamline goes through
  AAP and lives in the NSLS-II Ansible monorepo.
- Not a feature backlog. Convergence moves listed above are pointers to
  decisions, not work-tracked tasks.
- Not a substitute for the FDR. When the FDR sections are filled in, this
  document should be re-read against them and updated.

## Maintenance

Update this document when:

- The N3XTware FDR fills in Deliverable 1 sections beyond the current
  skeleton.
- The Deliverable 1 PDR is revised (new requirement IDs, changed statuses,
  changed deployment assumptions).
- ophyd-service ships a capability that closes one of the gaps above —
  collapse the row and remove the obsolete divergence-risk entry.
- ophyd-service adds an extension point that affects how a NEXT-III
  beamline configures the service.
