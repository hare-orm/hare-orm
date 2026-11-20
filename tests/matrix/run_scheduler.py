from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait

from tests.matrix.messages import MATRIX_EXECUTION_CLEANUP_FAILED, RUN_CONCURRENCY_INVALID
from tests.matrix.run_result import RunResult
from tests.matrix.run_spec import RunSpec


class RunScheduler:
    """Bound concurrency by workload and reserve shared physical resources before launching."""

    def __init__(self, jobs: int, few_jobs: int) -> None:
        if jobs < 1 or few_jobs < 1:
            raise ValueError(RUN_CONCURRENCY_INVALID)
        self.jobs = jobs
        self.few_jobs = few_jobs

    def run(
        self,
        specs: Sequence[RunSpec],
        execute: Callable[[RunSpec], RunResult],
        stop: Callable[[], None],
        progress: Callable[[int, int, RunResult], None],
    ) -> list[RunResult]:
        """Finish all results, or stop active processes before joining threads after an error."""
        pending = list(enumerate(specs))
        active: dict[Future[RunResult], tuple[int, RunSpec]] = {}
        results: dict[int, RunResult] = {}
        executor = ThreadPoolExecutor(max_workers=self.jobs + self.few_jobs)
        try:
            while pending or active:
                reserved = set().union(*(spec.resources for _, spec in active.values()))
                slots = {False: self.jobs, True: self.few_jobs}
                for _, spec in active.values():
                    slots[spec.few_tests] -= 1
                remaining = []
                for index, spec in pending:
                    if slots[spec.few_tests] and not reserved.intersection(spec.resources):
                        active[executor.submit(execute, spec)] = index, spec
                        reserved.update(spec.resources)
                        slots[spec.few_tests] -= 1
                    else:
                        remaining.append((index, spec))
                pending = remaining
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    index, _ = active.pop(future)
                    result = future.result()
                    results[index] = result
                    progress(len(results), len(specs), result)
        except BaseException as error:
            for future in active:
                future.cancel()
            try:
                stop()
            except BaseException as cleanup_error:
                raise BaseExceptionGroup(MATRIX_EXECUTION_CLEANUP_FAILED, [error, cleanup_error]) from None
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=True)
        return [results[index] for index in range(len(specs))]
