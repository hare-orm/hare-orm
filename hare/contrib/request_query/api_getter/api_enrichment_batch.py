from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from hare.contrib.request_query.api_getter.api_http_requests import ApiHttpRequests
from hare.contrib.request_query.api_getter.api_value_paths import ApiValuePaths
from hare.contrib.request_query.api_getter.declarations import ApiAssignment, ApiTargetValue
from hare.contrib.request_query.api_getter.enums import ApiGetterTrigger

if TYPE_CHECKING:
    from hare.contrib.request_query.api_getter.api_getter import ApiGetter
    from hare.contrib.request_query.api_getter.api_source import ApiSource
    from hare.contrib.request_query.api_getter.api_value_mapping import ApiValueMapping

TInput = TypeVar("TInput")
TResult = TypeVar("TResult")


class ApiEnrichmentBatch:
    """Selected mappings and sources for one preparation, with a bounded request scheduler.

    Preparation obtains and validates every assignment before touching targets. Source answers
    are applied to the preparation in declaration order, regardless of their completion order.
    The HTTP client and headers tasks belong to this batch and are drained before it exits.
    """

    def __init__(
        self,
        getter: type[ApiGetter],
        objs: list[Any],
        sources: Iterable[ApiSource],
        mappings: Iterable[ApiValueMapping],
        trigger: ApiGetterTrigger,
        requested_names: frozenset[str] | None,
    ) -> None:
        self.getter = getter
        self.objs = list(objs)
        self.mappings = tuple(mappings)
        self.sources = tuple(
            source
            for source in sources
            if source.triggers.runs_on(trigger)
            and (requested_names is None or source.get_target_names() & requested_names)
        )
        self.unique_sources = dict((id(source), source) for source in self.sources)
        self.containers_by_source = {
            source_id: self.get_source_containers(source) for source_id, source in self.unique_sources.items()
        }

    def get_source_containers(self, source: ApiSource) -> dict[Any, list[Any]]:
        """Snapshot the original keys and target containers before conversion or HTTP work."""
        containers: dict[Any, list[Any]] = {}
        key_name = source.get_key_name()
        for root in self.objs:
            for container in ApiValuePaths.get_containers(root, source.key):
                values = [
                    value
                    for value in ApiValuePaths.to_list(ApiValuePaths.get_child(container, key_name))
                    if self.getter.can_be_asked(value)
                ]
                for value in values or [None]:
                    containers.setdefault(value, []).append(container)
        return containers

    async def prepare(self) -> tuple[ApiAssignment, ...]:
        """Return assignments without changing any target object."""
        if not self.objs:
            return ()
        getter = self.getter
        target_values: dict[tuple[int, str], ApiTargetValue] = {}
        for mapping in self.mappings:
            for root in self.objs:
                value = getter.get_converted(None, mapping, ApiValuePaths.get_value_at_path(root, mapping.key))
                for container in ApiValuePaths.get_containers(root, mapping.set_key):
                    getter.apply_mapping_value(target_values, container, mapping, value)
        if self.sources:
            # Repeating a source preserves mapping precedence but does not duplicate HTTP work.
            async with ApiHttpRequests() as requests:
                answers = await self.run_together(
                    [
                        getter.ask(requests, source, self.containers_by_source[source_id])
                        for source_id, source in self.unique_sources.items()
                    ]
                )
            by_source = dict(zip(self.unique_sources, answers, strict=True))
            for source in self.sources:
                source_id = id(source)
                getter.apply_source_values(
                    target_values, source, self.containers_by_source[source_id], by_source[source_id]
                )
        return getter.prepare_target_values(target_values)

    @staticmethod
    async def run_together(awaitables: list[Coroutine[Any, Any, TResult]]) -> list[TResult]:
        """Run a fixed collection of workers, cancelling and draining siblings on any failure."""
        tasks = []
        try:
            async with asyncio.TaskGroup() as group:
                for awaitable in awaitables:
                    tasks.append(group.create_task(awaitable))
        except BaseExceptionGroup as errors:
            if len(errors.exceptions) == 1:
                error = errors.exceptions[0]
                raise error from error.__cause__
            raise
        return [task.result() for task in tasks]

    @staticmethod
    async def run_workers(
        inputs: Iterable[TInput],
        operation: Callable[[TInput], Awaitable[TResult]],
        concurrency: int,
    ) -> list[TResult]:
        """Consume inputs gradually using at most concurrency tasks, retaining input order."""
        iterator = iter(inputs)
        results: list[TResult | None] = []

        async def worker() -> None:
            for value in iterator:
                position = len(results)
                results.append(None)
                results[position] = await operation(value)

        await ApiEnrichmentBatch.run_together([worker() for _ in range(concurrency)])
        return cast("list[TResult]", results)
