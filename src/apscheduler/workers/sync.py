from __future__ import annotations

import os
import platform
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import datetime, timezone
from logging import Logger, getLogger
from typing import Any, Callable, Iterable, Optional
from uuid import UUID

from .. import events
from ..abc import DataStore, EventSource
from ..enums import JobOutcome, RunState
from ..events import (
    EventHub, JobAdded, JobCompleted, JobDeadlineMissed, JobFailed, JobStarted, SubscriptionToken,
    WorkerStarted, WorkerStopped)
from ..structures import Job, JobResult


class Worker(EventSource):
    """Runs jobs locally in a thread pool."""

    _executor: ThreadPoolExecutor
    _state: RunState = RunState.stopped
    _wakeup_event: threading.Event

    def __init__(self, data_store: DataStore, *, max_concurrent_jobs: int = 20,
                 identity: Optional[str] = None, logger: Optional[Logger] = None):
        self.max_concurrent_jobs = max_concurrent_jobs
        self.identity = identity or f'{platform.node()}-{os.getpid()}-{id(self)}'
        self.logger = logger or getLogger(__name__)
        self._acquired_jobs: set[Job] = set()
        self._exit_stack = ExitStack()
        self._events = EventHub()
        self._running_jobs: set[UUID] = set()

        if self.max_concurrent_jobs < 1:
            raise ValueError('max_concurrent_jobs must be at least 1')

        self.data_store = data_store

    @property
    def state(self) -> RunState:
        return self._state

    def __enter__(self) -> Worker:
        self._state = RunState.starting
        self._wakeup_event = threading.Event()
        self._exit_stack.__enter__()
        self._exit_stack.enter_context(self._events)

        # Initialize the data store
        self._exit_stack.enter_context(self.data_store)
        relay_token = self._events.relay_events_from(self.data_store)
        self._exit_stack.callback(self.data_store.unsubscribe, relay_token)

        # Wake up the worker if the data store emits a significant job event
        wakeup_token = self.data_store.subscribe(
            lambda event: self._wakeup_event.set(), {JobAdded})
        self._exit_stack.callback(self.data_store.unsubscribe, wakeup_token)

        # Start the worker and return when it has signalled readiness or raised an exception
        start_future: Future[None] = Future()
        token = self._events.subscribe(start_future.set_result)
        self._executor = ThreadPoolExecutor(1)
        run_future = self._executor.submit(self.run)
        try:
            wait([start_future, run_future], return_when=FIRST_COMPLETED)
        finally:
            self._events.unsubscribe(token)

        if run_future.done():
            run_future.result()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._state = RunState.stopping
        self._wakeup_event.set()
        self._executor.shutdown(wait=exc_type is None)
        self._exit_stack.__exit__(exc_type, exc_val, exc_tb)
        del self._wakeup_event

    def subscribe(self, callback: Callable[[events.Event], Any],
                  event_types: Optional[Iterable[type[events.Event]]] = None) -> SubscriptionToken:
        return self._events.subscribe(callback, event_types)

    def unsubscribe(self, token: events.SubscriptionToken) -> None:
        self._events.unsubscribe(token)

    def run(self) -> None:
        if self._state is not RunState.starting:
            raise RuntimeError(f'This function cannot be called while the worker is in the '
                               f'{self._state} state')

        # Signal that the worker has started
        self._state = RunState.started
        self._events.publish(WorkerStarted())

        executor = ThreadPoolExecutor(max_workers=self.max_concurrent_jobs)
        try:
            while self._state is RunState.started:
                available_slots = self.max_concurrent_jobs - len(self._running_jobs)
                if available_slots:
                    jobs = self.data_store.acquire_jobs(self.identity, available_slots)
                    for job in jobs:
                        task = self.data_store.get_task(job.task_id)
                        self._running_jobs.add(job.id)
                        executor.submit(self._run_job, job, task.func)

                self._wakeup_event.wait()
                self._wakeup_event = threading.Event()
        except BaseException as exc:
            executor.shutdown(wait=False)
            self._state = RunState.stopped
            self._events.publish(WorkerStopped(exception=exc))
            raise

        executor.shutdown()
        self._state = RunState.stopped
        self._events.publish(WorkerStopped())

    def _run_job(self, job: Job, func: Callable) -> None:
        try:
            # Check if the job started before the deadline
            start_time = datetime.now(timezone.utc)
            if job.start_deadline is not None and start_time > job.start_deadline:
                self._events.publish(JobDeadlineMissed.from_job(job, start_time))
                return

            self._events.publish(JobStarted.from_job(job, start_time))
            try:
                retval = func(*job.args, **job.kwargs)
            except BaseException as exc:
                result = JobResult(job_id=job.id, outcome=JobOutcome.failure, exception=exc)
                self.data_store.release_job(self.identity, job.task_id, result)
                self._events.publish(JobFailed.from_exception(job, start_time, exc))
                if not isinstance(exc, Exception):
                    raise
            else:
                result = JobResult(job_id=job.id, outcome=JobOutcome.success, return_value=retval)
                self.data_store.release_job(self.identity, job.task_id, result)
                self._events.publish(JobCompleted.from_retval(job, start_time, retval))
        finally:
            self._running_jobs.remove(job.id)
