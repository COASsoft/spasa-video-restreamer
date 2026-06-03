# WebRTC / WHEP — sub-second video

WebRTC is the restreamer's **lowest-latency** output (typically **< 1 second**,
vs. ~3–10 s for HLS). MediaMTX serves it via **WHEP** (WebRTC-HTTP Egress
Protocol): a viewer POSTs an SDP offer over HTTP and then receives media over
UDP. It is **disabled by default** — this guide is how to turn it on, expose the
right ports, survive NAT, and let SPASA consume it.

> **The one thing to remember:** WebRTC is HTTP for *signaling* and **UDP for
> *media***. The UDP media does **not** traverse an HTTP reverse proxy. If you
> only publish/forward the HTTP signaling port, you'll get a connected signaling
> handshake and **no video**.

## How WHEP works (just enough)

1. The player creates an `RTCPeerConnection`, adds recvonly transceivers, and makes
   an **SDP offer**.
2. It `POST`s that offer to `<webrtc_edge>/<path>/whep` with
   `Content-Type: application/sdp`.
3. MediaMTX replies with an **SDP answer** containing its ICE candidates.
4. ICE negotiates a **UDP** path between player and MediaMTX; media flows over it.

MediaMTX answers with its candidates inline (non-trickle works), so a single
offer/answer round-trip is enough — which is exactly what the SPASA admin's
`whep_shim.js` does.

## Enabling WebRTC in MediaMTX

In `mediaMTX.yml` (the shipped file has these **disabled**):

```yaml
# Global settings -> WebRTC server
webrtc: yes
webrtcAddress: :9898            # HTTP signaling (WHEP) — TCP
webrtcLocalUDPAddress: :8189    # ICE media — UDP (single muxed port)
webrtcAdditionalHosts: [localhost]   # hostnames/IPs advertised in ICE candidates
# webrtcICEServers2:            # STUN/TURN, only needed for NAT'd clients
#   - url: stun:stun.l.google.com:19302
#   - url: turn:turn.example.es:3478   # set username/password
```

| Key | Meaning |
|-----|---------|
| `webrtc` | Master on/off. |
| `webrtcAddress` | TCP port for WHEP **signaling** (the HTTP offer/answer). |
| `webrtcLocalUDPAddress` | A single UDP port that muxes all ICE host candidates (simplest to firewall). Alternatively use an ICE port *range*. |
| `webrtcAdditionalHosts` | Extra hostnames/IPs to advertise as ICE host candidates — set the **public** host/IP clients must reach. |
| `webrtcICEServers2` | STUN (candidate discovery) and/or TURN (relay) servers — needed when clients are behind NAT. |

> `webrtcAddress` in the shipped config is `:9898` because the MediaMTX management
> API already owns `:8889`. Pick any free port; just keep signaling and the API
> distinct.

## Ports to publish

You must publish **both** the signaling TCP port and the ICE UDP port:

```yaml
# docker-compose (media service)
ports:
  - "9898:9898"        # WHEP signaling (HTTP/TCP)
  - "8189:8189/udp"    # ICE host candidate (media/UDP)
```

If you firewall by hand, open `9898/tcp` and `8189/udp` (or your ICE range).

## NAT traversal (STUN / TURN)

- **Same host / LAN, no NAT:** host candidates on the advertised host
  (`webrtcAdditionalHosts`) over the published UDP port are enough.
- **Clients behind NAT:** add **STUN** (`webrtcICEServers2`) so the server learns
  its reflexive candidates.
- **Symmetric NAT / strict firewalls:** add a **TURN** relay — the only reliable
  path when direct UDP is impossible. TURN relays the media, so size it for your
  concurrent-viewer bandwidth.

## The reverse-proxy caveat

In hardened deployments an nginx mTLS edge fronts HTTP (`:443` → app / HLS). WebRTC
**signaling** can be proxied like any HTTP request, but the **ICE/UDP media cannot
go through an HTTP proxy**. Two options:

1. **Expose UDP directly** (publish the ICE UDP port at the host/firewall), keeping
   only signaling behind the proxy; or
2. **Use TURN** — the media relays through the TURN server instead of a direct UDP
   path, so the proxy only ever sees HTTP.

This is the main operational risk of the WebRTC path — it is a *networking* concern,
not a code one.

## The WHEP URL

MediaMTX serves WHEP at:

```
<scheme>://<host>:<webrtcPort>/<stream_name>/whep
```

SPASA builds exactly this from `webrtc_edge_base` (see below):
`<webrtc_edge_base>/<stream_name>/whep`.

## How SPASA consumes WebRTC

SPASA Server stamps the WHEP URL into the served feed when a feed publishes via the
`webrtc` protocol:

- Set `publish_protocol = "webrtc"` (global default) or override per feed.
- Set `webrtc_edge_base = "https://tak.example.es/webrtc"` (no trailing slash) in
  the `[video_restreamer]` config. SPASA appends `/<stream>/whep`.
- If `webrtc_edge_base` is **unset**, SPASA degrades gracefully to RTSP rather than
  emit a broken URL.
- The SPASA **admin player** auto-selects WHEP for `…/whep` feed URLs (via
  `whep_shim.js`) and falls back to HLS otherwise.

See [SPASA integration](spasa-integration.md) for the full wiring and the E2E rig.

## Verifying

1. `make run-docker-streamer` (SPASA) or start the sidecar with WebRTC enabled.
2. Publish a stream (or use a test pattern).
3. Open `http://<host>:9898/<name>/whep` from a WHEP-capable player (or the SPASA
   admin player on a `webrtc` feed).
4. Confirm playback and **measure end-to-end latency < 1 s**.
5. If signaling connects but no video appears → the **ICE/UDP path is blocked**
   (see [Troubleshooting](troubleshooting.md)).
