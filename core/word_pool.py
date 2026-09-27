"""Long-lived background Microsoft Word instances (core/word_pool.py).

Starting a hidden Word for every document costs ~2s each time. The app
instead starts a small pool of hidden Word instances when it opens and
reuses them for every conversion:

  * Each instance is owned by its own thread (Word's COM objects may
    only be used from the thread that created them). All threads take
    jobs from one shared queue, so two documents are laid out at the
    same time, each by its own Word.
  * A Word instance that crashes or is killed is replaced by a fresh
    one, and the interrupted document is retried once.
  * A conversion that exceeds its timeout has its Word process
    terminated (so a hung Word can't block the pool forever); the
    thread then starts a new instance for the next job.
  * ``shutdown()`` quits every instance; it is also registered with
    ``atexit`` so Word is not left running when Python exits. Each
    instance is also put in a Windows job object that terminates it
    when this process ends in any other way (crash, Task Manager).

No Qt imports. Only for the process that owns the GUI -- worker
processes fall back to one-shot conversion in core/office_convert.py.
"""

from __future__ import annotations

import atexit
import csv
import io
import logging
import os
import queue
import signal
import subprocess
import threading
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# COM HRESULTs meaning the Word process is gone.
_DEAD_SERVER_HRESULTS = {
    -2147023174,  # RPC_S_SERVER_UNAVAILABLE
    -2147023170,  # RPC_S_CALL_FAILED
    -2147417848,  # RPC_E_DISCONNECTED
    -2147418111,  # RPC_E_CALL_REJECTED
}

_START_LOCK = threading.Lock()

_kill_on_exit_job = None


def _tie_to_this_process(pid: int) -> None:
    """Make Windows terminate Word ``pid`` when this process ends, however
    it ends: the job object's only handle is ours, and closing it (which
    the OS does on exit) kills everything assigned to the job."""
    global _kill_on_exit_job
    try:
        import win32api
        import win32con
        import win32job

        if _kill_on_exit_job is None:
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation
            )
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            )
            win32job.SetInformationJobObject(
                job, win32job.JobObjectExtendedLimitInformation, info
            )
            _kill_on_exit_job = job
        handle = win32api.OpenProcess(
            win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid
        )
        try:
            win32job.AssignProcessToJobObject(_kill_on_exit_job, handle)
        finally:
            win32api.CloseHandle(handle)
    except Exception:  # noqa: BLE001 -- best effort; shutdown() still quits Word
        logger.warning("Could not tie background Word (pid %s) to the app's lifetime", pid)


@dataclass
class _Job:
    source: str
    output: str
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None
    abandoned: bool = False
    host: "_WordHost | None" = None


def _word_pids() -> set[int]:
    try:
        listing = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WINWORD.EXE", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids = set()
    for row in csv.reader(io.StringIO(listing)):
        if len(row) > 1 and row[1].isdigit():
            pids.add(int(row[1]))
    return pids


def _is_dead_server(exc: BaseException) -> bool:
    return bool(getattr(exc, "args", None)) and exc.args[0] in _DEAD_SERVER_HRESULTS


class _WordHost:
    """One hidden Word instance. Used only from its owning thread,
    except ``kill()``."""

    def __init__(self) -> None:
        self.app = None
        self.pid: int | None = None

    def ensure_started(self) -> None:
        if self.app is not None:
            return
        import win32com.client

        # Serialized, so the one new WINWORD.EXE that appears is ours.
        with _START_LOCK:
            before = _word_pids()
            app = win32com.client.DispatchEx("Word.Application")
            new = _word_pids() - before
        app.Visible = False
        app.DisplayAlerts = 0  # wdAlertsNone
        app.ScreenUpdating = False
        self.app = app
        self.pid = new.pop() if len(new) == 1 else None
        if self.pid is not None:
            _tie_to_this_process(self.pid)
        logger.info("Background Word started (pid %s)", self.pid)

    def convert(self, source: str, output: str, job: _Job) -> None:
        from core.office_convert import export_with_word

        for attempt in (1, 2):
            self.ensure_started()
            try:
                export_with_word(self.app, source, output)
                return
            except Exception as exc:
                if not _is_dead_server(exc) or attempt == 2 or job.abandoned:
                    if not _is_dead_server(exc):
                        raise
                    self.discard()
                    from core.office_convert import ConversionError

                    raise ConversionError(
                        f"Microsoft Word stopped while laying out '{os.path.basename(source)}'."
                    ) from exc
                logger.warning("Background Word died; restarting it")
                self.discard()

    def discard(self) -> None:
        self.app = None
        self.pid = None

    def quit(self) -> None:
        if self.app is not None:
            try:
                self.app.Quit(False)
            except Exception:  # noqa: BLE001 -- already gone
                pass
        self.discard()

    def kill(self) -> None:
        """Terminate this Word process (from any thread)."""
        pid = self.pid
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)  # TerminateProcess on Windows
                logger.warning("Terminated hung background Word (pid %s)", pid)
            except OSError:
                pass


class WordPool:
    def __init__(self, size: int = 2) -> None:
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._closed = False
        self._hosts: list[_WordHost] = []
        self._threads = [
            threading.Thread(target=self._run, name=f"word-host-{i}", daemon=True)
            for i in range(size)
        ]
        for thread in self._threads:
            thread.start()

    def convert(self, source: str, output: str, timeout: float) -> None:
        """Lay out ``source`` into the PDF ``output``; blocks until done.

        Raises:
            core.office_convert.ConversionError: Word failed or timed out.
        """
        from core.office_convert import ConversionError

        if self._closed:
            raise ConversionError("Word is shutting down.")
        job = _Job(source, output)
        self._jobs.put(job)
        if not job.done.wait(timeout):
            job.abandoned = True
            if job.host is not None:
                job.host.kill()
            raise ConversionError(
                f"Microsoft Word did not finish laying out "
                f"'{os.path.basename(source)}' within {int(timeout)} seconds."
            )
        if job.error is not None:
            raise job.error

    def shutdown(self, timeout: float = 5.0) -> None:
        """Quit every instance; one still busy after ``timeout`` seconds
        (mid-way through a long document) is terminated instead."""
        if self._closed:
            return
        self._closed = True
        for _ in self._threads:
            self._jobs.put(None)
        for thread in self._threads:
            thread.join(timeout)
        for host in self._hosts:
            if host.app is not None:
                host.kill()

    def _run(self) -> None:
        import pythoncom

        pythoncom.CoInitialize()
        host = _WordHost()
        self._hosts.append(host)
        try:
            try:
                host.ensure_started()  # warm up before the first job arrives
            except Exception:  # noqa: BLE001 -- retried when a job needs it
                logger.exception("Could not start background Word")
            while True:
                job = self._jobs.get()
                if job is None:
                    break
                if job.abandoned:
                    continue
                job.host = host
                try:
                    host.convert(job.source, job.output, job)
                except BaseException as exc:  # noqa: BLE001 -- handed to the caller
                    job.error = exc
                finally:
                    job.host = None
                    job.done.set()
        finally:
            host.quit()
            pythoncom.CoUninitialize()


_pool: WordPool | None = None
_pool_lock = threading.Lock()


def start(size: int = 2) -> None:
    """Start the shared pool (no-op if already running)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = WordPool(size)


def active_pool() -> WordPool | None:
    return _pool


def shutdown() -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown()


atexit.register(shutdown)
