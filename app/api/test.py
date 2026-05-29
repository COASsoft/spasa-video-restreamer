"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Test Pattern API endpoints
"""
import logging
import os
import re
import time
import uuid
from flask import Blueprint, request, jsonify

from app.utils.validation import is_valid_stream_name
from app.services.process import ManagedProcess

logger = logging.getLogger(__name__)

test_bp = Blueprint('test', __name__, url_prefix='/api/test')

# Where FFmpeg stderr for test patterns is captured (shared with other services).
_FFMPEG_LOG_DIR = os.environ.get(
    'FFMPEG_LOG_DIR', os.path.join(os.environ.get('LOGS_DIR', '/opt/app/logs'), 'ffmpeg'))

# Store active test processes
active_tests = {}


def _validate_stream_name(name: str) -> bool:
    return is_valid_stream_name(name)

_RESOLUTION_RE = re.compile(r'^\d{1,5}x\d{1,5}$')

def _validate_resolution(res: str) -> bool:
    return bool(_RESOLUTION_RE.match(res))

# Allowed test patterns (FFmpeg lavfi sources)
_VALID_PATTERNS = {'testsrc', 'testsrc2', 'smptebars', 'smptehdbars', 'color'}

# Allowed framerates
_VALID_FRAMERATES = {24, 25, 29.97, 30, 60}

def _build_test_cmd(protocol: str, stream_name: str, resolution: str,
                    duration: int, pattern: str = 'testsrc',
                    framerate: float = 30) -> list:
    """Build an FFmpeg test-pattern command for any protocol."""
    if pattern not in _VALID_PATTERNS:
        pattern = 'testsrc'
    if framerate not in _VALID_FRAMERATES:
        framerate = 30
    fps = int(framerate) if framerate == int(framerate) else framerate

    # Video source
    if pattern == 'color':
        video_input = f'color=c=blue:s={resolution}:r={fps}'
    elif pattern in ('smptebars', 'smptehdbars'):
        video_input = f'{pattern}=s={resolution}:r={fps}'
    else:
        video_input = f'{pattern}=size={resolution}:rate={fps}'

    cmd = [
        'ffmpeg',
        '-re',
        '-f', 'lavfi', '-i', video_input,
        '-f', 'lavfi', '-i', 'sine=frequency=1000:sample_rate=48000',
        '-c:v', 'libx264',
        '-preset', 'ultrafast',
        '-tune', 'zerolatency',
        '-c:a', 'aac',
    ]

    if duration > 0:
        cmd.extend(['-t', str(duration)])

    # Output
    if protocol == 'srt':
        cmd.extend(['-f', 'mpegts',
                     f'srt://localhost:8890?streamid=publish:{stream_name}'])
    elif protocol == 'rtsps':
        cmd.extend(['-f', 'rtsp', '-rtsp_transport', 'tcp',
                     f'rtsps://localhost:8555/{stream_name}'])
    elif protocol == 'rtsp-udp':
        cmd.extend(['-f', 'rtsp', '-rtsp_transport', 'udp',
                     f'rtsp://localhost:8554/{stream_name}'])
    else:  # rtsp tcp
        cmd.extend(['-f', 'rtsp', '-rtsp_transport', 'tcp',
                     f'rtsp://localhost:8554/{stream_name}'])
    return cmd


def _start_test(protocol: str):
    """Shared handler for all test-pattern endpoints."""
    data = request.get_json()
    stream_name = data.get('streamName')
    resolution = data.get('resolution', '1280x720')
    duration = data.get('duration', 60)
    pattern = data.get('pattern', 'testsrc')
    framerate = data.get('framerate', 30)

    if not stream_name:
        return jsonify({'success': False, 'error': 'streamName is required'}), 400
    if not _validate_stream_name(stream_name):
        return jsonify({'success': False, 'error': 'Invalid stream name (alphanumeric, dash, underscore only, max 64 chars)'}), 400
    if not _validate_resolution(resolution):
        return jsonify({'success': False, 'error': 'Invalid resolution format (e.g. 1280x720)'}), 400

    # Coerce types
    try:
        duration = int(duration)
    except (ValueError, TypeError):
        duration = 60
    try:
        framerate = float(framerate)
    except (ValueError, TypeError):
        framerate = 30

    # Clamp: 0 = continuous, otherwise 5..3600
    if duration != 0:
        duration = max(5, min(duration, 3600))

    test_id = str(uuid.uuid4())
    cmd = _build_test_cmd(protocol, stream_name, resolution, duration, pattern, framerate)

    label = protocol.upper().replace('-', ' ')
    logger.info(f"Starting {label} test pattern: {stream_name} ({resolution} {framerate}fps {pattern}) "
                f"{'continuous' if duration == 0 else f'{duration}s'}")

    process = ManagedProcess(f'test-{stream_name}', cmd, _FFMPEG_LOG_DIR,
                             label=f'test:{protocol}:{stream_name}')
    if not process.start():
        return jsonify({'success': False, 'error': 'Failed to start FFmpeg'}), 500
    active_tests[test_id] = {
        'process': process,
        'protocol': protocol,
        'stream_name': stream_name,
        'resolution': resolution,
        'duration': duration,
        'pattern': pattern,
        'framerate': framerate,
        'start_time': time.time(),
    }

    return jsonify({
        'success': True,
        'testId': test_id,
        'streamName': stream_name,
        'message': f'{label} test pattern started: {stream_name}',
    })


@test_bp.route('/srt', methods=['POST'])
def start_srt_test():
    """Start SRT test pattern"""
    try:
        return _start_test('srt')
    except Exception as e:
        logger.error(f"Failed to start SRT test: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@test_bp.route('/rtsp', methods=['POST'])
def start_rtsp_test():
    """Start RTSP (TCP) test pattern"""
    try:
        return _start_test('rtsp')
    except Exception as e:
        logger.error(f"Failed to start RTSP test: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@test_bp.route('/rtsps', methods=['POST'])
def start_rtsps_test():
    """Start RTSPS (encrypted) test pattern"""
    try:
        return _start_test('rtsps')
    except Exception as e:
        logger.error(f"Failed to start RTSPS test: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@test_bp.route('/rtsp-udp', methods=['POST'])
def start_rtsp_udp_test():
    """Start RTSP (UDP) test pattern"""
    try:
        return _start_test('rtsp-udp')
    except Exception as e:
        logger.error(f"Failed to start RTSP-UDP test: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@test_bp.route('/list', methods=['GET'])
def list_tests():
    """List all active test patterns (survives page refresh)."""
    result = []
    stale = []
    for test_id, info in list(active_tests.items()):
        proc = info['process']
        if proc.poll() is not None:
            stale.append(test_id)
            continue
        elapsed = int(time.time() - info['start_time'])
        result.append({
            'testId': test_id,
            'protocol': info['protocol'],
            'streamName': info['stream_name'],
            'resolution': info['resolution'],
            'duration': info['duration'],
            'pattern': info.get('pattern', 'testsrc'),
            'framerate': info.get('framerate', 30),
            'elapsed': elapsed,
            'running': True,
        })
    # Clean up stale entries
    for tid in stale:
        del active_tests[tid]
    return jsonify({'success': True, 'tests': result})


@test_bp.route('/<test_id>/stop', methods=['POST'])
def stop_test(test_id):
    """Stop a running test"""
    try:
        if test_id not in active_tests:
            return jsonify({'success': False, 'error': 'Test not found'}), 404
        
        test_info = active_tests[test_id]
        process = test_info['process']

        # Bounded graceful stop (SIGTERM -> wait -> SIGKILL), never blocks long.
        process.stop(term_timeout=5)

        del active_tests[test_id]
        
        return jsonify({
            'success': True,
            'message': f'Test stopped: {test_info["stream_name"]}'
        })
        
    except Exception as e:
        logger.error(f"Failed to stop test {test_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@test_bp.route('/<test_id>/status', methods=['GET'])
def get_test_status(test_id):
    """Get status of a running test"""
    try:
        if test_id not in active_tests:
            return jsonify({'success': False, 'running': False, 'error': 'Test not found'}), 404
        
        test_info = active_tests[test_id]
        process = test_info['process']
        elapsed = int(time.time() - test_info['start_time'])
        
        if process.poll() is None:
            status = 'running'
        else:
            status = 'completed'
            # Clean up completed test
            del active_tests[test_id]
        
        return jsonify({
            'success': True,
            'testId': test_id,
            'running': status == 'running',
            'status': status,
            'protocol': test_info['protocol'],
            'streamName': test_info['stream_name'],
            'resolution': test_info['resolution'],
            'duration': test_info['duration'],
            'pattern': test_info.get('pattern', 'testsrc'),
            'framerate': test_info.get('framerate', 30),
            'elapsed': elapsed,
        })
        
    except Exception as e:
        logger.error(f"Failed to get test status {test_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
