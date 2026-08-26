from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Literal


class JobKind(str, Enum):
    INGEST = "ingest"
    CALENDAR = "calendar"
    SELECT = "select"
    BACKTEST = "backtest"


class JobBusyError(RuntimeError):
    pass


@dataclass
class JobSnapshot:
    job_id: str
    kind: JobKind
    status: Literal["running", "succeeded", "failed"]
    events: list[dict[str, Any]]
    result: dict[str, Any] | None
    error: str | None
    progress: dict[str, Any] | None = None


class JobSlot:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._job_id: str | None = None
        self._kind: JobKind | None = None
        self._status: Literal["running", "succeeded", "failed"] | None = None
        self._events: list[dict[str, Any]] = []
        self._result: dict[str, Any] | None = None
        self._error: str | None = None
        self._progress: dict[str, Any] | None = None

    def try_start(
        self,
        kind: JobKind,
        worker: Callable[[Callable[[str, dict | None], None]], dict[str, Any]],
    ) -> str:
        with self._lock:
            if self._status == "running":
                raise JobBusyError("已有任务在跑")
            job_id = uuid.uuid4().hex
            self._job_id = job_id
            self._kind = kind
            self._status = "running"
            self._events = []
            self._result = None
            self._error = None
            self._progress = None

        def emit(message: str, payload: dict | None = None) -> None:
            with self._lock:
                if payload is not None and payload.get("kind") == "progress":
                    self._progress = payload
                    return
                self._events.append({"message": message, "payload": payload})

        def run() -> None:
            try:
                result = worker(emit)
            except Exception as exc:
                with self._lock:
                    self._status = "failed"
                    self._error = str(exc)
                return
            with self._lock:
                self._status = "succeeded"
                self._result = result

        threading.Thread(target=run, daemon=True).start()
        return job_id

    def snapshot(self) -> JobSnapshot | None:
        with self._lock:
            if self._job_id is None or self._kind is None or self._status is None:
                return None
            return JobSnapshot(
                job_id=self._job_id,
                kind=self._kind,
                status=self._status,
                events=list(self._events),
                result=self._result,
                error=self._error,
                progress=None if self._progress is None else dict(self._progress),
            )
