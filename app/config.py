"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Application configuration and settings

All environment variables are read and validated here. ``validate_runtime_config()``
is called once at app startup (create_app) and fails closed on insecure
configuration (e.g. the default admin password in a non-dev deployment).
"""
import os
import sys
import secrets
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Typed environment helpers — parse + validate, fail fast with clear messages
# ---------------------------------------------------------------------------

class ConfigError(RuntimeError):
    """Raised when configuration is invalid or insecure."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def _env_int(name: str, default: int, *, minimum: int = None, maximum: int = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer, got {raw!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}, got {value}")
    return value


# ---------------------------------------------------------------------------
# Deployment mode flags (control fail-closed behaviour)
# ---------------------------------------------------------------------------
# DEV_MODE relaxes production safety checks (default password, etc.). It must
# never be enabled in a fielded/military deployment.
DEV_MODE = _env_bool('DEV_MODE', False)
# Explicit, narrow override to permit the default admin password even outside
# DEV_MODE (e.g. a throwaway lab). Off by default — production is fail-closed.
ALLOW_DEFAULT_PASSWORD = _env_bool('ALLOW_DEFAULT_PASSWORD', False)

# Flask Configuration
PORT = _env_int('PORT', 3000, minimum=1, maximum=65535)

# Directory Configuration (defined before SECRET_KEY so the key can be persisted)
STREAMS_DIR = os.environ.get('STREAMS_DIR', '/opt/app/streams')
# Places a copy of recordings in a shared volume.
SHARED_VIDEOS_DIR = os.environ.get('SHARED_VIDEOS_DIR', '/opt/app/shared_videos')
LOGS_DIR = os.environ.get('LOGS_DIR', '/opt/app/logs')
# Per-process FFmpeg stderr logs (shared by recordings/test/abr/srt). Centralized
# here so every spawn site captures stderr to the same place.
FFMPEG_LOG_DIR = os.environ.get('FFMPEG_LOG_DIR', os.path.join(LOGS_DIR, 'ffmpeg'))
DATA_DIR = os.environ.get('DATA_DIR', '/opt/app/data')
CERTS_DIR = os.environ.get('CERTS_DIR', os.path.join(STREAMS_DIR, '.certs'))
EXTERNAL_CERTS_DIR = os.environ.get('EXTERNAL_CERTS_DIR', '/opt/app/external-certs')
ACTIVE_CERTS_DIR = os.environ.get('ACTIVE_CERTS_DIR', '/opt/app/certs')

# Path to the persisted auto-generated secret key (used only when SECRET_KEY
# is not supplied via the environment).
_SECRET_KEY_FILE = os.path.join(DATA_DIR, 'secret_key')


def _resolve_secret_key() -> str:
    """Resolve the Flask session secret.

    Priority: explicit env ``SECRET_KEY`` > persisted key file > freshly
    generated key (persisted with 0600 perms). Persisting the generated key
    keeps sessions valid across restarts and consistent across workers, instead
    of silently invalidating every session on each boot.
    """
    env_secret = os.environ.get('SECRET_KEY', '')
    if env_secret and env_secret != 'dev-secret-key-change-in-production':
        return env_secret

    try:
        if os.path.isfile(_SECRET_KEY_FILE):
            with open(_SECRET_KEY_FILE, 'r') as f:
                existing = f.read().strip()
            if existing:
                return existing
    except OSError as e:
        print(f"WARNING: could not read persisted SECRET_KEY ({e}); generating a new one.")

    generated = secrets.token_hex(32)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        # O_CREAT|O_EXCL-style write with restrictive perms from the start.
        fd = os.open(_SECRET_KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as f:
            f.write(generated)
    except OSError as e:
        print(f"WARNING: could not persist SECRET_KEY to {_SECRET_KEY_FILE} ({e}); "
              "sessions will not survive a restart. Set SECRET_KEY in the environment.")
    return generated


SECRET_KEY = _resolve_secret_key()

# MediaMTX Configuration
# Use 127.0.0.1 (not 'localhost') to avoid IPv6 resolution on Windows
MEDIAMTX_API_URL = os.environ.get('MEDIAMTX_API_URL', 'http://127.0.0.1:8889')
MEDIAMTX_RTSP_URL = os.environ.get('MEDIAMTX_RTSP_URL', 'rtsp://127.0.0.1:8554')

# Optional credentials the app uses to authenticate to the MediaMTX control API.
# Unset = no auth (back-compat with an open localhost-bound API). Set these once the
# MediaMTX API requires credentials (Phase 1 infra "B"): a token takes precedence over
# user/pass and is sent as a Bearer header; user/pass is sent as HTTP Basic.
MEDIAMTX_API_USER = os.environ.get('MEDIAMTX_API_USER', '')
MEDIAMTX_API_PASS = os.environ.get('MEDIAMTX_API_PASS', '')
MEDIAMTX_API_TOKEN = os.environ.get('MEDIAMTX_API_TOKEN', '')


def _mediamtx_auth_kwargs() -> dict:
    """requests kwargs (auth=/headers=) for authenticating to the MediaMTX API.
    Empty when no credentials are configured (open-API back-compat). Reused by the
    few ad-hoc ``requests.get(MEDIAMTX_API_URL/v3/...)`` call sites outside the client."""
    if MEDIAMTX_API_TOKEN:
        return {'headers': {'Authorization': f'Bearer {MEDIAMTX_API_TOKEN}'}}
    if MEDIAMTX_API_USER:
        return {'auth': (MEDIAMTX_API_USER, MEDIAMTX_API_PASS)}
    return {}


# Ready-made requests kwargs for ad-hoc MediaMTX API calls (see direct call sites).
MEDIAMTX_API_AUTH = _mediamtx_auth_kwargs()

# CORS Configuration
CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '*')

# Optional bearer token gating the /metrics endpoint. /metrics sits OUTSIDE the
# /api auth gate (Prometheus-style root path), so when this is unset it is
# reachable without auth — acceptable for the VPN-only deployment. Set it (or
# restrict /metrics at the nginx edge) to lock the endpoint down.
METRICS_TOKEN = os.environ.get('METRICS_TOKEN', '')

# ---------------------------------------------------------------------------
# mTLS / RBAC (Phase 1 infra "B")
# ---------------------------------------------------------------------------
# Role names are duplicated here as literals (instead of importing app.auth) to
# avoid an import cycle: app.auth imports from app.config, never the reverse.
_VALID_ROLES = ('viewer', 'operator', 'admin')

# When True, the app honours mTLS client-cert identity injected by the trusted
# reverse proxy (nginx). The X-SSL-Client-* headers are trusted ONLY when the
# request also carries a matching PROXY_SHARED_SECRET (anti-spoof), so this is
# safe even on a shared docker network.
MTLS_ENABLED = _env_bool('MTLS_ENABLED', False)
# Shared secret nginx sends as X-Proxy-Auth. Required when MTLS_ENABLED (the app
# refuses to start otherwise — see validate_runtime_config). Without it, no
# X-SSL-Client-* header is ever honoured.
PROXY_SHARED_SECRET = os.environ.get('PROXY_SHARED_SECRET', '')
# cert CN/DN → role map (atomic JSON). Schema:
#   {"cn_roles": {<cn>: <role>}, "dn_roles": {<dn>: <role>}, "default_role": <role|null>}
CERT_ROLE_MAP_FILE = os.environ.get('CERT_ROLE_MAP_FILE', os.path.join(DATA_DIR, 'cert_roles.json'))
# Fallback role for an authenticated-but-unmapped client cert when the map file
# omits "default_role". None = fail-closed (no implicit role → denied). Military
# default: every identity must be explicitly granted a role. Flip to 'viewer'
# only once SPASA's per-feed GroupVector/classification ACL is wired in.
CERT_DEFAULT_ROLE = os.environ.get('CERT_DEFAULT_ROLE') or None
# Role granted to legacy API keys that predate role-aware keys (no "role" field).
API_KEY_DEFAULT_ROLE = os.environ.get('API_KEY_DEFAULT_ROLE', 'viewer')

# ---------------------------------------------------------------------------
# Ingest authorization delegated to SPASA (SPASA SPEC-01 camino B / SPEC-08 gap D)
# ---------------------------------------------------------------------------
# When MediaMTX runs with `authMethod: http` it POSTs every publish/read attempt
# to `authHTTPAddress`. Pointing it at this app's `/auth/mediamtx` lets SPASA make
# the decision with its own users and groups, instead of authorizing by IP range
# (VPN_CIDR) — so an authorized device can publish from 4G or a new WiFi with no
# edit on the server, and revoking access takes effect on the very next attempt.
#
# MediaMTX cannot add headers to that POST, which is why the app proxies it: the
# shared secret and the TLS trust store live here, not in mediamtx.yml.
#
# UNSET (default) = the proxy route refuses everything and MediaMTX should stay on
# `authMethod: internal`. Nothing about an existing deployment changes.
SPASA_INGEST_AUTH_URL = os.environ.get('SPASA_INGEST_AUTH_URL', '')
# Shared secret sent as X-SPASA-Ingest-Token. Must match the value SPASA resolves
# from `[video_restreamer] ingest_auth_token_env`.
SPASA_VIDEO_INGEST_TOKEN = os.environ.get('SPASA_VIDEO_INGEST_TOKEN', '')
# Per-request timeout (seconds) talking to SPASA. Kept short: MediaMTX holds the
# publisher's handshake open while this call is in flight.
SPASA_INGEST_AUTH_TIMEOUT = _env_int('SPASA_INGEST_AUTH_TIMEOUT', 3, minimum=1, maximum=30)
# CA bundle verifying SPASA's TLS. Empty = use the system store. Set to the
# deployment's ca.crt when SPASA serves a private-CA certificate.
SPASA_INGEST_AUTH_CA = os.environ.get('SPASA_INGEST_AUTH_CA', '')
# What to do when SPASA is unreachable (down, network partition). Default False =
# FAIL CLOSED: nobody publishes while SPASA is unreachable. This is the tradeoff
# the JWT path (camino A) would have avoided, and it is a deliberate, documented
# choice — set to true only if continuity of ingest outweighs authorization.
SPASA_INGEST_FAIL_OPEN = _env_bool('SPASA_INGEST_FAIL_OPEN', False)

# ---------------------------------------------------------------------------
# HLS access control (Phase 1 infra "B")
# ---------------------------------------------------------------------------
# When True, HLS playback is fail-closed: each HLS view requires either an
# authenticated session/API-key/mTLS identity, OR a valid unexpired HMAC-signed
# URL (?exp=&sig=). Default ON; the in-browser player carries the session cookie.
HLS_REQUIRE_AUTH = _env_bool('HLS_REQUIRE_AUTH', True)
# TTL (seconds) for signed HLS URLs issued by /api/streams/<name>/hls-token.
HLS_URL_TTL = _env_int('HLS_URL_TTL', 3600, minimum=60, maximum=86400)
# When True, HLS *segments* are served by nginx via X-Accel-Redirect (the app still
# makes the fail-closed authz decision, but the bytes never pass through Python).
# Requires the nginx /_hls_internal/ location + a read-only HLS mount in the proxy.
# Default OFF so direct runs (dev/rig/tests, no nginx) serve segments from disk.
HLS_X_ACCEL = _env_bool('HLS_X_ACCEL', False)
# Internal location prefix nginx maps back to the HLS dir (see nginx template).
HLS_X_ACCEL_PREFIX = os.environ.get('HLS_X_ACCEL_PREFIX', '/_hls_internal')

# ---------------------------------------------------------------------------
# Observability (Fase 4)
# ---------------------------------------------------------------------------
# Emit logs as structured JSON (one object per line) with a correlation id.
# Default OFF (human-readable in dev); the container turns it on.
LOG_JSON = _env_bool('LOG_JSON', False)
# Optional syslog/SIEM sink for the tamper-evident audit trail. When AUDIT_SYSLOG_HOST
# is set, each audit record is also shipped to syslog (append-only off-box = the real
# anti-tamper control). Unset = local file only (back-compat).
AUDIT_SYSLOG_HOST = os.environ.get('AUDIT_SYSLOG_HOST', '')
AUDIT_SYSLOG_PORT = _env_int('AUDIT_SYSLOG_PORT', 514, minimum=1, maximum=65535)

# Logging Configuration
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO').upper()
_VALID_LOG_LEVELS = {'DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'}
if LOG_LEVEL not in _VALID_LOG_LEVELS:
    LOG_LEVEL = 'INFO'
LOG_MAX_BYTES = 10 * 1024 * 1024  # 10MB per file
LOG_BACKUP_COUNT = 5  # Keep 5 backup files

# Check for optional modules
# KLV Module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))
try:
    import klv
    KLV_AVAILABLE = True
except ImportError:
    KLV_AVAILABLE = False
    print("Warning: KLV module not available")

# SRT Buffer Module
try:
    from srt_buffer import get_manager as get_srt_buffer_manager
    SRT_BUFFER_AVAILABLE = True
except ImportError:
    SRT_BUFFER_AVAILABLE = False
    print("Warning: SRT buffer module not available")

# Pull Stream Configuration
PULL_STREAM_BUFFER_SIZE = _env_int('PULL_STREAM_BUFFER_SIZE', 10485760, minimum=0)  # 10MB default buffer
PULL_STREAM_MAX_DELAY = _env_int('PULL_STREAM_MAX_DELAY', 1000000, minimum=0)  # 1s in microseconds


# ---------------------------------------------------------------------------
# Startup validation (fail-closed)
# ---------------------------------------------------------------------------

def validate_runtime_config() -> None:
    """Validate security-critical configuration at startup.

    Raises ConfigError (aborting startup) on insecure configuration. Called
    once from create_app(). Override knobs (DEV_MODE / ALLOW_DEFAULT_PASSWORD)
    exist for labs but default to the safe, fail-closed behaviour.
    """
    errors = []

    admin_password = os.environ.get('ADMIN_PASSWORD', 'changeme')
    if admin_password == 'changeme' and not (DEV_MODE or ALLOW_DEFAULT_PASSWORD):
        errors.append(
            "ADMIN_PASSWORD is the insecure default 'changeme'. Set a strong "
            "ADMIN_PASSWORD. (For a non-production lab only, set DEV_MODE=true or "
            "ALLOW_DEFAULT_PASSWORD=true to override.)"
        )

    # mTLS must not be enabled without the anti-spoof shared secret, or any
    # process on the docker network could forge X-SSL-Client-* identity headers.
    if MTLS_ENABLED and not PROXY_SHARED_SECRET and not DEV_MODE:
        errors.append(
            "MTLS_ENABLED is set but PROXY_SHARED_SECRET is empty. The shared "
            "secret is required to trust the nginx-injected X-SSL-Client-* headers "
            "(anti-spoof). Set PROXY_SHARED_SECRET."
        )

    # Role config must reference real roles, else access decisions are undefined.
    if CERT_DEFAULT_ROLE is not None and CERT_DEFAULT_ROLE not in _VALID_ROLES:
        errors.append(f"CERT_DEFAULT_ROLE invalid: {CERT_DEFAULT_ROLE!r} (expected one of {_VALID_ROLES} or unset).")
    if API_KEY_DEFAULT_ROLE not in _VALID_ROLES:
        errors.append(f"API_KEY_DEFAULT_ROLE invalid: {API_KEY_DEFAULT_ROLE!r} (expected one of {_VALID_ROLES}).")

    if errors:
        msg = "Insecure/invalid configuration — refusing to start:\n  - " + "\n  - ".join(errors)
        logger.critical(msg)
        raise ConfigError(msg)

    # Non-fatal posture warnings (tightened further in later phases).
    if CORS_ORIGINS == '*' and not DEV_MODE:
        logger.warning("CORS_ORIGINS is '*' (all origins). Set an explicit allow-list for production.")
    if not HLS_REQUIRE_AUTH and not DEV_MODE:
        logger.warning("HLS_REQUIRE_AUTH is off — HLS playback is unauthenticated. Leave it on outside a lab.")
    if MEDIAMTX_API_USER and not MEDIAMTX_API_PASS and not MEDIAMTX_API_TOKEN:
        logger.warning("MEDIAMTX_API_USER is set without MEDIAMTX_API_PASS/TOKEN — MediaMTX API auth may fail.")
    if PROXY_SHARED_SECRET and not MTLS_ENABLED:
        logger.warning("PROXY_SHARED_SECRET is set but MTLS_ENABLED is off — mTLS identity headers will be ignored.")


# Server Settings - Recording & Stream Management
# Type schema for validation: maps each key to (type, min, max) or (type,) for no range check
# str keys use (str,) only
SERVER_SETTINGS_SCHEMA = {
    'segmented_recording': (bool,),
    'segment_duration': (int, 10, 86400),
    'max_file_size_gb': (int, 1, 1000),
    'auto_cleanup_enabled': (bool,),
    'cleanup_days': (int, 1, 3650),
    'min_free_space_gb': (int, 1, 1000),
    'auto_reconnect': (bool,),
    'reconnect_delay': (int, 1, 300),
    'max_reconnect_attempts': (int, -1, 10000),
    'exponential_backoff': (bool,),
    'max_backoff_delay': (int, 1, 3600),
    'health_check_enabled': (bool,),
    'stall_detection_enabled': (bool,),
    'stall_threshold_seconds': (int, 5, 600),
    'srt_buffer_enabled': (bool,),
    'srt_auto_reconnect': (bool,),
    'srt_reconnect_delay': (int, 1, 300),
    'srt_max_buffer_seconds': (int, 1, 300),
    'rtsp_transport': (str,),
    'connection_timeout': (int, 100000, 60000000),
    'enable_ffmpeg_reconnect': (bool,),
    'standby_enabled': (bool,),
    'standby_timeout_minutes': (int, 0, 14400),  # 0 = infinite, max 10 days
}

SERVER_SETTINGS = {
    # Recording settings
    'segmented_recording': False,
    'segment_duration': 600,  # 10 minutes in seconds
    'max_file_size_gb': 10,
    'auto_cleanup_enabled': False,
    'cleanup_days': 30,
    'min_free_space_gb': 10,

    # Stream recovery settings
    'auto_reconnect': True,
    'reconnect_delay': 5,
    'max_reconnect_attempts': -1,  # -1 = unlimited
    'exponential_backoff': False,
    'max_backoff_delay': 60,

    # Health monitoring
    'health_check_enabled': True,
    'stall_detection_enabled': True,
    'stall_threshold_seconds': 30,

    # SRT buffering and recovery
    'srt_buffer_enabled': True,
    'srt_auto_reconnect': True,
    'srt_reconnect_delay': 2,
    'srt_max_buffer_seconds': 30,

    # Network resilience
    'rtsp_transport': 'tcp',  # tcp or udp
    'connection_timeout': 5000000,  # microseconds
    'enable_ffmpeg_reconnect': True,

    # Stream standby / persistence
    'standby_enabled': True,
    'standby_timeout_minutes': 60,  # 0 = never expire
}
