# Deployment

Two topologies: **standard** (publish the media ports directly) and **hardened**
(everything behind an nginx mTLS edge on `:443`). Plus the SPASA compose rig for
running the sidecar next to the server.

## Standard

Publish the protocol ports you actually use; keep the MediaMTX management API on
loopback.

```yaml
# docker-compose.yml (media service) — illustrative
ports:
  - "3000:3000"      # Flask control API + UI
  - "8554:8554"      # RTSP (add /udp if you enable UDP)
  - "8555:8555"      # RTSPS
  - "8890:8890/udp"  # SRT
  - "8888:8888"      # HLS
  # WebRTC (only if enabled — see below):
  # - "9898:9898"      # WHEP signaling (TCP)
  # - "8189:8189/udp"  # ICE media (UDP)
```

Never publish `8889` (MediaMTX management API) — it is unauthenticated and bound to
`127.0.0.1`.

## Hardened (mTLS edge)

nginx terminates TLS 1.2/1.3 and enforces **client mTLS** at `:443`, forwarding the
app (`:3000`) and native HLS (`:8888`) over a private `edge` network and serving HLS
segments via `X-Accel-Redirect`. Set `MTLS_ENABLED=true`, a real `PROXY_SHARED_SECRET`
(matched on nginx and Flask), and a `data/cert_roles.json` mapping client certs to
roles. Only `:443` (and the raw media ingest ports you need) are published.

```text
   client ──TLS+mTLS──► nginx :443 ──(edge net)──► Flask :3000 / HLS :8888
                          │  X-SSL-Client-*, X-Proxy-Auth
                          └─ serves HLS segments directly (X-Accel-Redirect)
```

See [`DEFERRED-HARDENING.md`](DEFERRED-HARDENING.md) for hardening still on the
roadmap (FDE, classification, container STIG).

## WebRTC ports (when enabled)

WebRTC adds a port that an HTTP proxy **cannot** carry: the ICE **UDP** media. You
must publish both:

| Port | Transport | Role |
|------|-----------|------|
| `9898` (or your `webrtcAddress`) | TCP | WHEP signaling — proxyable. |
| `8189/udp` (or your ICE range) | **UDP** | ICE media — publish directly, or relay via TURN. |

If clients are behind NAT, configure STUN/TURN (`webrtcICEServers2`). Details in
[WebRTC / WHEP](webrtc-whep.md).

## SPASA compose rig

For end-to-end testing next to SPASA Server (`make run-docker-streamer` in the SPASA
repo):

- `docker-compose.streamer.yml` (SPASA) `include:`s **this repo's** compose verbatim
  (single source of truth for ports/volumes) and layers the SPASA wiring + lab
  relaxations. For WebRTC it publishes the signaling + ICE UDP ports on the `media`
  service.
- `deploy/streamer-e2e/mediamtx.test.yml` (SPASA) overrides `mediaMTX.yml` for the
  lab (open RTSP read; `webrtc: yes` with ICE settings).
- `config/config-docker-streamer.toml` (SPASA) points the server at the sidecar edge
  and selects `publish_protocol`.

Override the sidecar location with `RTVR_PATH` (default `../spasa-video-restreamer`).

## Production checklist

- [ ] Change `ADMIN_PASSWORD`; do **not** set `DEV_MODE`/`ALLOW_DEFAULT_PASSWORD`.
- [ ] Set a strong `MEDIAMTX_API_PASS` and `PROXY_SHARED_SECRET`.
- [ ] Keep MediaMTX API (`8889`) on loopback; never publish it.
- [ ] Hardened mode: real PKI in `nginx/pki/`, `cert_roles.json` populated,
      `CERT_DEFAULT_ROLE` left unset (fail-closed), `HLS_REQUIRE_AUTH=true`.
- [ ] WebRTC: publish the ICE UDP port (or run TURN); set the public host in
      `webrtcAdditionalHosts`.
- [ ] Persist the `data/` volume (settings, certs, audit log, pull sources).
