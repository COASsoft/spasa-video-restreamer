# Protocols & ports

The restreamer ingests on a few protocols and re-publishes on several. This is the
canonical port table and a per-protocol cheat sheet: URL shape, transport, latency
profile, and when to reach for each.

## Port table

| Port | Protocol | Transport | Role | Exposure |
|-----:|----------|-----------|------|----------|
| 8554 | RTSP | TCP (UDP opt.) | Ingest **and** relay/playback | Published |
| 8555 | RTSPS | TCP (TLS) | Encrypted RTSP ingest/playback | Published |
| 8890 | SRT | **UDP** | Low-latency ingest/playback over lossy links | Published |
| 1935 | RTMP | TCP | Legacy ingest | Published (optional) |
| 8888 | HLS | TCP (HTTP) | MediaMTX native single-bitrate HLS playback | Published or proxy-only |
| 8889 | WebRTC | TCP signaling (HTTP) | WHEP signaling — **disabled by default** | Published *if* enabled |
| (range) | WebRTC ICE | **UDP** | WebRTC media (ICE) — needed when WebRTC is on | Published *if* enabled |
| 3000 | HTTP/REST + WS | TCP | Flask control API + Web UI | Published or proxy-only |
| 8889 | MediaMTX API | TCP (HTTP) | Management API — **loopback only** | `127.0.0.1` only |
| 443 | HTTPS + mTLS | TCP | nginx edge (hardened mode) | Published (hardened) |

> **Port `8889` appears twice on purpose.** The shipped `mediaMTX.yml` puts the
> MediaMTX *management API* on `127.0.0.1:8889` (loopback, unauthenticated — never
> publish it) and the *WebRTC signaling* server on `:9898` (disabled by default).
> SPASA's prod config and E2E rig re-map these; always check which `:8889` a config
> means. See [WebRTC / WHEP](webrtc-whep.md).

## Cheat sheet

| Protocol | URL shape | Latency | Use it when… |
|----------|-----------|--------:|--------------|
| **RTSP** | `rtsp://<host>:8554/<name>` | ~0.5–2 s | Default. ATAK/WinTAK/desktop players, recording sources. |
| **RTSPS** | `rtsps://<host>:8555/<name>` | ~0.5–2 s | Same as RTSP but the link must be encrypted (TLS). |
| **SRT** | `srt://<host>:8890?streamid=read:<name>` | ~0.3–1 s | Lossy / long-haul links (satellite, cellular) — SRT recovers loss. |
| **HLS** | `http://<host>:8888/<name>/index.m3u8` (native) or `/<name>/master.m3u8` (ABR) | ~3–10 s | Browsers and CDNs; adaptive bitrate; widest reach, highest latency. |
| **WebRTC (WHEP)** | `<webrtc_edge>/<name>/whep` | **< 1 s** | Browser playback that must be near-real-time (operator situational awareness). |

## RTSP / RTSPS

The lingua franca of tactical video. RTSP is the default publish protocol and the
input FFmpeg reads for recording and ABR. RTSPS is the same stream with TLS on
`:8555` (the in-app TLS service manages the cert; `AUTO_GENERATE_CERTS=1` makes a
self-signed one on startup).

## SRT

UDP-based with built-in loss recovery and configurable latency buffering — the
right choice over unreliable links. Read with `streamid=read:<name>`, publish with
`streamid=publish:<name>`.

## HLS (and ABR)

MediaMTX serves a native single-bitrate HLS on `:8888`. The Flask app adds **ABR**
(adaptive bitrate): FFmpeg produces multiple renditions (e.g. `high` 1280×720 @
2500 kbps, `low` 640×360 @ 600 kbps) and a master playlist at
`/hls/<name>/master.m3u8`, with a CORS proxy at `/api/hls/proxy/<name>/...` for
external embedding. HLS trades latency (segment buffering) for reach and
adaptivity. See `FEATURE-MAP.md` §7–8 for the full ABR/HLS API.

## WebRTC (WHEP)

The sub-second path. MediaMTX serves **WHEP** (WebRTC-HTTP Egress Protocol): a
browser POSTs an SDP offer to `<webrtc_edge>/<name>/whep` and gets an SDP answer,
then media flows over **UDP (ICE)** — which is exactly why WebRTC needs more than
an HTTP port. It is **disabled by default** in the shipped `mediaMTX.yml`. Enabling
it, the ports, and NAT traversal are covered in detail in
[WebRTC / WHEP](webrtc-whep.md).
