# Troubleshooting

Symptom → likely cause → fix, for the failures that actually happen. For anything
deeper, check the app log and per-stream FFmpeg logs under `data/logs/`
(`data/logs/ffmpeg/<name>.log`) and the audit log (`GET /api/audit`).

## No video at all

| Symptom | Cause | Fix |
|---------|-------|-----|
| Player can't connect on `:8554` | Stream not published, or wrong path | Confirm the publisher is pushing to `rtsp://<host>:8554/<name>`; check the MediaMTX path list in the UI. |
| Relay path missing in the UI | Flask can't reach the MediaMTX API | Ensure `MEDIAMTX_API_URL` is reachable on loopback and `MEDIAMTX_API_PASS` matches `MTX_AUTHINTERNALUSERS_0_PASS`. |
| Recording/ABR never starts | FFmpeg failed to spawn | Read `data/logs/ffmpeg/<name>.log`; usually a codec/source issue. |

## WebRTC: signaling connects but no video

This is the classic WebRTC failure and it is almost always the **ICE/UDP path**:

| Symptom | Cause | Fix |
|---------|-------|-----|
| WHEP `POST` returns 200, then black | ICE UDP port not reachable | Publish the ICE UDP port (`8189/udp` or your range); it does **not** go through an HTTP proxy. |
| Works on the host, not remotely | ICE candidates advertise the wrong host | Set the public host/IP in `webrtcAdditionalHosts`. |
| Behind NAT / strict firewall | No usable direct UDP path | Add STUN, and TURN for symmetric NAT (`webrtcICEServers2`). |
| 404 on the WHEP URL | WebRTC disabled or wrong port | `webrtc: yes` in `mediaMTX.yml`; URL is `<host>:<webrtcAddress>/<name>/whep`. |

See [WebRTC / WHEP](webrtc-whep.md) for the full enablement.

## HLS

| Symptom | Cause | Fix |
|---------|-------|-----|
| `503` on `master.m3u8` | ABR not running or stalled > 30 s | Enable ABR (`POST /api/streams/<name>/abr`); check the source is live. |
| Browser CORS error embedding HLS | Cross-origin playback | Use the CORS proxy `/api/hls/proxy/<name>/...` (or a signed HLS URL in hardened mode). |
| Playback denied (hardened) | `HLS_REQUIRE_AUTH=true` | Use a signed URL (`GET /api/streams/<name>/hls-token`) or authenticate. |

## Pull streams

| Symptom | Cause | Fix |
|---------|-------|-----|
| Pull keeps reconnecting | Upstream flapping | Expected; tune `auto_reconnect` / `reconnect_delay` / `max_reconnect_attempts` in settings. |
| Pull lost after restart | — | It shouldn't be: sources persist in `pull_sources.json` and auto-retry. Check that `data/` is a persisted volume. |

## Auth / mTLS

| Symptom | Cause | Fix |
|---------|-------|-----|
| 401 on the API | Missing/invalid credential | Send `X-API-Key`, a session cookie, or Basic auth. |
| mTLS requests denied (hardened) | Cert not mapped / secret mismatch | Add the CN/DN to `data/cert_roles.json`; ensure `PROXY_SHARED_SECRET` matches on nginx and Flask. |
| "default password" banner | `ADMIN_PASSWORD` still default | Set a real `ADMIN_PASSWORD` (and don't set `ALLOW_DEFAULT_PASSWORD`). |

## SPASA integration

| Symptom | Cause | Fix |
|---------|-------|-----|
| SPASA feed plays nothing | Wrong stamped URL / protocol | Check SPASA's `publish_protocol` and the matching `*_edge_base`; for `webrtc`, confirm `webrtc_edge_base` is set (else it falls back to RTSP). |
| KLV CoT never appears | KLV not flowing or `klv_cot` off | Confirm the source carries MISB 0601; `GET /api/streams/<name>/klv/latest` should show `present: true`; enable `klv_cot` in SPASA. |
| SPASA can't reach the sidecar | mTLS / network | Verify SPASA's `client_cert`/`client_key`/`ca_cert` and that the edge service name is in the cert SAN. |
