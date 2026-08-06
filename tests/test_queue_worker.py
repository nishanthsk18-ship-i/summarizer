"""
test_queue_worker.py — Pytest suite for queue_worker.py

Tests cover:
  - Job submission and ID generation
  - Sequential FIFO execution
  - Correct DONE status and result storage
  - Correct FAILED status and error capture
  - Queue position tracking
  - Singleton QueueManager integrity
"""

from __future__ import annotations

import time
import pytest
from queue_worker import (
    Job,
    JobStatus,
    QueueManager,
    get_queue_manager,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _wait_for_job(manager: QueueManager, job_id: str, timeout: float = 5.0) -> Job:
    """Poll until the job transitions out of PENDING/PROCESSING, or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get_job(job_id)
        assert job is not None, f"Job {job_id} not found in registry"
        if job.status in (JobStatus.DONE, JobStatus.FAILED):
            return job
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not complete within {timeout}s")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestQueueManagerSubmit:
    def test_submit_returns_unique_string_id(self) -> None:
        mgr = QueueManager()
        id1 = mgr.submit(lambda: None)
        id2 = mgr.submit(lambda: None)
        assert isinstance(id1, str) and len(id1) > 0
        assert isinstance(id2, str) and len(id2) > 0
        assert id1 != id2

    def test_submitted_job_exists_in_registry(self) -> None:
        mgr = QueueManager()
        job_id = mgr.submit(lambda: 42)
        job = mgr.get_job(job_id)
        assert job is not None
        assert job.job_id == job_id

    def test_unknown_job_id_returns_none(self) -> None:
        mgr = QueueManager()
        assert mgr.get_job("nonexistent-job-id") is None


class TestQueueManagerExecution:
    def test_successful_job_transitions_to_done(self) -> None:
        mgr = QueueManager()
        job_id = mgr.submit(lambda: "hello_result")
        job = _wait_for_job(mgr, job_id)
        assert job.status == JobStatus.DONE
        assert job.result == "hello_result"
        assert job.error is None

    def test_failed_job_transitions_to_failed(self) -> None:
        def _boom() -> None:
            raise ValueError("intentional failure")

        mgr = QueueManager()
        job_id = mgr.submit(_boom)
        job = _wait_for_job(mgr, job_id)
        assert job.status == JobStatus.FAILED
        assert job.result is None
        assert "ValueError" in (job.error or "")
        assert "intentional failure" in (job.error or "")

    def test_jobs_execute_in_fifo_order(self) -> None:
        execution_order: list[int] = []
        barrier = time.monotonic()

        def _make_job(n: int):
            def _fn() -> int:
                execution_order.append(n)
                return n
            return _fn

        mgr = QueueManager()
        ids = [mgr.submit(_make_job(i)) for i in range(5)]

        # Wait for the last job to complete
        _wait_for_job(mgr, ids[-1], timeout=10.0)

        assert execution_order == [0, 1, 2, 3, 4], f"Got: {execution_order}"

    def test_job_with_args_and_kwargs(self) -> None:
        def _add(a: int, b: int, multiplier: int = 1) -> int:
            return (a + b) * multiplier

        mgr = QueueManager()
        job_id = mgr.submit(_add, 3, 4, multiplier=2)
        job = _wait_for_job(mgr, job_id)
        assert job.status == JobStatus.DONE
        assert job.result == 14


class TestQueuePositions:
    def test_first_job_position_is_1_or_0(self) -> None:
        """
        Position 1 = queued and not yet started.
        Position 0 = actively being processed.
        Either is valid immediately after submit depending on scheduler timing.
        """
        import threading
        ready = threading.Event()

        def _slow() -> str:
            ready.wait()
            return "done"

        mgr = QueueManager()
        job_id = mgr.submit(_slow)
        time.sleep(0.1)  # let the worker pick it up
        job = mgr.get_job(job_id)
        assert job is not None
        assert job.position in (0, 1)
        ready.set()
        _wait_for_job(mgr, job_id)


class TestSingleton:
    def test_get_queue_manager_is_singleton(self) -> None:
        mgr1 = get_queue_manager()
        mgr2 = get_queue_manager()
        assert mgr1 is mgr2

    def test_singleton_shares_job_state(self) -> None:
        mgr = get_queue_manager()
        job_id = mgr.submit(lambda: 99)
        _wait_for_job(mgr, job_id)
        # The singleton from a second call must see the same job
        assert get_queue_manager().get_job(job_id) is not None
