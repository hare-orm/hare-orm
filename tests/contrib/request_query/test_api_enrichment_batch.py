"""Preparation is non-mutating and request workers have bounded lifetimes."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from hare.contrib.request_query import ApiGetter, ApiValueMapping, ApiValueMissingError
from hare.contrib.request_query.api_getter.api_enrichment_batch import ApiEnrichmentBatch
from hare.contrib.request_query.api_getter.api_http_requests import ApiHttpRequests

pytestmark = pytest.mark.database_independent


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_worker_cleanup() -> None:
    entered = asyncio.Event()
    cleaning = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def operation(value: int) -> int:
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()
            finished.set()
        return value

    batch = asyncio.create_task(ApiEnrichmentBatch.run_workers(range(20), operation, 1))
    try:
        await entered.wait()
        batch.cancel()
        await cleaning.wait()
        batch.cancel()
        assert not batch.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await batch
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(batch, return_exceptions=True)


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_client_close(monkeypatch) -> None:
    requests = ApiHttpRequests()
    entered = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    original = requests.client.aclose

    async def close() -> None:
        entered.set()
        await release.wait()
        await original()
        finished.set()

    monkeypatch.setattr(requests.client, "aclose", close)
    closing = asyncio.create_task(requests.__aexit__())
    try:
        await entered.wait()
        closing.cancel()
        closing.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert finished.is_set()
        assert requests.client.is_closed
    finally:
        release.set()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("body_failure", [None, "error", "cancellation"])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_client_cleanup_preserves_body_failure_and_drains_headers(monkeypatch, body_failure, cleanup_fails):
    requests = ApiHttpRequests()
    primary = None
    if body_failure is not None:
        primary = ValueError("request failed") if body_failure == "error" else asyncio.CancelledError()
    cleanup = RuntimeError("client close failed")
    started = asyncio.Event()
    drained = asyncio.Event()
    original = requests.client.aclose

    async def headers():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            drained.set()

    async def close():
        assert drained.is_set()
        await original()
        if cleanup_fails:
            raise cleanup

    requests.headers_by_source[1] = asyncio.create_task(headers())
    await started.wait()
    monkeypatch.setattr(requests.client, "aclose", close)
    caught = None
    try:
        async with requests:
            if primary is not None:
                raise primary
    except BaseException as error:
        caught = error
    if primary is not None and cleanup_fails:
        assert isinstance(caught, BaseExceptionGroup)
        assert caught.exceptions == (primary, cleanup)
    else:
        assert caught is (cleanup if cleanup_fails else primary)
    assert drained.is_set()
    assert requests.headers_by_source[1].done()
    assert requests.client.is_closed


@pytest.mark.asyncio
async def test_required_failure_does_not_apply_earlier_assignments() -> None:
    objs = [{"input": "new", "result": "old"}, {"input": None, "result": "old"}]
    with pytest.raises(ApiValueMissingError):
        await ApiGetter.set(objs, mappings=[ApiValueMapping(key="input", set_key="result", required=True)])
    assert [obj["result"] for obj in objs] == ["old", "old"]


@pytest.mark.asyncio
async def test_prepared_values_can_be_read_without_applying() -> None:
    objs = [{"input": "3"}, {"input": None}]
    assignments = await ApiGetter.prepare(
        objs, mappings=[ApiValueMapping(key="input", set_key="result", convert=int, default=4)]
    )
    assert [assignment.value for assignment in assignments] == [3, 4]
    assert objs == [{"input": "3"}, {"input": None}]
    ApiGetter.apply(assignments)
    assert objs == [{"input": "3", "result": 3}, {"input": None, "result": 4}]


@pytest.mark.asyncio
async def test_workers_consume_inputs_lazily_and_preserve_order() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    consumed: list[int] = []
    workers: set[asyncio.Task[object]] = set()

    def inputs() -> Iterator[int]:
        for value in range(1000):
            consumed.append(value)
            yield value

    async def operation(value: int) -> int:
        task = asyncio.current_task()
        assert task is not None
        workers.add(task)
        if len(workers) == 3:
            entered.set()
        await release.wait()
        return value * 2

    baseline = asyncio.all_tasks()
    batch = asyncio.create_task(ApiEnrichmentBatch.run_workers(inputs(), operation, 3))
    try:
        await entered.wait()
        assert consumed == [0, 1, 2]
        assert asyncio.all_tasks() - baseline == workers | {batch}
        release.set()
        assert await batch == [value * 2 for value in range(1000)]
        assert len(workers) == 3
    finally:
        batch.cancel()
        await asyncio.gather(batch, return_exceptions=True)


@pytest.mark.parametrize("fail", [False, True])
@pytest.mark.asyncio
async def test_failure_and_cancellation_drain_workers(fail: bool) -> None:
    ready = asyncio.Event()
    failure = asyncio.Event()
    running: set[int] = set()

    async def operation(value: int) -> int:
        running.add(value)
        if len(running) == 3:
            ready.set()
        try:
            if fail and value == 0:
                await failure.wait()
                raise ValueError("request failed")
            await asyncio.Event().wait()
            return value
        finally:
            running.remove(value)

    batch = asyncio.create_task(ApiEnrichmentBatch.run_workers(range(1000), operation, 3))
    try:
        await ready.wait()
        if fail:
            failure.set()
        else:
            batch.cancel()
        with pytest.raises(ValueError if fail else asyncio.CancelledError):
            await batch
        assert not running
    finally:
        batch.cancel()
        await asyncio.gather(batch, return_exceptions=True)
