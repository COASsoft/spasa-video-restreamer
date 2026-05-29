"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Pytest configuration: make the suite hermetic and runnable with a plain
``pytest``, without a running MediaMTX or the production ``/opt/app`` paths.

These defaults are set BEFORE the ``app`` package is imported (conftest is
loaded first), and only when not already provided, so CI / Docker can override:
- threading SocketIO mode (eventlet is not needed for tests and is not
  importable on every interpreter, e.g. Python 3.13+),
- all writable directories under a throwaway temp tree,
- a non-default admin password + fixed secret key so fail-closed startup
  checks are satisfied in tests.
"""
import os
import tempfile

os.environ.setdefault('SOCKETIO_ASYNC_MODE', 'threading')

_tmp = tempfile.mkdtemp(prefix='tvr-test-')
for _var, _sub in (
    ('DATA_DIR', 'data'),
    ('LOGS_DIR', 'logs'),
    ('STREAMS_DIR', 'streams'),
    ('SHARED_VIDEOS_DIR', 'shared'),
    ('HLS_OUTPUT_DIR', 'hls'),
    ('FFMPEG_LOG_DIR', 'logs/ffmpeg'),
):
    os.environ.setdefault(_var, os.path.join(_tmp, _sub))

os.environ.setdefault('ADMIN_PASSWORD', 'pytest-not-default')
os.environ.setdefault('SECRET_KEY', 'pytest-secret-key')
