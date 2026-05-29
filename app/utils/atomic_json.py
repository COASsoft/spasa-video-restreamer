"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Atomic JSON read/write helpers.

Writing JSON with a plain ``open('w')`` + ``json.dump`` is not crash-safe: if the
process dies mid-write, the file is left truncated/corrupt and the next read
fails (losing API keys, pull sources, settings, etc.). These helpers write to a
temp file in the same directory, fsync it, then atomically ``os.replace()`` it
over the target, so a reader always sees either the complete old file or the
complete new one. Files are created with 0600 by default (least privilege).
"""
import json
import logging
import os
import tempfile
from typing import Any

logger = logging.getLogger(__name__)


def write_json_atomic(path: str, data: Any, *, indent: int = 2, mode: int = 0o600) -> None:
    """Atomically write ``data`` as JSON to ``path``.

    Writes to a temp file in the same directory, fsyncs it, then replaces the
    destination atomically. Raises on failure so the caller can decide how to
    handle it (most callers log a warning and continue).
    """
    directory = os.path.dirname(os.path.abspath(path)) or '.'
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix='.tmp-', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def read_json(path: str, default: Any = None) -> Any:
    """Read JSON from ``path``; return ``default`` if missing or unparseable."""
    try:
        if not os.path.isfile(path):
            return default
        with open(path, 'r') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.warning("Could not read JSON from %s: %s", path, e)
        return default
