"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Stream Standby Service

Tracks streams that have been seen and keeps them in a "standby" state
after the publisher disconnects, so they reappear immediately when the
publisher reconnects.

Persistence: standby_streams.json in DATA_DIR
Settings: standby_enabled, standby_timeout_minutes in server settings
"""
import os
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Dict, Optional

from app.config import DATA_DIR
from app.utils.atomic_json import write_json_atomic, read_json
from app.websocket.broadcast import broadcast

logger = logging.getLogger(__name__)

_STANDBY_FILE = os.path.join(DATA_DIR, 'standby_streams.json')


class StandbyManager:
    """Manages stream standby state."""

    def __init__(self):
        self._streams: Dict[str, dict] = {}
        self._lock = threading.Lock()
        self._load()
        self._cleanup_thread = threading.Thread(
            target=self._timeout_loop, daemon=True, name='standby-timeout'
        )
        self._cleanup_thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stream_seen(self, stream_name: str, source_info: Optional[dict] = None):
        """Called when a publisher connects / stream becomes ready."""
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            if stream_name in self._streams:
                self._streams[stream_name]['last_seen'] = now
                self._streams[stream_name]['status'] = 'active'
                self._streams[stream_name]['disconnect_time'] = None
            else:
                self._streams[stream_name] = {
                    'name': stream_name,
                    'first_seen': now,
                    'last_seen': now,
                    'status': 'active',       # active | standby
                    'disconnect_time': None,
                    'source_info': source_info or {},
                }
            self._persist()
        broadcast('stream_standby_update', self.list_all())

    def stream_gone(self, stream_name: str):
        """Called when a publisher disconnects / stream is no longer ready."""
        with self._lock:
            if stream_name in self._streams:
                self._streams[stream_name]['status'] = 'standby'
                self._streams[stream_name]['disconnect_time'] = datetime.now(timezone.utc).isoformat()
                self._persist()
        broadcast('stream_standby_update', self.list_all())

    def remove_stream(self, stream_name: str) -> bool:
        """Manually remove a stream from standby (user action)."""
        with self._lock:
            removed = self._streams.pop(stream_name, None)
            if removed:
                self._persist()
        if removed:
            broadcast('stream_standby_update', self.list_all())
        return removed is not None

    def list_all(self) -> list:
        """Return all tracked streams (active + standby)."""
        with self._lock:
            return list(self._streams.values())

    def get_standby_streams(self) -> list:
        """Return only standby streams."""
        with self._lock:
            return [s for s in self._streams.values() if s['status'] == 'standby']

    def get_active_streams(self) -> list:
        """Return only active streams."""
        with self._lock:
            return [s for s in self._streams.values() if s['status'] == 'active']

    def is_standby(self, stream_name: str) -> bool:
        with self._lock:
            s = self._streams.get(stream_name)
            return s is not None and s['status'] == 'standby'

    def clear_all(self):
        """Remove all standby entries."""
        with self._lock:
            self._streams.clear()
            self._persist()
        broadcast('stream_standby_update', [])

    # ------------------------------------------------------------------
    # Timeout sweep
    # ------------------------------------------------------------------

    def _timeout_loop(self):
        """Periodically remove standby streams whose timeout has expired."""
        # Delay first run to allow the app to finish initializing all imports
        time.sleep(5)
        while True:
            try:
                self._check_timeouts()
            except Exception as e:
                logger.error(f"Standby timeout check error: {e}")
            time.sleep(30)  # check every 30 seconds

    def _check_timeouts(self):
        try:
            from app.api.settings import server_settings
        except (ImportError, AttributeError):
            # App not fully initialized yet (race with module-level thread start)
            from app.config import SERVER_SETTINGS as server_settings
        enabled = server_settings.get('standby_enabled', False)
        timeout_min = server_settings.get('standby_timeout_minutes', 60)

        if not enabled:
            return

        if timeout_min <= 0:
            return  # 0 = infinite / no timeout

        now = time.time()
        expired = []

        with self._lock:
            for name, info in list(self._streams.items()):
                if info['status'] != 'standby':
                    continue
                dt = info.get('disconnect_time')
                if not dt:
                    continue
                disc_ts = datetime.fromisoformat(dt).timestamp()
                if now - disc_ts > timeout_min * 60:
                    expired.append(name)

            if expired:
                for name in expired:
                    del self._streams[name]
                    logger.info(f"Standby timeout expired for stream: {name}")
                self._persist()

        if expired:
            broadcast('stream_standby_update', self.list_all())

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self):
        """Write current state to disk (caller must hold lock)."""
        try:
            write_json_atomic(_STANDBY_FILE, self._streams)
        except Exception as e:
            logger.error(f"Error persisting standby streams: {e}")

    def _load(self):
        """Load state from disk."""
        loaded = read_json(_STANDBY_FILE, default=None)
        self._streams = loaded if isinstance(loaded, dict) else {}
        if loaded:
            logger.info(f"Loaded {len(self._streams)} standby stream entries")


# Module singleton
standby_manager = StandbyManager()
