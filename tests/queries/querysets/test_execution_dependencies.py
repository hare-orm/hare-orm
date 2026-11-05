from __future__ import annotations

import asyncio
from copy import copy

import pytest

from hare.exceptions import QueryError
from hare.query.execution.dependent_condition import DependentCondition
from hare.query.execution.query_dependency import QueryDependency
from hare.query.execution.query_execution import QueryExecution
from hare.query.expressions import Q
from hare.query.plans.description.plan_context import PlanContext
from hare.query.queryset.queryset import QuerySet
from hare.query.queryset.selection.statement_selection import StatementSelection
from tests.shared_models.testmodels import Tournament


class FilterDependency(QueryDependency):
    def __init__(self):
        self.calls = 0

    async def load(self):
        self.calls += 1
        return Q(id=self.calls)


def test_bound_conditions_do_not_become_declared_filters():
    declaration = QuerySet(Tournament)
    declaration._q_objects = [Q(name="Book")]
    statement = StatementSelection.get_select_query(declaration)
    statement._ambient_conditions = (Q(id=7),)
    assert statement._q_objects == [Q(id=7), Q(name="Book")]
    assert declaration._q_objects == [Q(name="Book")]
    rebound = StatementSelection.get_select_query(statement)
    assert rebound._q_objects == [Q(name="Book")]
    branch = copy(statement)
    branch._annotation_output_fields["title"] = None
    assert statement._annotation_output_fields == {}


@pytest.mark.asyncio
async def test_dependency_values_are_reused_only_inside_the_execution():
    dependency = FilterDependency()
    declaration = QuerySet(Tournament)
    declaration._execution_dependencies = (dependency,)
    condition = DependentCondition(dependency)
    with pytest.raises(QueryError, match="before its SQL"):
        condition.get_plan_description(PlanContext.EMPTY)

    async def read_nested():
        nested = QueryExecution()
        await nested.prepare(declaration)
        return QueryExecution.get_condition(dependency).filters["id"]

    first = QueryExecution()
    await first.prepare(declaration)
    assert await first.call(read_nested) == 1
    assert dependency.calls == 1
    second = QueryExecution()
    await second.prepare(declaration)
    assert await second.call(read_nested) == 2
    assert await first.call(read_nested) == 1
    assert QueryExecution.current.get() is None


@pytest.mark.asyncio
async def test_iterator_holds_inputs_without_leaking_context_and_closes_in_another_task():
    dependency = FilterDependency()
    declaration = QuerySet(Tournament)
    declaration._execution_dependencies = (dependency,)
    closed = []

    async def rows():
        try:
            for _ in range(3):
                yield QueryExecution.get_condition(dependency).filters["id"]
        finally:
            closed.append(QueryExecution.get_condition(dependency).filters["id"])

    iterator = QueryExecution.iterate(declaration, rows())
    assert await anext(iterator) == 1
    assert QueryExecution.current.get() is None
    assert await anext(iterator) == 1
    await asyncio.create_task(iterator.aclose())
    assert closed == [1]
    assert dependency.calls == 1
    assert QueryExecution.current.get() is None


@pytest.mark.asyncio
async def test_cancelled_execution_restores_context():
    execution = QueryExecution()
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def operation():
        started.set()
        await blocked.wait()

    task = asyncio.create_task(execution.call(operation))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert QueryExecution.current.get() is None


@pytest.mark.asyncio
async def test_empty_declaration_does_not_load_dependencies():
    dependency = FilterDependency()
    declaration = QuerySet(Tournament)
    declaration._is_none = True
    declaration._execution_dependencies = (dependency,)
    execution = QueryExecution()
    await execution.prepare(declaration)
    assert dependency.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_first", [True, False])
async def test_empty_and_nonempty_queries_share_only_loaded_dependency_values(empty_first):
    dependency = FilterDependency()
    declaration = QuerySet(Tournament)
    declaration._execution_dependencies = (dependency,)
    empty = declaration.none()
    specifications = (empty, declaration) if empty_first else (declaration, empty)

    async def read_condition():
        return QueryExecution.get_condition(dependency).filters

    assert await QueryExecution.run_operation(specifications, read_condition) == {"id": 1}
    assert dependency.calls == 1
    assert QueryExecution.current.get() is None


@pytest.mark.asyncio
async def test_nested_execution_loads_a_dependency_skipped_by_an_empty_parent():
    dependency = FilterDependency()
    declaration = QuerySet(Tournament)
    declaration._execution_dependencies = (dependency,)
    execution = QueryExecution()
    await execution.prepare(declaration.none())

    async def read_nested():
        assert QueryExecution.get_condition(dependency).filters == {}
        nested = QueryExecution()
        await nested.prepare(declaration)
        return await nested.call(read_condition)

    async def read_condition():
        return QueryExecution.get_condition(dependency).filters

    assert await execution.call(read_nested) == {"id": 1}
    assert await execution.call(read_condition) == {}
    assert dependency.calls == 1
    assert QueryExecution.current.get() is None
