#!/usr/bin/env python3
"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

SRT Stream Buffer & Recovery
Continuously captures SRT stream, buffers it, and attempts reconnection on failure
"""
import os
import threading
import time
import logging
import signal
import sys
from pathlib import Path
from collections import deque

logger = logging.getLogger(__name__)

def _get_srt_settings():
    """Read SRT settings from server_settings (lazy import to avoid circular deps)."""
    try:
        from app.api.settings import server_settings
        return {
            'auto_reconnect': server_settings.get('srt_auto_reconnect', True),
            'reconnect_delay': server_settings.get('srt_reconnect_delay', 2),
            'max_buffer_seconds': server_settings.get('srt_max_buffer_seconds', 30),
        }
    except Exception:
        return {'auto_reconnect': True, 'reconnect_delay': 2, 'max_buffer_seconds': 30}


class SRTStreamBuffer:
    def __init__(self, stream_name, max_buffer_size=30):
        self.stream_name = stream_name
        self.max_buffer_size = max_buffer_size  # seconds of video to buffer
        self.buffer = deque(maxlen=max_buffer_size * 30)  # ~30fps assumption
        self.is_running = False
        self.process = None
        self.last_packet_time = time.time()
        self.reconnect_delay = 2  # Start with 2 second delay
        self.max_reconnect_delay = 30
        self.total_reconnects = 0

    def start(self):
        """Start buffering the stream"""
        # Read buffer size from settings
        srt_cfg = _get_srt_settings()
        self.max_buffer_size = srt_cfg['max_buffer_seconds']
        self.buffer = deque(maxlen=self.max_buffer_size * 30)
        self.reconnect_delay = srt_cfg['reconnect_delay']
        self.is_running = True
        threading.Thread(target=self._capture_loop, daemon=True).start()
        threading.Thread(target=self._monitor_loop, daemon=True).start()
        logger.info(f"Started SRT buffer for stream: {self.stream_name}")

    def stop(self):
        """Stop buffering"""
        self.is_running = False
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                self.process.kill()
        logger.info(f"Stopped SRT buffer for stream: {self.stream_name}")

    def _capture_loop(self):
        """Continuously capture from SRT with auto-reconnect"""
        while self.is_running:
            try:
                # Use environment variable for SRT host (default to localhost for local development)
                srt_host = os.environ.get('SRT_HOST', 'localhost')
                srt_port = os.environ.get('SRT_PORT', '8890')
                streams_dir = os.environ.get('STREAMS_DIR', '/opt/app/streams')

                srt_url = f"srt://{srt_host}:{srt_port}?streamid=read:{self.stream_name}&latency=2000&peeridletimeo=30000"

                # Use ffmpeg to capture with aggressive buffering
                # Note: -reconnect options only work with HTTP, not SRT
                # SRT has its own reconnection via peeridletimeo parameter
                cmd = [
                    'ffmpeg',
                    '-loglevel', 'warning',
                    '-re',  # Read at native frame rate
                    '-fflags', '+genpts+discardcorrupt+nobuffer',
                    '-analyzeduration', '1000000',
                    '-probesize', '1000000',
                    '-i', srt_url,
                    '-c', 'copy',
                    '-f', 'mpegts',
                    '-use_wallclock_as_timestamps', '1',
                    f'{streams_dir}/{self.stream_name}_buffer.ts'
                ]

                logger.info(f"Starting capture for {self.stream_name} (reconnect #{self.total_reconnects})")

                # Spawn under the shared supervisor: stderr is captured to a log
                # file (a PIPE's ~64KB OS buffer fills with FFmpeg progress within
                # ~200s and would deadlock the relay), the child runs in its own
                # session for clean teardown, and it is tracked for startup orphan
                # reconciliation. Imported lazily to avoid an import cycle
                # (app.config imports this module while it is still initialising).
                from app.services.process import ManagedProcess
                log_dir = os.environ.get('FFMPEG_LOG_DIR', '/opt/app/logs/ffmpeg')
                mp = ManagedProcess(f'srt-{self.stream_name}', cmd, log_dir,
                                    label=f'srt:{self.stream_name}')
                if not mp.start():
                    logger.error(f"Failed to start SRT capture for {self.stream_name}")
                    if not self.is_running:
                        break
                    time.sleep(self.reconnect_delay)
                    continue
                self.process = mp

                # Wait for exit; stderr lives in the log file.
                return_code = mp.wait()
                stderr_tail = mp.tail_stderr(10)
                mp.close()

                # Log last lines of stderr on failure
                if return_code not in (0, None) and stderr_tail:
                    logger.warning(f"SRT {self.stream_name} ffmpeg stderr (exit code {return_code}):")
                    for line in stderr_tail:
                        logger.warning(f"  ffmpeg[srt:{self.stream_name}]: {line}")

                if not self.is_running:
                    break

                # Check if auto-reconnect is enabled
                srt_cfg = _get_srt_settings()
                if not srt_cfg['auto_reconnect']:
                    logger.info(f"SRT auto-reconnect disabled, stopping {self.stream_name}")
                    break

                self.reconnect_delay = max(self.reconnect_delay, srt_cfg['reconnect_delay'])
                logger.warning(f"Stream {self.stream_name} disconnected (code: {return_code}), reconnecting in {self.reconnect_delay}s...")
                self.total_reconnects += 1
                time.sleep(self.reconnect_delay)

                # Exponential backoff
                self.reconnect_delay = min(self.reconnect_delay * 1.5, self.max_reconnect_delay)

            except Exception as e:
                logger.error(f"Error in capture loop: {e}")
                if self.is_running:
                    time.sleep(self.reconnect_delay)

    def _monitor_loop(self):
        """Monitor stream health and trigger recovery if needed"""
        streams_dir = os.environ.get('STREAMS_DIR', '/opt/app/streams')

        while self.is_running:
            time.sleep(5)

            # Check if we're receiving data
            buffer_file = Path(f'{streams_dir}/{self.stream_name}_buffer.ts')
            if buffer_file.exists():
                file_age = time.time() - buffer_file.stat().st_mtime

                if file_age > 10:  # No new data in 10 seconds
                    logger.warning(f"Stream {self.stream_name} appears stalled (no data for {file_age:.1f}s)")
                    if self.process:
                        logger.info("Forcing reconnection...")
                        try:
                            self.process.terminate()
                        except ProcessLookupError:
                            pass
                else:
                    # Reset reconnect delay on successful streaming
                    self.reconnect_delay = 2


class SRTBufferManager:
    """Manages multiple buffered SRT streams"""
    def __init__(self):
        self.streams = {}
        self.is_running = False

    def add_stream(self, stream_name):
        """Add a stream to buffer"""
        if stream_name not in self.streams:
            buffer = SRTStreamBuffer(stream_name)
            buffer.start()
            self.streams[stream_name] = buffer
            logger.info(f"Added buffered stream: {stream_name}")
            return True
        return False

    def remove_stream(self, stream_name):
        """Remove a buffered stream"""
        if stream_name in self.streams:
            self.streams[stream_name].stop()
            del self.streams[stream_name]
            logger.info(f"Removed buffered stream: {stream_name}")
            return True
        return False

    def stop_all(self):
        """Stop all buffered streams"""
        for stream_name in list(self.streams.keys()):
            self.remove_stream(stream_name)


# Global manager instance
_manager = None

def get_manager():
    global _manager
    if _manager is None:
        _manager = SRTBufferManager()
    return _manager


if __name__ == "__main__":
    # Test mode
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    stream_name = sys.argv[1] if len(sys.argv) > 1 else "uas"

    manager = get_manager()
    manager.add_stream(stream_name)

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        manager.stop_all()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info(f"Buffering stream '{stream_name}' with auto-reconnect. Press Ctrl+C to stop.")

    # Keep running
    while True:
        time.sleep(1)
