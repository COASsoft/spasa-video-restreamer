"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Prometheus-compatible metrics endpoint (dependency-free).

Exposes operational gauges in the Prometheus text exposition format so the
service can be monitored without pulling in a metrics library. Kept defensive:
any subsystem that can't be queried contributes 0 rather than failing the scrape.
"""
import hmac
import logging
import shutil
import time

import requests as http_requests
from flask import Blueprint, Response, request

from app import state
from app.config import STREAMS_DIR, MEDIAMTX_API_URL, METRICS_TOKEN

logger = logging.getLogger(__name__)

metrics_bp = Blueprint('metrics', __name__)


# Cache the MediaMTX reachability probe so a high scrape frequency doesn't issue
# one (potentially 2s-blocking) network call per scrape.
_MEDIAMTX_UP_TTL = 5.0  # seconds
_mediamtx_up_cache = {'ts': 0.0, 'value': 0}


def _mediamtx_up() -> int:
    now = time.monotonic()
    if _mediamtx_up_cache['ts'] and (now - _mediamtx_up_cache['ts']) < _MEDIAMTX_UP_TTL:
        return _mediamtx_up_cache['value']
    try:
        r = http_requests.get(f'{MEDIAMTX_API_URL}/v3/paths/list', timeout=2)
        value = 1 if r.status_code == 200 else 0
    except Exception:
        value = 0
    _mediamtx_up_cache['ts'] = now
    _mediamtx_up_cache['value'] = value
    return value


def _disk_free_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return -1


def _gauge(lines: list, name: str, value, help_text: str):
    lines.append(f'# HELP {name} {help_text}')
    lines.append(f'# TYPE {name} gauge')
    lines.append(f'{name} {value}')


def _abr_active_count() -> int:
    try:
        from app.services.abr import abr_manager
        return len(abr_manager.list_active())
    except Exception:
        return 0


def _standby_count() -> int:
    try:
        from app.services.standby import standby_manager
        return len(standby_manager.get_standby_streams())
    except Exception:
        return 0


@metrics_bp.route('/metrics', methods=['GET'])
def metrics():
    """Render current gauges in Prometheus text exposition format."""
    # Optional opt-in bearer-token guard (see METRICS_TOKEN). When unset the
    # endpoint stays open for scraping; edge restriction is handled by nginx.
    if METRICS_TOKEN:
        provided = request.headers.get('Authorization', '')
        if not hmac.compare_digest(provided, f'Bearer {METRICS_TOKEN}'):
            return Response('forbidden\n', status=403,
                            mimetype='text/plain; charset=utf-8')

    lines = []
    _gauge(lines, 'tvr_up', 1, 'Service liveness (1 while responding).')
    _gauge(lines, 'tvr_active_recordings', len(state.active_recordings),
           'Number of active recording processes.')
    _gauge(lines, 'tvr_active_pull_streams', len(state.active_pull_streams),
           'Number of active pull-stream processes.')
    _gauge(lines, 'tvr_known_streams', len(state.known_streams),
           'Streams currently observed as ready on MediaMTX.')
    _gauge(lines, 'tvr_active_abr_streams', _abr_active_count(),
           'Number of active ABR transcoding processes.')
    _gauge(lines, 'tvr_standby_streams', _standby_count(),
           'Number of streams currently in standby.')
    _gauge(lines, 'tvr_streams_dir_free_bytes', _disk_free_bytes(STREAMS_DIR),
           'Free bytes on the recordings volume (-1 if unknown).')
    _gauge(lines, 'tvr_mediamtx_up', _mediamtx_up(),
           'MediaMTX API reachability (1 up / 0 down).')

    body = '\n'.join(lines) + '\n'
    return Response(body, mimetype='text/plain; version=0.0.4; charset=utf-8')
