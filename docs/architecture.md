# Architecture

The restreamer is split into a **control plane** (a Python/Flask app that decides
*what* to stream, record and transcode) and a **data plane** (MediaMTX + FFmpeg
that move the actual video bytes). An optional **nginx mTLS edge** fronts both in
hardened deployments. Nothing in the control plane touches video frames directly;
nothing in the data plane makes access decisions.

```text
   publisher (drone / camera / upstream)
        │  RTSP 8554 · RTSPS 8555 · SRT 8890 · RTMP 1935
        ▼
   ┌──────────────────────────────────────────────────────────┐
   │  DATA PLANE                                                │
   │   MediaMTX  ── relays ──►  RTSP/HLS/WebRTC to viewers      │
   │      ▲  ▲                                                  │
   │      │  └── polled (loopback API :8889, user "control")    │
   │   FFmpeg subprocesses (spawned by Flask):                  │
   │     • recording  → data/streams/<name>/*.mov               │
   │     • ABR HLS     → data/hls/<name>/v<n>/*.ts              │
   │     • pull streams (ingest external source → relay)        │
   │     • KLV/VMTI demux → latest ST 0601 sample (cached)      │
   └──────────────────────────────────────────────────────────┘
        ▲                                   ▲
        │ control (REST :3000 + WebSocket)  │ HLS segments (offloaded)
   ┌──────────────────────────┐        ┌────────────────────────────┐
   │ CONTROL PLANE (Flask)     │        │ nginx mTLS edge :443        │
   │  auth (session/API key)   │        │  (hardened only)            │
   │  RBAC, audit log          │◄──────►│  TLS 1.2/1.3 + client mTLS  │
   │  stream/recording/ABR API │  X-SSL-*│  forwards :3000 / :8888    │
   │  KLV latest, pull control │  headers│  serves HLS via X-Accel    │
   └──────────────────────────┘        └────────────────────────────┘
```

## Components

| Component | Role | Plane |
|-----------|------|-------|
| **Flask app** (Gunicorn + eventlet) | REST API + WebSocket event bus; session/API-key auth, RBAC, audit; spawns and supervises FFmpeg; polls MediaMTX. | Control |
| **MediaMTX** (Go binary) | Multi-protocol ingest + relay engine (RTSP/RTSPS/SRT/RTMP/HLS/WebRTC). Its management API listens on `127.0.0.1:8889` (loopback, never published). | Data |
| **FFmpeg** (system binary) | Recording, ABR transcoding, pull-stream ingest, synthetic test patterns, and KLV data-track demux. One subprocess per task. | Data |
| **nginx** (hardened mode only) | TLS termination + client mTLS at `:443`; forwards the app and native HLS over a private `edge` network; serves HLS segments via `X-Accel-Redirect`. | Edge |
| **KLV reader** (`shared.klv`, optional) | Background per-stream FFmpeg demux of the STANAG 4609 / MISB 0601 data track; decodes and caches the latest sample for SPASA. One reader per stream serves both `/klv/latest` and `/vmti/latest`. | Data |
| **VMTI / ST 0102 decoders** (`shared.vmti`, `shared.security`) | Pure (no I/O) parsers for the nested Local Sets carried in the ST 0601 stream: VMTI moving targets (ST 0903, tag 74 → `/vmti/latest`) and the security marking (ST 0102, tag 48 → the `security` field of `/klv/latest`). | Data |
| **ONVIF discovery** (`shared.onvif` + `app/api/onvif.py`) | WS-Discovery LAN probe + ONVIF Media RTSP-URL resolution; lists cameras for SPASA onboarding (`/api/onvif/discover`). SOAP build/parse is pure; only the socket/HTTP glue does I/O. | Data |

## Control plane vs data plane

- **Control plane** owns *decisions*: who may view, which streams record, which run
  ABR, when to pull an upstream source, and the audit trail. It authenticates every
  request (session cookie, `X-API-Key`, Basic, or mTLS headers from nginx).
- **Data plane** owns *bytes*: MediaMTX accepts publishers and relays to viewers;
  FFmpeg transcodes/records. It makes no access decisions — it trusts that the
  control plane (or the nginx edge) already authorized the request.
- **HLS offload (hardened):** segments live under `/opt/app/hls/`; the app validates
  access and returns `X-Accel-Redirect`, and nginx transmits the bytes directly
  (the app never proxies segment bytes).

## How the planes interact

1. A publisher pushes RTSP/SRT/RTMP into MediaMTX.
2. Flask polls MediaMTX's loopback API (`:8889`, authenticated as the `control`
   user with `MEDIAMTX_API_PASS`) ~every 2 s to discover active paths.
3. For each enabled feature Flask spawns an FFmpeg subprocess that reads from the
   MediaMTX RTSP relay (`rtsp://127.0.0.1:8554/<name>`) and writes recordings, ABR
   segments, or decodes KLV.
4. Viewers consume the relay directly (RTSP/HLS/WebRTC from MediaMTX) or the ABR
   master playlist from Flask (`/hls/<name>/master.m3u8`).
5. In hardened mode nginx terminates mTLS, maps the client cert to a role
   (`data/cert_roles.json`), and forwards or serves accordingly.

## Where this sits next to SPASA

SPASA Server treats the restreamer as the **video data plane**: SPASA's control
logic (feeds, groups, CoT) drives the sidecar over the REST control API and stamps
the relay's public URL into the served feed. The boundary is deliberate — see
[SPASA integration](spasa-integration.md) and [`LICENSING-NOTES.md`](LICENSING-NOTES.md).
