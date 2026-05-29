# Deferred hardening — when each remaining phase becomes necessary

This document records the hardening work **intentionally deferred** on the
`spasa-video-restreamer`, and the concrete conditions under which each item turns from
"nice to have" into "required". It exists so the decision is explicit and revisitable —
nothing here is a silent gap.

Status as of this writing: Fases 0/1/2 + infra "B" (nginx mTLS, RBAC, HLS fail-closed,
MediaMTX auth) + the HLS data-plane offload + Fase 4 (observability/audit) are **done**.
The items below are **not implemented yet, by design**.

The decision hinges on three questions about the actual concept of operation (ConOps):

- **(a) Concurrency** — dozens vs hundreds of simultaneous viewers?
- **(b) Classification** — is the node single-level, or do feeds/users of *different*
  classification or need-to-know coexist on it?
- **(c) Accreditation** — does this system go through a formal ATO (STIG/RMF), or is it a
  lab/PoC?

Current answers assumed here: (a) dozens, (b) undecided/likely mixed need-to-know,
(c) no formal accreditation yet.

---

## FDE / LUKS — data at rest

**What it is.** Full-disk encryption of the partition holding recordings
(`data/streams`). **Host/OS-level provisioning, not application code** — LUKS is the
Linux standard.

**What it affects.** The server host, not the app. Implications:
- A key is required at boot. Unattended reboots need **TPM-backed auto-unlock** or a
  network key server; a manual passphrase means the node won't come back alone.
- Performance overhead is negligible on CPUs with AES-NI.

**When it becomes necessary.** When the hardware **can be physically captured or stolen**
(tactical/forward deployment) → it is the standard control protecting recorded footage.
If the node lives in a physically secured facility, that facility's controls may cover it.

**Effort.** Provisioning task (low code, operational planning for key custody).

---

## Fase 5 — classification labelling + per-feed access control

**What it is.** A classification label per stream/recording plus mandatory access control
so a viewer cannot see a feed above their clearance/need-to-know.

**What it affects.** Live-view authorization and recording handling.

**When it becomes necessary.** **Only if feeds/users of different classification or
need-to-know coexist on the node.** The deciding question:

> Do *all* viewers have the same clearance **and** need-to-know for *all* feeds?
> - **Yes** → not needed; the node boundary covers it. Set `CERT_DEFAULT_ROLE=viewer`
>   and any valid SPASA cert may watch.
> - **No** (coalition partners, different units, sensitive feeds mixed in) → per-feed
>   access segmentation **is required**.

**Recommended split when it is needed:**
- **Live-view authorization → delegate to SPASA's GroupVector.** TAK/SPASA already does
  exactly this (128-bit `groups_mask` per feed). Do **not** build a parallel
  classification engine in the restreamer. The mechanism is already in place: the
  **HMAC-signed HLS URLs** let SPASA gate per-feed access (per its GroupVector) and hand
  out a credential-less, time-limited URL; the restreamer just validates it. Until that
  GroupVector hook is wired, keep `CERT_DEFAULT_ROLE` **unset (fail-closed)**.
- **Recordings at rest → the restreamer's own part:** a classification **label** on each
  recording (for handling), plus **retention + crypto-erase** (build on
  `app/services/cleanup.py` + `cleanup_days`), and **FDE** above.

**Effort.** Live-view: low on the restreamer side (mostly the SPASA integration hook).
Recording labels/retention: medium.

---

## Fase 3 — SQLite control-plane + multi-worker + MediaMTX hooks

**What it is.** Move in-memory state (`app/state.py`) to **SQLite** so the control plane
can run **multiple workers** and survive restarts with full state; replace the 2 s
MediaMTX polling loop with **MediaMTX hooks** (`runOnReady`/`runOnNotReady`).

**What it affects.** Runtime topology, scalability, and state durability.

**When it becomes necessary.** When viewer concurrency grows to **hundreds with multiple
Flask workers**, or when full state durability across restarts is required.

> **Note:** the immediate scale concern for viewers is already addressed — the **HLS
> data-plane offload** (nginx serves segments via `X-Accel-Redirect`) removes the Python
> worker from the per-segment hot path, so one async worker handles many viewers. SQLite
> + multi-worker only becomes necessary beyond that. Partial state durability already
> exists (settings, ABR state, pull sources are persisted; orphan reconciliation on
> startup).

**Effort.** High (state-store abstraction + multi-worker correctness + hook wiring).

---

## Fase 6 — STIG-hardened container + SBOM + FIPS switch

**What it is.** Run the container **non-root**, **read-only rootfs**, `cap_drop: ALL`,
`no-new-privileges`, minimal multi-stage base; generate an **SBOM** (syft) and scan for
CVEs (trivy); document (do not enable) the **FIPS** switch.

**What it affects.** How the container is built and run (attack surface), and
supply-chain/compliance posture.

**When it becomes necessary.** When the system goes through **formal accreditation
(ATO / STIG / RMF)** — these are required controls. For a lab/PoC it's strong
best-practice but not blocking.

**FIPS note.** The codebase is already **FIPS-ready** (crypto centralized in
`app/utils/crypto.py`; nginx uses approved TLS suites). Activating FIPS is a documented,
contained switch: FIPS base image (e.g. UBI), FIPS builds of ffmpeg/MediaMTX, and host
`fips=1`. Do **not** enable it now (permanent maintenance cost) unless mandated.

**Effort.** Medium (Dockerfile/compose hardening + CI for SBOM/trivy).

---

## Cross-cutting (also deferred)

`ruff` + `mypy` + a CI pipeline (lint + pytest + trivy + SBOM). Low effort, high
long-term value; pairs naturally with Fase 6.
