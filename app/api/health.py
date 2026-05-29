"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Health check API endpoints
"""
from flask import Blueprint, jsonify
from datetime import datetime, timezone
import os
import logging
import requests as http_requests
from app import state
from app.config import MEDIAMTX_API_URL, DATA_DIR, STREAMS_DIR

logger = logging.getLogger(__name__)

health_bp = Blueprint('health', __name__)

# Module-level flags for KLV and SRT buffer availability
_klv_available = None
_srt_buffer_available = None

def _check_klv_availability():
    """Check KLV module availability once at startup"""
    global _klv_available
    if _klv_available is None:
        try:
            from shared import klv
            _klv_available = True
        except ImportError:
            _klv_available = False
    return _klv_available

def _check_srt_buffer_availability():
    """Check SRT buffer availability once at startup"""
    global _srt_buffer_available
    if _srt_buffer_available is None:
        try:
            from shared.srt_buffer import get_manager
            _srt_buffer_available = True
        except ImportError:
            _srt_buffer_available = False
    return _srt_buffer_available


def _check_mediamtx():
    """Check if MediaMTX API is reachable."""
    try:
        resp = http_requests.get(f'{MEDIAMTX_API_URL}/v3/paths/list', timeout=3)
        return resp.status_code == 200
    except Exception:
        return False


def _dir_writable(path: str) -> bool:
    """True if path is an existing, writable directory."""
    try:
        return os.path.isdir(path) and os.access(path, os.W_OK)
    except OSError:
        return False


@health_bp.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        mediamtx_ok = _check_mediamtx()
        overall_status = 'healthy' if mediamtx_ok else 'degraded'
        
        return jsonify({
            'status': overall_status,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'systemStartTime': state.system_start_time.isoformat(),
            'mediamtx': 'up' if mediamtx_ok else 'down',
            'klvAvailable': _check_klv_availability(),
            'srtBufferAvailable': _check_srt_buffer_availability(),
            'activeRecordings': len(state.active_recordings),
            'activePullStreams': len(state.active_pull_streams),
            'pullStreamConfigs': len(state.pull_stream_configs),
            'autoRecordEnabled': state.auto_record_enabled
        })
    except Exception as e:
        logger.error(f"Health check error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@health_bp.route('/ready', methods=['GET'])
def readiness_check():
    """Readiness probe (distinct from /health liveness).

    Returns 200 only when the service can actually serve traffic: MediaMTX
    reachable AND the data/streams directories writable. Returns 503 otherwise
    so an orchestrator/load-balancer routes around a not-ready instance.
    """
    mediamtx_ok = _check_mediamtx()
    data_ok = _dir_writable(DATA_DIR)
    streams_ok = _dir_writable(STREAMS_DIR)
    ready = mediamtx_ok and data_ok and streams_ok
    payload = {
        'ready': ready,
        'mediamtx': 'up' if mediamtx_ok else 'down',
        'dataDirWritable': data_ok,
        'streamsDirWritable': streams_ok,
        'timestamp': datetime.now(timezone.utc).isoformat(),
    }
    return jsonify(payload), (200 if ready else 503)


@health_bp.route('/api/status', methods=['GET'])
def get_status():
    """Get overall system status (compatibility endpoint)"""
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'systemStartTime': state.system_start_time.isoformat(),
        'activeStreams': len([s for s in state.active_recordings.keys()]),
        'activeRecordings': len(state.active_recordings)
    })
