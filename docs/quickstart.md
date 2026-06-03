# Quick start

Get the restreamer running, publish a stream, and watch it. For the full feature
walkthrough see the top-level [`README.md`](../README.md); this is the shortest path
to a working relay.

## 1. Run it (Docker)

```bash
docker compose up --build -d
# Web UI + control API:  http://localhost:3000
# Default login:         admin / changeme   (change it — see below)
```

The container starts MediaMTX (ingest/relay), the Flask control plane, and FFmpeg.
In production, **never** ship with the default password — set `ADMIN_PASSWORD` (the
UI shows a warning banner while the default is in use).

## 2. Publish a stream

Push RTSP into MediaMTX (port 8554, path = stream name):

```bash
# From a file (loop), as a quick test source:
ffmpeg -re -stream_loop -1 -i sample.mp4 -c copy -f rtsp rtsp://localhost:8554/cam1

# …or a drone/camera pointed at rtsp://<host>:8554/cam1
```

No source handy? Use the built-in **test pattern** API (see `FEATURE-MAP.md` §9) or
the test page in the Web UI.

## 3. Watch it

| Protocol | URL |
|----------|-----|
| RTSP (ATAK/VLC) | `rtsp://localhost:8554/cam1` |
| HLS (browser) | `http://localhost:8888/cam1/index.m3u8` (native) |
| ABR HLS (browser) | enable ABR, then `http://localhost:3000/hls/cam1/master.m3u8` |
| WebRTC (sub-second) | enable WebRTC first, then `http://localhost:9898/cam1/whep` |

VLC: *Open Network Stream* → paste the RTSP URL. Browser: open the HLS player at
`/hls/cam1/player`.

## 4. (Optional) Sub-second WebRTC

WebRTC is **off by default**. To try it: set `webrtc: yes` in `mediaMTX.yml`,
publish the signaling (TCP) and ICE (UDP) ports, and open the WHEP URL. Full steps
in [WebRTC / WHEP](webrtc-whep.md).

## 5. (Optional) Wire it to SPASA

Point SPASA's `[video_restreamer]` config at this sidecar, pick a
`publish_protocol`, and let SPASA stamp the relay URL into its feeds. See
[SPASA integration](spasa-integration.md).

## Next steps

- [Architecture](architecture.md) — how the pieces fit.
- [Protocols & ports](protocols.md) — pick the right output.
- [Configuration](configuration.md) — env vars and settings.
- [Deployment](deployment.md) — standard vs hardened (mTLS edge).
