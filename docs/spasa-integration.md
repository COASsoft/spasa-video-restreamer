# SPASA integration

SPASA Server uses this restreamer as its **video data plane**. SPASA's control
logic (feeds, groups, CoT) drives the sidecar over its REST control API and stamps
the relay's public URL into the served feed; clients (ATAK/WinTAK/SPASA3D) then
play from the relay without knowing a sidecar exists. This guide documents that
boundary and the wiring you configure on the SPASA side.

> Opt-in by design: with the integration **disabled or absent**, SPASA behaves as
> metadata-only (feeds advertise their source URL directly). When enabled, SPASA
> republishes sources through the restreamer and rewrites the served feed URL.

## The boundary

```text
  ATAK / WinTAK / SPASA3D
        ▲   plays the stamped feed URL (rtsp/rtsps/srt/hls/webrtc)
        │
   SPASA Server  ──(control: REST over the nginx mTLS edge)──►  restreamer
   • feeds / groups / CoT          • pull source → relay
   • stamps FeedV2.url             • ABR, recording
   • KLV → CoT (SPI/footprint)     • KLV + VMTI latest sample
   • VMTI → C2 tracks              • ST 0102 security marking (in KLV)
   • ONVIF camera onboarding       • ONVIF LAN discovery
   • reconcile loop (feeds ⇄ streams)
```

SPASA talks to the sidecar's **control plane** (the Flask REST API), normally
through the sidecar's **nginx mTLS edge** with a SPASA-PKI client cert mapped to an
operator role. The media itself flows from MediaMTX straight to the clients.

## What SPASA configures (`[video_restreamer]`)

The relevant keys in SPASA's config (`config/config.reference.toml`,
`config/config-docker-streamer*.toml`):

| Key | Purpose |
|-----|---------|
| `enabled` | Master switch for the integration. |
| `base_url` | The sidecar control-plane base URL (e.g. the nginx edge service name). |
| `api_key_env` / `api_key_header` | API-key auth (or use mTLS, where the key is unused). |
| `public_rtsp_host` / `public_rtsp_port` / `public_rtsps_port` / `public_srt_port` | What gets stamped into the served feed URL. |
| `hls_edge_base` | HLS-ABR edge base — required when `publish_protocol = "hls"`. |
| `webrtc_edge_base` | **WebRTC WHEP edge base** — required when `publish_protocol = "webrtc"`. |
| `publish_protocol` | Default protocol stamped into the feed URL: `rtsp` \| `rtsps` \| `srt` \| `hls` \| `webrtc` (per-feed override supported). |
| `autopublish_cot` / `status_alerts` | Re-emit the `__video` CoT for live feeds / GeoChat on relay online↔offline. |
| `klv_cot` / `klv_stale_secs` | Poll each live feed's KLV and broadcast SPI / sensor / footprint CoT (see below). |
| `client_cert` / `client_key` / `ca_cert` | mTLS client identity to the sidecar edge. |
| `admin_ui` | Show the streamer control surface + embedded player in the SPASA admin. |

### Publish protocol → stamped URL

SPASA builds the served URL from the protocol:

| `publish_protocol` | Stamped URL |
|--------------------|-------------|
| `rtsp` | `rtsp://<public_rtsp_host>:<public_rtsp_port>/<stream>` |
| `rtsps` | `rtsps://<public_rtsp_host>:<public_rtsps_port>/<stream>` |
| `srt` | `srt://<public_rtsp_host>:<public_srt_port>?streamid=read:<stream>` |
| `hls` | `<hls_edge_base>/<stream>/master.m3u8` (falls back to RTSP if `hls_edge_base` unset) |
| `webrtc` | `<webrtc_edge_base>/<stream>/whep` (falls back to RTSP if `webrtc_edge_base` unset) |

WebRTC gives sub-second latency; see [WebRTC / WHEP](webrtc-whep.md) for the
sidecar-side enablement and the ICE/UDP ports.

## Control-plane endpoints SPASA uses

SPASA drives the sidecar through a small slice of the REST API (full inventory in
[`FEATURE-MAP.md`](../FEATURE-MAP.md)):

- **Pull streams** — `POST /api/streams/<name>/pull` (ingest an upstream source
  into a relay), `POST /api/streams/<name>/stop-pull`, `GET /api/pull-status`.
  Pull sources persist (`pull_sources.json`) and auto-retry across restarts.
- **ABR** — `POST /api/streams/<name>/abr` (enable adaptive HLS) and
  `GET /hls/<name>/master.m3u8` for the master playlist.
- **KLV latest** — `GET /api/streams/<name>/klv/latest` returns the decoded STANAG
  4609 / MISB 0601 sample (sensor lat/lon/alt, frame-center, heading, FOV, slant
  range, footprint corners) for SPASA to turn into CoT. The sample now also carries a
  **`security`** object (decoded MISB ST 0102, see below).
- **VMTI latest** — `GET /api/streams/<name>/vmti/latest` returns the live VMTI
  (MISB ST 0903) moving-target detections SPASA ingests as C2 tracks. Rides the same
  ST 0601 stream and **shares the per-stream KLV reader** (no extra FFmpeg per stream).
- **ONVIF discover** — `GET /api/onvif/discover` lists ONVIF cameras found on the LAN
  (with resolved RTSP URLs) for SPASA's `POST /restreamer/onvif/add` onboarding.
- **Health** — used by the reconcile loop and the SPASA admin `/c2/health`-style
  surfacing.

## KLV → CoT

With `klv_cot = true`, SPASA polls `…/klv/latest` once per reconcile cycle and
broadcasts SPI / sensor / footprint CoT to the feed's groups — so the whole team
sees where a sensor is looking without opening the video. Markers expire
`klv_stale_secs` after KLV stops. Lower `reconcile_interval_secs` for snappier SPI.

## VMTI → C2 tracks

VMTI (MISB ST 0903) rides the **same** UAS Datalink (ST 0601) stream as KLV — it is a
nested Local Set in tag 74 — so SPASA reads it from `…/vmti/latest` without a second
relay or a second FFmpeg (the endpoint reuses the per-stream KLV reader). Each entry is
a moving-target detection with an **absolute** geo position:

```json
{"streamName": "drone1", "present": true, "ageMs": 380,
 "targets": [{"targetId": 1, "lat": 38.82, "lon": -77.02, "haeM": 110.0,
              "confidence": 88, "priority": 2}]}
```

SPASA materializes **one C2 track per target** (reusing the track-bridge funnel — no
V1/V3/V8 duplication). Targets without a resolvable absolute Target Location are dropped
on the sidecar side (a track needs lat/lon); pixel-only centroids are deferred and simply
don't appear, so the consumer degrades cleanly.

## Classification marking (MISB ST 0102)

When the source embeds an ST 0102 Security Metadata Local Set (nested in ST 0601 tag 48),
`…/klv/latest` surfaces it as a `security` object:

```json
"security": {"classification": "SECRET", "classifyingCountry": "//US",
             "releasability": ["USA", "ESP"]}
```

`classification` is one of `UNCLASSIFIED | RESTRICTED | CONFIDENTIAL | SECRET |
TOP SECRET`. SPASA stamps this onto the feed's classification. The parse is **fail-soft**:
when no usable marking is present (absent/empty/truncated) the field is `null`, and SPASA
leaves the feed's existing classification untouched rather than downgrading it on a signal
glitch. This surfaces the *signal* only — per-feed access **enforcement** is delegated to
SPASA's GroupVector and remains a deferred hardening phase (see
[`DEFERRED-HARDENING.md`](DEFERRED-HARDENING.md) Fase 5).

## ONVIF camera onboarding

`GET /api/onvif/discover` runs a WS-Discovery multicast probe on the sidecar's LAN and
returns the ONVIF cameras it finds, each with a best-effort resolved RTSP URL, in the flat
shape SPASA's `POST /restreamer/onvif/add` consumes:

```json
[{"name": "Front Gate Cam", "rtspUrl": "rtsp://192.168.1.50:554/Profile_1",
  "profile": "Profile_1"}]
```

A camera whose RTSP URL can't be resolved (e.g. it requires auth) is still listed with an
empty `rtspUrl` so the operator can fill it in before adding; a LAN with no ONVIF cameras
yields `[]`. Discovery is LAN-scoped — the sidecar must share an L2 segment with the
cameras (the multicast probe doesn't cross routers).

## The reconcile loop

When enabled, SPASA keeps DB feeds ⇄ sidecar streams consistent and (optionally)
re-emits the `__video` CoT for live feeds so clients' "Play" reflects reality and
never goes stale. The broadcast is GroupVector-scoped per feed, so a restricted
feed stays within its groups.

## The SPASA admin player

The SPASA admin embeds a player for live preview, gated by `admin_ui`:

- **HLS** feeds → `hls.js` shim (`hls_shim.js`).
- **WebRTC** feeds (URL ends in `…/whep`) → `whep_shim.js` (native
  `RTCPeerConnection`, single WHEP offer/answer). The player auto-selects by URL
  and falls back to HLS.

## The E2E rig (SPASA repo)

SPASA ships a Docker rig that runs the sidecar next to the server for end-to-end
testing (`make run-docker-streamer`):

- `docker-compose.streamer.yml` — `include:`s the sidecar's own compose and layers
  the SPASA wiring + test relaxations. For WebRTC it publishes the **signaling TCP**
  and **ICE UDP** ports on the `media` service.
- `deploy/streamer-e2e/mediamtx.test.yml` — lab MediaMTX config; opens RTSP read to
  any IP and (for the WebRTC path) sets `webrtc: yes`, `webrtcAddress`,
  `webrtcLocalUDPAddress` and `webrtcAdditionalHosts`.
- `config/config-docker-streamer.toml` — the SPASA server config that points at the
  sidecar edge and selects the publish protocol.

See [Deployment](deployment.md) for which ports to publish and the hardened vs
standard topology.
