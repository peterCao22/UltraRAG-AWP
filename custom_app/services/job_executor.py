import queue
import threading
from typing import Callable


class JobExecutor:
    """Lightweight FIFO job queue executor for KB jobs (Phase 2.5)."""

    def __init__(self, max_workers: int = 1) -> None:
        # Keep single-worker semantics for deterministic task order.
        self._max_workers = max_workers
        self._q: "queue.Queue[tuple[Callable, tuple, dict]]" = queue.Queue()
        self._workers: list[threading.Thread] = []
        for i in range(max(1, self._max_workers)):
            t = threading.Thread(
                target=self._worker_loop,
                name=f"kb-job-worker-{i}",
                daemon=True,
            )
            t.start()
            self._workers.append(t)

    def submit(self, fn: Callable, *args, **kwargs):
        self._q.put((fn, args, kwargs))
        return {"queued": True, "queue_size": self._q.qsize()}

    def _worker_loop(self) -> None:
        while True:
            fn, args, kwargs = self._q.get()
            try:
                fn(*args, **kwargs)
            except Exception:
                # Execution errors are already persisted by task handlers.
                pass
            finally:
                self._q.task_done()
