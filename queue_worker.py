"""
queue_worker.py — In-process background job queue for the Media Summarizer.

Uses Python's built-in threading + queue modules (zero external dependencies).
A single daemon thread processes one job at a time, preserving the 15-RPM
Google Gemini rate limit contract without external infrastructure.

Architecture:
  - QueueManager  : Singleton that owns the worker thread and job registry.
  - Job           : Immutable job descriptor with live status tracking.
  - JobStatus     : State machine enum for job lifecycle.
"""

from __future__ import annotations

import logging
import queue
import threading
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Job lifecycle state machine
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    DONE       = "done"
    FAILED     = "failed"


# ---------------------------------------------------------------------------
# Job descriptor
# ---------------------------------------------------------------------------

@dataclass
class Job:
    """Represents a single queued analysis request."""
    job_id:      str
    fn:          Callable[..., Any]
    args:        tuple[Any, ...]
    kwargs:      Dict[str, Any]
    status:      JobStatus         = JobStatus.PENDING
    result:      Optional[Any]     = None
    error:       Optional[str]     = None
    position:    int               = 0          # 1-indexed queue depth; 0 = being processed
    _started_at: float             = 0.0        # monotonic timestamp when job started processing


# ---------------------------------------------------------------------------
# QueueManager — singleton with a daemon background worker
# ---------------------------------------------------------------------------

class QueueManager:
    """
    Thread-safe in-process job queue.

    Submit callable tasks via `submit(fn, *args, **kwargs)`.
    Poll job state via `get_job(job_id)`.
    A single daemon worker thread processes jobs sequentially (FIFO).
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[Job]   = queue.Queue()
        self._jobs:  Dict[str, Job]     = {}
        self._lock:  threading.Lock     = threading.Lock()

        # Daemon thread dies automatically when the main process exits —
        # no explicit shutdown needed.
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="QueueWorker",
            daemon=True,
        )
        self._thread.start()
        logger.info("QueueManager started (daemon worker thread)")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> str:
        """
        Enqueue a callable and return its unique job ID.

        Args:
            fn:     The callable to execute (e.g., gemini.summarise_stream).
            *args:  Positional arguments forwarded to fn.
            **kwargs: Keyword arguments forwarded to fn.

        Returns:
            job_id: A UUID string that callers can use to poll job state.
        """
        job_id = str(uuid.uuid4())
        with self._lock:
            # Read qsize INSIDE the lock so the position is accurate
            position = self._queue.qsize() + 1
            job = Job(
                job_id=job_id,
                fn=fn,
                args=args,
                kwargs=kwargs,
                position=position,
            )
            self._jobs[job_id] = job
            self._queue.put(job)
        logger.info("Job %s queued at position %d", job_id, position)
        return job_id

    def get_job(self, job_id: str) -> Optional[Job]:
        """Return the current Job state, or None if the job_id is unknown."""
        with self._lock:
            return self._jobs.get(job_id)

    def queue_depth(self) -> int:
        """Return the number of jobs currently waiting (not counting the active job)."""
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # Internal worker
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """
        Runs forever in the daemon thread.
        Picks one job at a time, executes it, stores the result/error,
        then updates queue positions for remaining pending jobs.
        Evicts DONE/FAILED jobs older than JOB_TTL_SECONDS to prevent
        unbounded memory growth.
        """
        import time as _time

        JOB_TTL_SECONDS = 600  # 10 minutes

        while True:
            job = self._queue.get()   # blocks until a job is available

            job._started_at = _time.monotonic()

            with self._lock:
                job.status   = JobStatus.PROCESSING
                job.position = 0      # position 0 = actively running

                # Recompute 1-indexed positions for remaining pending jobs
                pending = list(self._queue.queue)
                for idx, pending_job in enumerate(pending):
                    pending_job.position = idx + 1

            logger.info("Processing job %s", job.job_id)
            try:
                result = job.fn(*job.args, **job.kwargs)
                with self._lock:
                    job.status = JobStatus.DONE
                    job.result = result
                logger.info("Job %s completed successfully", job.job_id)

            except Exception as exc:
                with self._lock:
                    job.status = JobStatus.FAILED
                    job.error  = f"{type(exc).__name__}: {exc}"
                logger.exception("Job %s failed: %s", job.job_id, exc)

            finally:
                self._queue.task_done()

            # ── Registry cleanup: evict terminal jobs older than TTL ────────
            now = _time.monotonic()
            with self._lock:
                stale = [
                    jid for jid, j in self._jobs.items()
                    if j.status in (JobStatus.DONE, JobStatus.FAILED)
                    and j._started_at > 0
                    and (now - j._started_at) > JOB_TTL_SECONDS
                ]
                for jid in stale:
                    del self._jobs[jid]
            if stale:
                logger.debug("Evicted %d stale jobs from registry", len(stale))


# ---------------------------------------------------------------------------
# Module-level singleton accessor
# ---------------------------------------------------------------------------

_manager: Optional[QueueManager] = None
_manager_lock = threading.Lock()


def get_queue_manager() -> QueueManager:
    """
    Return the process-wide singleton QueueManager, creating it on first call.
    Thread-safe via double-checked locking.
    """
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = QueueManager()
    return _manager
