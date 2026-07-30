"""MediaMTX authorization hook proxied to SPASA (SPASA SPEC-01 camino B / SPEC-08 gap D).

Exposes ``POST /auth/mediamtx``, the address MediaMTX calls when it runs with
``authMethod: http``. It forwards the hook body verbatim to SPASA's
``POST /video/ingest-auth`` and mirrors back the allow/deny, so that authorization to
publish stops being "is your IP inside ``VPN_CIDR``?" and becomes "does your SPASA user
have access to this feed?" — a device can then publish from 4G or from a new WiFi with
nothing to edit on the server, and revoking access takes effect on the next attempt.

Why a proxy instead of pointing MediaMTX straight at SPASA:

* MediaMTX cannot add headers to the hook request, and the sidecar→SPASA channel needs
  a shared secret. Here we can add ``X-SPASA-Ingest-Token``.
* The TLS trust store (SPASA's private CA) belongs to the container, not to
  ``mediamtx.yml``.
* Whether an unreachable SPASA means "deny" or "allow" is an operational decision, and
  this is the only place where it can be made. Default: deny (fail closed).

The contract is verified, not assumed — see ``CONTRACTS.md`` §4.5quater in the SPASA
server repo. Body: 9 fields (``action, id, ip, password, path, protocol, query, token,
user``). Response: **2xx allows, any non-2xx denies**. RTSP resolves in two calls (the
first arrives with empty credentials and must be refused so MediaMTX issues the
challenge); SRT resolves in one. This proxy takes no part in that logic — it relays the
decision — but it must never turn a non-2xx into a 2xx.
"""
import logging

import requests as http_requests
from flask import Blueprint, jsonify, request

from app.config import (SPASA_INGEST_AUTH_CA, SPASA_INGEST_AUTH_TIMEOUT, SPASA_INGEST_AUTH_URL,
                        SPASA_INGEST_FAIL_OPEN, SPASA_VIDEO_INGEST_TOKEN)

logger = logging.getLogger(__name__)

ingest_auth_bp = Blueprint('ingest_auth', __name__)

# Fields MediaMTX sends. Anything else in the body is dropped rather than forwarded:
# the hook payload is a fixed, verified contract, and relaying unknown fields would
# make this a general-purpose tunnel into SPASA.
_HOOK_FIELDS = ('action', 'id', 'ip', 'password', 'path', 'protocol', 'query', 'token', 'user')


def _deny():
    return jsonify({'error': 'denied'}), 401


def _is_loopback(addr):
    """MediaMTX runs in this very container, so the hook can only come from loopback.

    Flask's port is not published to the host in the shipped compose file, but this
    keeps the route from becoming a credential-checking oracle if someone publishes it.
    """
    return addr in ('127.0.0.1', '::1', '::ffff:127.0.0.1')


@ingest_auth_bp.route('/auth/mediamtx', methods=['POST'])
def mediamtx_auth():
    """Relay one MediaMTX authorization request to SPASA and mirror its verdict."""
    if not _is_loopback(request.remote_addr):
        logger.warning('ingest auth hook called from %s (loopback only)', request.remote_addr)
        return _deny()

    if not SPASA_INGEST_AUTH_URL or not SPASA_VIDEO_INGEST_TOKEN:
        # Misconfiguration, not a policy decision: deny and say why, once per attempt.
        # MediaMTX should not be on `authMethod: http` without this configured.
        logger.error('MediaMTX is using authMethod: http but SPASA_INGEST_AUTH_URL / '
                     'SPASA_VIDEO_INGEST_TOKEN are not set — denying every attempt')
        return _deny()

    body = request.get_json(silent=True) or {}
    payload = {field: body.get(field, '') for field in _HOOK_FIELDS}

    try:
        resp = http_requests.post(
            SPASA_INGEST_AUTH_URL,
            json=payload,
            headers={'X-SPASA-Ingest-Token': SPASA_VIDEO_INGEST_TOKEN},
            timeout=SPASA_INGEST_AUTH_TIMEOUT,
            verify=SPASA_INGEST_AUTH_CA or True,
        )
    except Exception as e:
        # Never log the body: it carries the publisher's password in clear.
        logger.error('SPASA ingest auth unreachable (%s): %s', type(e).__name__,
                     'failing open' if SPASA_INGEST_FAIL_OPEN else 'denying')
        if SPASA_INGEST_FAIL_OPEN:
            return jsonify({'status': 'allowed', 'degraded': True}), 200
        return _deny()

    if 200 <= resp.status_code < 300:
        return jsonify({'status': 'allowed'}), 200

    # Any non-2xx is a denial. Kept as a plain 401 rather than echoing SPASA's code:
    # MediaMTX only distinguishes 2xx from non-2xx, and the RTSP client will see 401
    # regardless of what we answer here.
    logger.info('SPASA denied %s on path %s from %s (status %s)',
                payload['action'] or '?', payload['path'] or '?', payload['ip'] or '?',
                resp.status_code)
    return _deny()
