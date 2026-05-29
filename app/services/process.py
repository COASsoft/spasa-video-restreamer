"""
This material is based upon work supported by the United States Air Force under contract number FA8750-24-S-B079 (Prime Contractor Smart Information Flow Technologies (SIFT)).  Any opinions, findings and conclusions or recommendations expressed in this material are those of the author(s) and do not necessarily reflect the views of the United States Air Force.
 Copyright (c) 2026 RTX BBN Technologies. Licensed to US Government with unlimited rights.

This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, or (at your option) any later version.
This is distributed in the hope that it will be useful, but without any warranty, without even the implied warranty of merchantability or fitness for a particular purpose.  See the GNU General Public License for more details. https://www.gnu.org/licenses/

Unified external-process supervision (FFmpeg & friends).

This consolidates the process-lifecycle pattern that was proven in
``app/services/abr.py`` and was previously re-implemented (more weakly, often
discarding stderr to DEVNULL) across auto-record, pull streams, the SRT buffer
and test patterns. Key properties:

- stderr is captured to a per-process log file (never DEVNULL), so failures are
  diagnosable; ``tail_stderr()`` returns the last lines on demand;
- each process runs in its own session (``start_new_session=True``) so it can be
  signalled/orphaned cleanly;
- termination never blocks indefinitely on an unkillable D-state process
  (``kill()`` is fire-and-forget; ``stop()`` is bounded);
- ``supervise()`` runs a process and restarts it on exit/stall with capped
  exponential backoff until a stop predicate is satisfied.

The module is intentionally free of Flask/app-config imports so it is trivially
unit-testable and reusable.
"""
import logging
import os
import subprocess
import time

logger = logging.getLogger(__name__)


# Optional registry hooks, set by app.services.reconcile.install_hooks(). Kept as
# plain module-level callables so this module stays free of app/config imports
# (and trivially unit-testable). Signatures:
#   _spawn_hook(pid: int, label: str, cmd: list)  -- after a successful spawn
#   _exit_hook(pid: int)                          -- when the process is torn down
_spawn_hook = None
_exit_hook = None


def set_registry_hooks(on_spawn=None, on_exit=None):
    """Install (or clear) the spawn/exit hooks used for orphan reconciliation."""
    global _spawn_hook, _exit_hook
    _spawn_hook = on_spawn
    _exit_hook = on_exit


def open_stderr_log(log_dir: str, name: str):
    """Open a per-process stderr log file. Returns (file_or_DEVNULL, path_or_None)."""
    try:
        os.makedirs(log_dir, exist_ok=True)
        path = os.path.join(log_dir, f'{name}.log')
        return open(path, 'w'), path
    except OSError as e:
        logger.error("Could not open stderr log for %s in %s: %s", name, log_dir, e)
        return subprocess.DEVNULL, None


def tail_file(path, n: int = 15) -> list:
    """Return the last ``n`` non-blank lines of a file (best-effort)."""
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, 'r') as f:
            lines = f.readlines()
    except OSError:
        return []
    return [ln.rstrip() for ln in lines[-n:] if ln.strip()]


class ManagedProcess:
    """A supervised external process with captured stderr and safe teardown.

    Exposes the subset of ``subprocess.Popen`` methods that existing call sites
    use (``poll``/``wait``/``terminate``/``kill``/``pid``) so it can be a drop-in
    replacement, while adding stderr capture and a bounded ``stop()``.
    """

    def __init__(self, name: str, cmd, log_dir: str, label: str = None,
                 stdin_pipe: bool = False):
        self.name = name
        self.cmd = list(cmd)
        self.log_dir = log_dir
        self.label = label or name
        # stdin_pipe=True keeps an stdin pipe open so callers can send FFmpeg a
        # graceful 'q' (needed e.g. for recordings to finalise the MOV moov atom).
        self._stdin_pipe = stdin_pipe
        self._proc = None
        self._stderr_file = None
        self._stderr_path = None
        self.started_at = None

    def start(self) -> bool:
        """Spawn the process. Returns True on success, False on failure."""
        self._stderr_file, self._stderr_path = open_stderr_log(self.log_dir, self.name)
        try:
            self._proc = subprocess.Popen(
                self.cmd,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr_file,
                stdin=subprocess.PIPE if self._stdin_pipe else subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, ValueError) as e:
            logger.error("ManagedProcess %s failed to start: %s", self.label, e)
            self._close_log()
            self._proc = None
            return False
        self.started_at = time.monotonic()
        logger.info("ManagedProcess %s started (pid=%s)", self.label, self._proc.pid)
        if _spawn_hook is not None:
            try:
                _spawn_hook(self._proc.pid, self.label, self.cmd)
            except Exception as e:  # registry must never break a spawn
                logger.debug("spawn hook error for %s: %s", self.label, e)
        return True

    @property
    def pid(self):
        return self._proc.pid if self._proc else None

    @property
    def returncode(self):
        return self._proc.returncode if self._proc else None

    def request_graceful_quit(self) -> bool:
        """Ask FFmpeg to quit cleanly by writing 'q' to stdin, then closing it.

        Requires ``stdin_pipe=True``. Returns True if the signal was sent. This
        lets the process finalise its output (e.g. write the MOV moov atom)
        before exiting, which a SIGTERM/SIGKILL would not allow.
        """
        if not self._stdin_pipe or self._proc is None or self._proc.stdin is None:
            return False
        if self._proc.poll() is not None:
            return False
        try:
            self._proc.stdin.write(b'q')
            self._proc.stdin.flush()
            self._proc.stdin.close()
            return True
        except (OSError, ValueError):
            return False

    def poll(self):
        return self._proc.poll() if self._proc else None

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def wait(self, timeout=None):
        return self._proc.wait(timeout=timeout) if self._proc else None

    def terminate(self):
        if self.is_alive():
            try:
                self._proc.terminate()
            except OSError:
                pass

    def kill(self):
        """Fire-and-forget SIGKILL. Never waits (safe against D-state)."""
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def stop(self, term_timeout: float = 3.0):
        """Graceful stop: SIGTERM, wait up to ``term_timeout``, then SIGKILL.

        Bounded — never blocks longer than ``term_timeout``. Closes the log file.
        """
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            self._close_log()
            return
        try:
            self._proc.terminate()
        except OSError:
            pass
        try:
            self._proc.wait(timeout=term_timeout)
        except subprocess.TimeoutExpired:
            self.kill()
        except OSError:
            pass
        self._close_log()

    def uptime(self) -> float:
        return (time.monotonic() - self.started_at) if self.started_at else 0.0

    def tail_stderr(self, n: int = 15) -> list:
        return tail_file(self._stderr_path, n)

    @property
    def stderr_path(self):
        return self._stderr_path

    def close(self):
        """Release the captured stderr file handle (idempotent).

        Use this when the process exited on its own (i.e. ``stop()`` was not
        called) so the log FD isn't held open until garbage collection.
        """
        self._close_log()

    def _close_log(self):
        f = self._stderr_file
        if f is not None and f is not subprocess.DEVNULL and hasattr(f, 'close'):
            try:
                f.close()
            except OSError:
                pass
        self._stderr_file = None
        # Drop the registry entry once we stop tracking the process. Guarded by
        # pid (None on a failed/never-started spawn → nothing was registered).
        if _exit_hook is not None and self._proc is not None:
            try:
                _exit_hook(self._proc.pid)
            except Exception as e:
                logger.debug("exit hook error for %s: %s", self.label, e)


def _sleep_unless_stop(should_stop, seconds: float, slice_s: float = 0.25) -> bool:
    """Sleep up to ``seconds`` in small slices. Return True if stop was requested."""
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if should_stop():
            return True
        time.sleep(min(slice_s, max(0.0, deadline - time.monotonic())))
    return should_stop()


def supervise(make_process, should_stop, *, is_stalled=None, on_restart=None,
              startup_grace: float = 15.0, check_interval: float = 5.0,
              base_backoff: float = 2.0, max_backoff: float = 30.0,
              healthy_after: float = 30.0):
    """Run and supervise a process until ``should_stop()`` returns True.

    Args:
        make_process: zero-arg callable returning a fresh ``ManagedProcess``.
        should_stop: zero-arg predicate; when True the loop stops the process and returns.
        is_stalled: optional ``(ManagedProcess) -> bool`` checked after ``startup_grace``.
        on_restart: optional ``(reason: str, stderr_tail: list)`` callback before each restart.
        startup_grace: seconds before stall detection applies after a (re)start.
        check_interval: seconds between liveness/stall checks.
        base_backoff/max_backoff: capped exponential backoff between restarts.
        healthy_after: if a process ran at least this long, backoff resets to base.
    """
    backoff = base_backoff
    while not should_stop():
        mp = make_process()
        if mp is None or not mp.start():
            if _sleep_unless_stop(should_stop, backoff):
                return
            backoff = min(backoff * 1.5, max_backoff)
            continue

        started = time.monotonic()
        reason = 'exited'
        while True:
            if _sleep_unless_stop(should_stop, check_interval):
                mp.stop()
                return
            if mp.poll() is not None:
                reason = 'exited'
                break
            if is_stalled is not None and (time.monotonic() - started) > startup_grace:
                try:
                    if is_stalled(mp):
                        reason = 'stall'
                        break
                except Exception as e:  # stall check must never crash the loop
                    logger.debug("stall check error for %s: %s", mp.label, e)

        ran_for = time.monotonic() - started
        tail = mp.tail_stderr()
        if reason == 'stall':
            mp.kill()
        else:
            mp._close_log()
        if on_restart is not None:
            try:
                on_restart(reason, tail)
            except Exception as e:
                logger.debug("on_restart callback error for %s: %s", mp.label, e)
        logger.warning("supervise %s: %s after %.0fs; stderr tail: %s",
                       mp.label, reason, ran_for, tail[-3:] if tail else [])

        # Reset backoff if the process stayed up a while; otherwise grow it.
        backoff = base_backoff if ran_for >= healthy_after else min(backoff * 1.5, max_backoff)
        if _sleep_unless_stop(should_stop, backoff):
            return
