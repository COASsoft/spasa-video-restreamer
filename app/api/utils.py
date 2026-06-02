"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Utils API Blueprint - Utility endpoints (transcode, test patterns, etc.)
"""
from flask import Blueprint, request, jsonify
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import tempfile
import threading
import time
import os
import sys
import re
import logging

from app.config import KLV_AVAILABLE, STREAMS_DIR
from app.state import active_transcodes
from app.websocket.broadcast import broadcast
from shared.klv import parse_klv_packets

logger = logging.getLogger(__name__)

utils_bp = Blueprint('utils', __name__)


@utils_bp.route('/api/transcode/options', methods=['GET'])
def get_transcode_options():
    """
    Get available transcode options
    """
    options = [
        {
            "option": 1,
            "name": "MOV with corrected timecode",
            "description": "QuickTime MOV with H.264 video and corrected timecode track",
            "format": "mov",
            "klv": False
        },
        {
            "option": 2,
            "name": "MP4 + KLV backup",
            "description": "MP4 with H.264 video and separate binary KLV file",
            "format": "mp4",
            "klv": True,
            "klvFormat": "separate_file"
        },
        {
            "option": 3,
            "name": "MXF + KLV backup",
            "description": "Broadcast MXF with MPEG-2 video and separate binary KLV file",
            "format": "mxf",
            "klv": True,
            "klvFormat": "separate_file"
        },
        {
            "option": 4,
            "name": "MPEG-TS with embedded KLV (RECOMMENDED)",
            "description": "MPEG Transport Stream with H.264 video and STANAG 4609 embedded KLV metadata",
            "format": "ts",
            "klv": True,
            "klvFormat": "embedded",
            "recommended": True
        }
    ]
    
    return jsonify({
        "options": options,
        "default": 4
    })


@utils_bp.route('/api/transcode', methods=['POST'])
def start_transcode():
    """
    Start transcoding a video file
    
    Request body:
        {
            "inputFile": "/path/to/video.mov",
            "option": 1-4,
            "streamName": "optional"
        }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Request body required'}), 400
        
        input_file = data.get('inputFile')
        option = data.get('option', 4)  # Default to MPEG-TS with embedded KLV
        stream_name = data.get('streamName', 'transcoded')
        
        if not input_file:
            return jsonify({'error': 'inputFile required'}), 400
        
        # Resolve relative paths from STREAMS_DIR
        if not os.path.isabs(input_file):
            if '..' in input_file or '\x00' in input_file:
                return jsonify({'error': 'Invalid file path'}), 400
            input_file = os.path.join(STREAMS_DIR, input_file)
            resolved = Path(input_file).resolve()
            if not resolved.is_relative_to(Path(STREAMS_DIR).resolve()):
                return jsonify({'error': 'Access denied'}), 403
        
        if not os.path.exists(input_file):
            return jsonify({'error': 'Input file not found'}), 404
        
        if option not in [1, 2, 3, 4]:
            return jsonify({'error': 'Invalid option (must be 1-4)'}), 400
        
        if option in [2, 3, 4] and not KLV_AVAILABLE:
            return jsonify({'error': 'KLV module not available'}), 400
        
        # Generate unique transcode ID
        import random
        transcode_id = f"transcode_{int(time.time())}_{random.randint(1000, 9999)}"
        
        # Determine output file extension based on option
        output_extensions = {1: '.mov', 2: '.mp4', 3: '.mxf', 4: '.ts'}
        expected_ext = output_extensions.get(option, '.mov')
        base_name = os.path.splitext(input_file)[0]
        expected_output = f"{base_name}_transcoded{expected_ext}"
        
        # Store transcode job info
        active_transcodes[transcode_id] = {
            'id': transcode_id,
            'inputFile': input_file,
            'option': option,
            'streamName': stream_name,
            'status': 'starting',
            'progress': 0,
            'startTime': time.time(),
            'expectedOutput': expected_output,
            'error': None
        }
        
        # Run transcode in background thread
        def transcode_worker():
            try:
                active_transcodes[transcode_id]['status'] = 'running'
                logger.info(f"Starting transcode [{transcode_id}]: {input_file} (option {option})")
                
                # Get path to transcode script (cross-platform)
                script_dir = Path(__file__).parent.parent.parent / 'utils'
                transcode_script = script_dir / 'transcode_video.py'
                
                # Build command
                cmd = [
                    sys.executable,  # Use current Python interpreter
                    str(transcode_script),
                    input_file
                ]
                
                # Run with option selection via stdin
                process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1  # Line buffered
                )
                
                active_transcodes[transcode_id]['pid'] = process.pid
                
                # Send option selection to stdin and close it
                process.stdin.write(str(option) + '\n')
                process.stdin.close()
                
                # Read stdout line by line to parse progress
                stdout_lines = []
                stderr_lines = []
                
                # Read both stdout and stderr
                while True:
                    # Check if process is still running
                    if process.poll() is not None:
                        # Process finished, read remaining output
                        remaining_stdout = process.stdout.read()
                        remaining_stderr = process.stderr.read()
                        if remaining_stdout:
                            stdout_lines.append(remaining_stdout)
                        if remaining_stderr:
                            stderr_lines.append(remaining_stderr)
                        break
                    
                    # Read stdout line
                    line = process.stdout.readline()
                    if line:
                        stdout_lines.append(line)
                        
                        # Parse progress from lines like: "  [████████░░░░] 45% (12.5/28.0s)"
                        if '%' in line and '[' in line:
                            try:
                                # Extract percentage
                                percent_match = re.search(r'(\d+)%', line)
                                if percent_match:
                                    progress = int(percent_match.group(1))
                                    active_transcodes[transcode_id]['progress'] = progress
                                    logger.debug(f"Transcode [{transcode_id}] progress: {progress}%")
                            except (ValueError, KeyError):
                                pass
                    
                    # Small delay to avoid busy waiting
                    time.sleep(0.1)
                
                stdout = ''.join(stdout_lines)
                stderr = ''.join(stderr_lines)
                
                duration = time.time() - active_transcodes[transcode_id]['startTime']
                
                if process.returncode == 0:
                    logger.info(f"Transcode completed [{transcode_id}]: {input_file} ({duration:.1f}s)")
                    active_transcodes[transcode_id]['status'] = 'completed'
                    active_transcodes[transcode_id]['progress'] = 100
                    active_transcodes[transcode_id]['completedTime'] = time.time()
                    active_transcodes[transcode_id]['duration'] = duration
                    
                    # Try to find actual output file
                    if os.path.exists(expected_output):
                        active_transcodes[transcode_id]['outputFile'] = expected_output
                        active_transcodes[transcode_id]['outputSize'] = os.path.getsize(expected_output)
                    
                    broadcast('transcode_complete', {
                        'id': transcode_id,
                        'inputFile': input_file,
                        'outputFile': expected_output,
                        'option': option,
                        'success': True,
                        'duration': duration
                    })
                else:
                    logger.error(f"Transcode failed [{transcode_id}]: {stderr}")
                    active_transcodes[transcode_id]['status'] = 'failed'
                    active_transcodes[transcode_id]['error'] = stderr[-500:] if stderr else 'Unknown error'
                    active_transcodes[transcode_id]['completedTime'] = time.time()
                    active_transcodes[transcode_id]['duration'] = duration
                    
                    broadcast('transcode_complete', {
                        'id': transcode_id,
                        'inputFile': input_file,
                        'option': option,
                        'success': False,
                        'error': stderr[-500:] if stderr else 'Unknown error'
                    })
                    
            except subprocess.TimeoutExpired:
                error_msg = 'Transcode timeout after 10 minutes'
                logger.error(f"Transcode timeout [{transcode_id}]")
                active_transcodes[transcode_id]['status'] = 'failed'
                active_transcodes[transcode_id]['error'] = error_msg
                active_transcodes[transcode_id]['completedTime'] = time.time()
                broadcast('transcode_complete', {
                    'id': transcode_id,
                    'inputFile': input_file,
                    'option': option,
                    'success': False,
                    'error': error_msg
                })
            except Exception as e:
                logger.error(f"Transcode error [{transcode_id}]: {e}")
                active_transcodes[transcode_id]['status'] = 'failed'
                active_transcodes[transcode_id]['error'] = str(e)
                active_transcodes[transcode_id]['completedTime'] = time.time()
                broadcast('transcode_complete', {
                    'id': transcode_id,
                    'inputFile': input_file,
                    'option': option,
                    'success': False,
                    'error': str(e)
                })
        
        threading.Thread(target=transcode_worker, daemon=True).start()
        
        return jsonify({
            'success': True,
            'message': 'Transcode started',
            'transcodeId': transcode_id,
            'inputFile': input_file,
            'option': option,
            'expectedOutput': expected_output
        })
        
    except Exception as e:
        logger.error(f"Error starting transcode: {e}")
        return jsonify({'error': 'Failed to start transcode. Please check the input file.'}), 500


@utils_bp.route('/api/transcode/<transcode_id>/status', methods=['GET'])
def get_transcode_status(transcode_id):
    """Get status of a specific transcode job"""
    try:
        if transcode_id not in active_transcodes:
            return jsonify({'error': 'Transcode job not found'}), 404
        
        job = active_transcodes[transcode_id]
        
        # Calculate elapsed time - use stored duration for completed/failed jobs
        if job['status'] in ['completed', 'failed']:
            elapsed = job.get('duration', time.time() - job['startTime'])
        else:
            elapsed = time.time() - job['startTime']
        
        response = {
            'id': job['id'],
            'inputFile': job['inputFile'],
            'option': job['option'],
            'streamName': job.get('streamName'),
            'status': job['status'],
            'progress': job.get('progress', 0),
            'startTime': job['startTime'],
            'elapsed': elapsed,
            'expectedOutput': job.get('expectedOutput'),
            'error': job.get('error')
        }
        
        # Add completion info if available
        if job['status'] in ['completed', 'failed']:
            response['completedTime'] = job.get('completedTime')
            response['duration'] = job.get('duration', elapsed)
            
        # Add output file info if available
        if job.get('outputFile'):
            response['outputFile'] = job['outputFile']
            response['outputSize'] = job.get('outputSize')
        
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"Error listing transcode jobs: {e}")
        return jsonify({'error': 'Failed to list transcode jobs'}), 500


@utils_bp.route('/api/transcode/status', methods=['GET'])
def get_all_transcode_status():
    """Get status of all transcode jobs"""
    try:
        active = []
        completed = []
        failed = []
        
        for transcode_id, job in active_transcodes.items():
            # Use stored duration for completed/failed jobs, otherwise calculate current elapsed
            if job['status'] in ['completed', 'failed']:
                elapsed = job.get('duration', time.time() - job['startTime'])
            else:
                elapsed = time.time() - job['startTime']
            
            job_info = {
                'id': job['id'],
                'inputFile': Path(job['inputFile']).name,
                'option': job['option'],
                'status': job['status'],
                'progress': job.get('progress', 0),
                'startTime': job['startTime'],
                'elapsed': elapsed
            }
            
            if job['status'] == 'completed':
                job_info['duration'] = job.get('duration', elapsed)
                job_info['outputFile'] = Path(job.get('outputFile', '')).name if job.get('outputFile') else None
                job_info['outputSize'] = job.get('outputSize')
                completed.append(job_info)
            elif job['status'] == 'failed':
                job_info['duration'] = job.get('duration', elapsed)
                job_info['error'] = job.get('error')
                failed.append(job_info)
            else:
                active.append(job_info)
        
        return jsonify({
            'active': active,
            'completed': completed,
            'failed': failed,
            'total': len(active_transcodes)
        })
        
    except Exception as e:
        logger.error(f"Error getting all transcode status: {e}")
        return jsonify({'error': str(e)}), 500


@utils_bp.route('/api/transcode/<transcode_id>', methods=['DELETE'])
def cancel_transcode(transcode_id):
    """Cancel a running transcode job"""
    try:
        if transcode_id not in active_transcodes:
            return jsonify({'error': 'Transcode job not found'}), 404
        
        job = active_transcodes[transcode_id]
        
        if job['status'] not in ['starting', 'running']:
            return jsonify({'error': f'Cannot cancel transcode with status: {job["status"]}'}), 400
        
        # Try to kill the process
        pid = job.get('pid')
        if pid:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
                logger.info(f"Sent SIGTERM to transcode process [{transcode_id}] pid={pid}")
            except ProcessLookupError:
                logger.warning(f"Process {pid} not found for transcode [{transcode_id}]")
            except Exception as e:
                logger.error(f"Error killing transcode process: {e}")
        
        job['status'] = 'cancelled'
        job['completedTime'] = time.time()
        job['duration'] = time.time() - job['startTime']
        
        broadcast('transcode_cancelled', {'id': transcode_id})
        
        return jsonify({
            'success': True,
            'message': f'Transcode {transcode_id} cancelled'
        })
        
    except Exception as e:
        logger.error(f"Error cancelling transcode: {e}")
        return jsonify({'error': str(e)}), 500


def _shape_klv_result(packets, video_path, include_raw):
    """Shape decoded KLV packets into the structure the utils UI consumes.

    ``web/static/utils.html`` reads ``data.extraction_info.{total_packets, format,
    include_raw_values, extraction_time}`` and ``data.packets[i].metadata``. Each parsed
    packet carries a ``tags`` map ``{name: {tag_id, value, raw_value, raw_length}}``; we
    expose a clean ``name -> value`` map as ``metadata`` by default, or the full per-tag
    detail (including raw hex) when ``include_raw`` is set.
    """
    shaped = []
    for p in packets:
        tags = p.get('tags') or {}
        if include_raw:
            metadata = tags
        else:
            metadata = {name: info.get('value') for name, info in tags.items()}
        shaped.append({
            'timestamp': p.get('timestamp'),
            'raw_size': p.get('raw_size'),
            'metadata': metadata,
        })
    return {
        'extraction_info': {
            'format': (video_path.suffix.lower().lstrip('.') or 'unknown'),
            'total_packets': len(shaped),
            'include_raw_values': bool(include_raw),
            'extraction_time': datetime.now(timezone.utc).isoformat(),
        },
        'packets': shaped,
    }


@utils_bp.route('/api/klv/extract', methods=['POST'])
def extract_klv():
    """
    Extract KLV metadata from video file
    
    Request body:
        {
            "videoFile": "stream_name/filename.mov",  // Relative to STREAMS_DIR
            "includeRaw": false  // Optional: include raw hex values
        }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'Request body required'}), 400
        
        video_file = data.get('videoFile')
        include_raw = data.get('includeRaw', False)
        
        if not video_file:
            return jsonify({'error': 'videoFile required'}), 400
        
        # Reject absolute paths — only allow paths relative to STREAMS_DIR
        if os.path.isabs(video_file):
            return jsonify({'error': 'Absolute paths not allowed. Use a path relative to the streams directory.'}), 400
        
        # Block path traversal sequences
        if '..' in video_file or '\x00' in video_file:
            return jsonify({'error': 'Invalid file path'}), 400
        
        video_file = os.path.join(STREAMS_DIR, video_file)
        
        # Verify resolved path is within STREAMS_DIR
        resolved = Path(video_file).resolve()
        if not resolved.is_relative_to(Path(STREAMS_DIR).resolve()):
            return jsonify({'error': 'Access denied'}), 403
        
        if not os.path.exists(video_file):
            return jsonify({'error': 'Video file not found'}), 404
        
        # Check file extension
        video_path = Path(video_file)
        if video_path.suffix.lower() not in ['.mov', '.mp4', '.ts', '.mkv']:
            return jsonify({'error': 'Unsupported file format. Supported formats: .mov, .mp4, .ts, .mkv'}), 400
        
        # Generate output JSON file path (different names for raw vs decoded)
        suffix = '_raw' if include_raw else ''
        json_file = video_path.parent / f"{video_path.stem}_extracted_klv{suffix}.json"
        
        # Check if extraction already exists
        if json_file.exists():
            logger.info(f"Loading existing KLV extraction: {json_file}")
            try:
                import json as json_lib
                with open(json_file, 'r') as f:
                    klv_data = json_lib.load(f)
                
                # Add total_packets to response if available
                total_packets = klv_data.get('extraction_info', {}).get('total_packets', 0)
                
                return jsonify({
                    'success': True,
                    'message': 'Loaded existing KLV extraction',
                    'data': klv_data,
                    'cached': True,
                    'total_packets': total_packets
                })
            except Exception as e:
                logger.warning(f"Failed to load existing extraction, re-extracting: {e}")
        
        # Extract the KLV data stream in-process and decode it. The previous code
        # shelled out to a utils/extract_video_klv.py helper that does not exist, so
        # *every* call failed with a 500 regardless of the file's contents; we now reuse
        # the shared STANAG 4609 framing/parser directly.
        logger.info(f"Extracting KLV from: {video_file} (include_raw={include_raw})")

        # 1) Demux the data (KLV) track to a temp file. A feed without MISB 0601
        #    metadata (e.g. a synthetic test pattern) has no data stream, so ffmpeg
        #    writes nothing / exits non-zero -- an expected "no KLV" outcome, not an error.
        temp_klv = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
        temp_klv.close()
        try:
            result = subprocess.run(
                ['ffmpeg', '-i', video_file, '-map', '0:d', '-c', 'copy',
                 '-f', 'data', '-y', temp_klv.name],
                capture_output=True, text=True, timeout=120,
            )
            has_stream = result.returncode == 0 and os.path.getsize(temp_klv.name) > 0
            klv_bytes = b''
            if has_stream:
                with open(temp_klv.name, 'rb') as fh:
                    klv_bytes = fh.read()
        finally:
            try:
                os.unlink(temp_klv.name)
            except OSError:
                pass

        # 2) No data track at all -> no KLV present (expected for non-UAS feeds).
        no_klv = {
            'success': False,
            'error': 'No KLV metadata found in video file. This file may not contain embedded STANAG 4609 KLV data.',
            'details': 'KLV metadata is typically found in MPEG-TS (.ts) files recorded from drone streams. MOV/MP4 files may not contain KLV unless specifically encoded with it.'
        }
        if not has_stream:
            # Log ffmpeg's stderr so a genuinely corrupt/unreadable file (vs a valid feed
            # that simply has no data track) stays diagnosable, even though both map to the
            # same "no KLV" 404 for the operator.
            logger.info("No KLV data stream in %s (ffmpeg rc=%s): %s", video_file,
                        result.returncode, (result.stderr or '').strip()[-500:])
            return jsonify(no_klv), 404

        # 3) Frame + decode packets (shared single source of truth in shared.klv).
        packets = parse_klv_packets(klv_bytes)
        if not packets:
            logger.info(f"KLV data stream present but no decodable packets in: {video_file}")
            return jsonify(no_klv), 404

        # 4) Shape for the UI and cache the JSON next to the video so repeat calls hit
        #    the fast 'cached' path above.
        klv_data = _shape_klv_result(packets, video_path, include_raw)
        try:
            import json as json_lib
            with open(json_file, 'w') as f:
                json_lib.dump(klv_data, f, indent=2, default=str)
        except OSError as e:
            logger.warning(f"Could not cache KLV extraction to {json_file}: {e}")

        total_packets = klv_data['extraction_info']['total_packets']
        logger.info(f"KLV extraction successful: {total_packets} packets")
        return jsonify({
            'success': True,
            'message': 'KLV extracted successfully',
            'data': klv_data,
            'cached': False,
            'total_packets': total_packets
        })
        
    except subprocess.TimeoutExpired:
        logger.error(f"KLV extraction timeout for: {video_file}")
        return jsonify({'error': 'KLV extraction timed out. The file may be too large or complex.'}), 500
    except Exception as e:
        logger.error(f"Error extracting KLV: {e}")
        return jsonify({'error': 'An error occurred during KLV extraction.'}), 500
