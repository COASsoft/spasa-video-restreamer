"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Startup reconciliation of orphaned FFmpeg processes.

The runtime state (active_recordings / active_pull_streams / ABR) lives only in
memory, so if the service crashes its FFmpeg children keep running — still
re-publishing to MediaMTX or writing files — with no handle in the next instance.

To recover deterministically we persist a tiny PID registry as each supervised
``ManagedProcess`` starts and stops (via hooks installed into that module). On
startup ``reconcile_orphans()`` kills any registered PID that is *still alive and
still looks like the FFmpeg we launched* (PID-reuse guard), then clears the
registry so the fresh instance starts from a clean slate.

The process-inspection primitives are module-level callables so tests can inject
fakes without spawning anything.
"""
import logging
import os
import signal
import subprocess
import threading

from app.config import DATA_DIR
from app.utils.atomic_json import write_json_atomic, read_json

logger = logging.getLogger(__name__)

_REGISTRY_FILE = os.path.join(DATA_DIR, 'process_registry.json')
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Registry persistence (called from the ManagedProcess spawn/exit hooks)
# ---------------------------------------------------------------------------

def _load() -> dict:
    data = read_json(_REGISTRY_FILE, default={})
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    try:
        write_json_atomic(_REGISTRY_FILE, data)
    except Exception as e:  # registry is best-effort; never crash a spawn/exit
        logger.debug("could not persist process registry: %s", e)


def register(pid, label, cmd) -> None:
    """Record a freshly-spawned supervised process."""
    if not pid:
        return
    with _lock:
        data = _load()
        data[str(pid)] = {
            'label': label or '',
            'cmd': ' '.join(str(c) for c in cmd) if cmd else '',
        }
        _save(data)


def unregister(pid) -> None:
    """Drop a process from the registry once it has been torn down."""
    if not pid:
        return
    with _lock:
        data = _load()
        if data.pop(str(pid), None) is not None:
            _save(data)


def install_hooks() -> None:
    """Wire register/unregister into ManagedProcess (call once at startup)."""
    from app.services import process
    process.set_registry_hooks(on_spawn=register, on_exit=unregister)


# ---------------------------------------------------------------------------
# Process inspection — module-level so tests can monkeypatch them
# ---------------------------------------------------------------------------

def is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not signallable by us
    except OSError:
        return False


def cmdline(pid: int) -> str:
    """Best-effort command line for ``pid`` ('' if unknown).

    Linux ``/proc`` first (cheap, exact); ``ps`` fallback for macOS/dev.
    """
    try:
        with open(f'/proc/{pid}/cmdline', 'rb') as f:
            return f.read().replace(b'\x00', b' ').decode('utf-8', 'ignore').strip()
    except OSError:
        pass
    try:
        out = subprocess.run(['ps', '-p', str(pid), '-o', 'command='],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ''


def kill(pid: int) -> None:
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _cmdlines_match(recorded: str, live: str) -> bool:
    """True if the live cmdline plausibly belongs to our recorded command.

    Guards against PID reuse: we match on the recorded command's distinctive
    tail (the output target — a file path or rtsp:// URL), which uniquely
    identifies the relay/recording even if argv ordering differs.
    """
    parts = recorded.split()
    if not parts:
        return False
    return parts[-1] in live


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------

def reconcile_orphans(*, registry=None, _is_alive=None, _cmdline=None, _kill=None) -> list:
    """Kill leftover FFmpeg from a previous (crashed) instance; reset the registry.

    Args (all optional, for testing): ``registry`` overrides the on-disk data;
    ``_is_alive``/``_cmdline``/``_kill`` override the inspection primitives.
    Returns the list of reaped PIDs.
    """
    alive = _is_alive or is_alive
    cmd_of = _cmdline or cmdline
    do_kill = _kill or kill

    with _lock:
        data = registry if registry is not None else _load()
        reaped = []
        for pid_str, info in list((data or {}).items()):
            try:
                pid = int(pid_str)
            except (TypeError, ValueError):
                continue
            if not alive(pid):
                continue
            live = cmd_of(pid) or ''
            if 'ffmpeg' not in live.lower():
                continue  # PID was reused by something unrelated
            recorded = (info or {}).get('cmd', '')
            if recorded and not _cmdlines_match(recorded, live):
                continue
            do_kill(pid)
            reaped.append(pid)
            logger.warning("Reconciliation: killed orphaned FFmpeg pid=%s (%s)",
                           pid, (info or {}).get('label', ''))
        # Fresh start: clear the registry. Live processes (re)register on spawn.
        _save({})

    if reaped:
        logger.warning("Reconciliation reaped %d orphaned FFmpeg process(es): %s",
                       len(reaped), reaped)
    else:
        logger.info("Reconciliation: no orphaned FFmpeg processes found")
    return reaped
