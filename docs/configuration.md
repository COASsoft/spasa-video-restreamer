# Configuration

What you actually configure: environment variables (deploy-time), runtime settings
(editable via the API/UI), and the `mediaMTX.yml` keys you touch. The exhaustive
list of every setting and endpoint is in [`FEATURE-MAP.md`](../FEATURE-MAP.md) §10.

## Environment variables

### Core

| Variable | Default | Purpose |
|----------|---------|---------|
| `PORT` | `3000` | Flask HTTP port. |
| `ADMIN_USERNAME` | `admin` | Default admin user. |
| `ADMIN_PASSWORD` | `changeme` | **Change in production.** UI warns while default. |
| `SECRET_KEY` | auto (persisted to `data/secret_key`) | Flask session secret. |
| `MEDIAMTX_API_URL` | `http://127.0.0.1:8889` | MediaMTX management API (loopback). |
| `MEDIAMTX_RTSP_URL` | `rtsp://127.0.0.1:8554` | RTSP relay FFmpeg reads from. |
| `MEDIAMTX_HLS_URL` | `http://127.0.0.1:8888` | MediaMTX native HLS base. |
| `MEDIAMTX_API_PASS` | `mediamtx-change-me` | Password for the `control` user on the MediaMTX API (injected as `MTX_AUTHINTERNALUSERS_0_PASS`). |
| `STREAMS_DIR` / `DATA_DIR` / `LOGS_DIR` / `HLS_OUTPUT_DIR` | `/opt/app/...` | Recordings / persistence / logs / ABR segments. |
| `ENABLE_GPU_ENCODING` | `0` | `1` to use NVIDIA NVENC. |
| `HLS_SEGMENT_DURATION` / `HLS_LIST_SIZE` | `4` / `10` | HLS segment length / playlist depth. |
| `DEV_MODE` | `false` | Relax production safety checks (lab only). |
| `ALLOW_DEFAULT_PASSWORD` | `false` | Permit the default password (lab only). |
| `AUTO_GENERATE_CERTS` | `false` | Self-sign an RTSPS cert on startup. |

### Hardened mode (mTLS edge)

| Variable | Default | Purpose |
|----------|---------|---------|
| `MTLS_ENABLED` | `false` | Trust the `X-SSL-Client-*` identity headers from nginx. |
| `PROXY_SHARED_SECRET` | `proxy-shared-change-me` | Anti-spoof secret; must match on nginx **and** Flask. |
| `CERT_DEFAULT_ROLE` | unset (fail-closed) | Fallback role for an unmapped client cert. |
| `HLS_REQUIRE_AUTH` | `true` | Fail-closed HLS playback (auth or signed URL required). |
| `HLS_URL_TTL` | `3600` | TTL (s) for HMAC-signed HLS URLs. |
| `VPN_CIDR` | `10.0.0.0/8` | CIDR allowed to publish without credentials. |
| `METRICS_ALLOW_CIDR` | `127.0.0.1/32` | Who may scrape `/metrics` at the edge. |

## Runtime settings (`POST /api/settings`)

Editable live (persisted to `data/*.json`): segmented recording + segment duration,
auto-cleanup (age threshold, min free space), pull-stream auto-reconnect (delay,
max attempts), and the ABR rendition ladder (`POST /api/settings/abr`). Defaults and
ranges are in `FEATURE-MAP.md` §10.

## `mediaMTX.yml` keys you touch

The data-plane config (mounted into the container at `/opt/app/mediamtx.yml`). The
keys most teams change:

```yaml
rtsp: yes
rtspAddress: :8554
protocols: [tcp, udp]

hls: yes
hlsAddress: :8888

# WebRTC — OFF by default; see docs/webrtc-whep.md to enable.
webrtc: no
webrtcAddress: :9898
# webrtcLocalUDPAddress: :8189
# webrtcAdditionalHosts: [your.public.host]
# webrtcICEServers2: [...]   # STUN/TURN for NAT

api: yes
apiAddress: 127.0.0.1:8889   # loopback only — never publish
```

> Keep `apiAddress` on loopback. The MediaMTX management API is unauthenticated;
> the Flask control plane authenticates to it as the `control` user.

## Authentication

The control API accepts, in order of preference:

1. **Session cookie** (`POST /api/auth/login`, 7-day remember-me).
2. **API key** — `X-API-Key` header (stored SHA-256-hashed; create via
   `POST /api/auth/keys`).
3. **Basic auth** — `Authorization: Basic …`.
4. **mTLS** (hardened) — nginx forwards `X-SSL-Client-CN/DN/Verify` + `X-Proxy-Auth`;
   `data/cert_roles.json` maps the cert to `admin` / `operator` / `viewer`.

Login is rate-limited (5/min); the audit log (`data/audit.log`,
`GET /api/audit`) records logins, key changes, and cert/TLS changes.
