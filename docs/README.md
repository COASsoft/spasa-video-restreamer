# Documentation — TAK Video Restreamer (streamer-spasa)

Canonical documentation for the **TAK Video Restreamer**: the Flask + MediaMTX +
FFmpeg video data-plane sidecar that SPASA Server (and operators) use to ingest,
relay, record and re-publish tactical video over RTSP / RTSPS / SRT / HLS /
**WebRTC**, with STANAG 4609 (MISB 0601) KLV processing.

> New here? Start with the [quick start](quickstart.md). Already running it and
> wiring SPASA? Jump to [SPASA integration](spasa-integration.md). Want the
> sub-second-latency path? See [WebRTC / WHEP](webrtc-whep.md).

## Documentation map

| Document | What it covers |
|----------|----------------|
| [Quick start](quickstart.md) | Run the sidecar, publish a test stream, watch it. The "hello, restreamer". |
| [Architecture](architecture.md) | Control plane (Flask) vs data plane (MediaMTX + FFmpeg), the hardened nginx mTLS edge, KLV, and the request/media flow. |
| [Protocols & ports](protocols.md) | Every protocol (RTSP/RTSPS/SRT/RTMP/HLS/WebRTC) with its port, URL shape, latency profile and when to use it. The canonical ports table. |
| [WebRTC / WHEP](webrtc-whep.md) | The sub-second-latency path: enabling WebRTC in MediaMTX, the signaling + ICE/UDP ports, STUN/TURN for NAT, the WHEP URL, and the nginx-proxy caveat. |
| [SPASA integration](spasa-integration.md) | How SPASA Server drives the sidecar: the pull/ABR control API, `publish_protocol` (incl. `webrtc`), `webrtc_edge_base`, served-feed URL stamping, KLV→CoT, the admin player, and the E2E rig. |
| [Configuration](configuration.md) | Environment variables, runtime settings, and the `mediaMTX.yml` keys you actually touch. |
| [Deployment](deployment.md) | Standard vs hardened (mTLS edge) deploy, which ports to publish (incl. UDP for WebRTC), Docker, and the SPASA compose rig. |
| [Troubleshooting](troubleshooting.md) | Symptom → cause → fix for the common failures (no video, WebRTC won't connect, HLS stalls, pull retries, auth). |
| [FAQ](faq.md) | Frequent questions about protocols, latency, security and the SPASA wiring. |

## Reference material (exhaustive)

The two top-level files are the **complete** product reference; the guides above
are the curated, task-oriented layer on top of them:

- [`README.md`](../README.md) — features, quick start, full feature walkthrough.
- [`FEATURE-MAP.md`](../FEATURE-MAP.md) — every REST endpoint, service, config
  option and WebSocket event (porting reference).

## Operational / legal notes

- [`DEFERRED-HARDENING.md`](DEFERRED-HARDENING.md) — hardening phases still open
  (FDE, classification, container STIG…) and when each applies.
- [`LICENSING-NOTES.md`](LICENSING-NOTES.md) — GPLv3 + USAF unlimited-rights, and
  why SPASA stays network-separated (the mTLS boundary is also a licensing
  safeguard).
