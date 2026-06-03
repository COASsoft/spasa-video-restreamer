# FAQ

### Which output protocol should I use?

Default to **RTSP** (port 8554) for ATAK/WinTAK/desktop. Use **SRT** over lossy or
long-haul links. Use **HLS/ABR** for browsers and the widest reach (at ~3–10 s
latency). Use **WebRTC/WHEP** when you need **sub-second** browser latency. See
[Protocols & ports](protocols.md).

### Why is WebRTC off by default?

WebRTC needs a UDP media path and (often) STUN/TURN, which is environment-specific
and a firewall/NAT concern. Shipping it off avoids a half-working default. Turning
it on is a few `mediaMTX.yml` keys plus publishing the ICE UDP port — see
[WebRTC / WHEP](webrtc-whep.md).

### WebRTC signaling connects but I get no video. Why?

The ICE **UDP** media path is blocked. The HTTP signaling working tells you nothing
about UDP — publish the ICE UDP port directly (it can't traverse an HTTP proxy), set
the public host in `webrtcAdditionalHosts`, and add STUN/TURN for NAT.

### How does SPASA consume the streams?

SPASA drives the control API (pull/ABR/KLV) and **stamps** the relay URL into its
served feed based on `publish_protocol`. For WebRTC it builds
`<webrtc_edge_base>/<stream>/whep`. Clients play that URL without knowing a sidecar
exists. See [SPASA integration](spasa-integration.md).

### Can I run WebRTC behind the nginx mTLS edge?

The **signaling** can be proxied like any HTTP. The **media** cannot — expose the
ICE UDP port directly or relay it through TURN. The proxy then only ever sees HTTP.

### Is the MediaMTX API safe to expose?

No. It's unauthenticated and bound to `127.0.0.1:8889`. Keep it on loopback; the
Flask control plane authenticates to it as the `control` user.

### How do I secure a production deployment?

Change `ADMIN_PASSWORD`, set strong `MEDIAMTX_API_PASS` / `PROXY_SHARED_SECRET`, run
hardened mode (nginx mTLS edge, `cert_roles.json`, fail-closed `CERT_DEFAULT_ROLE`,
`HLS_REQUIRE_AUTH=true`), and never set `DEV_MODE`/`ALLOW_DEFAULT_PASSWORD`. See the
[Deployment](deployment.md) checklist.

### What's KLV and why does SPASA poll it?

KLV is STANAG 4609 / MISB 0601 telemetry embedded in the video (sensor position,
frame center, FOV, footprint). The sidecar decodes the latest sample at
`GET /api/streams/<name>/klv/latest`; SPASA turns it into SPI / sensor / footprint
CoT so the team sees where a sensor is looking without opening the video.

### Where's the exhaustive reference?

[`README.md`](../README.md) (features + walkthrough) and
[`FEATURE-MAP.md`](../FEATURE-MAP.md) (every endpoint, service, setting, event).
The guides in this `docs/` folder are the curated layer on top.

### Is the GPLv3 of this sidecar a problem for SPASA?

No — SPASA stays network-separated and aggregates rather than derives. The mTLS
boundary is also a licensing safeguard. See [`LICENSING-NOTES.md`](LICENSING-NOTES.md).
