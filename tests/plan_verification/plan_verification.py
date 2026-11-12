from __future__ import annotations

import contextvars
import math
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import copy
from enum import Enum
from typing import Any, ClassVar

from hare.query.plans.call_signatures.call_signature_plans import CallSignaturePlans
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.select.raw_sql_query import RawSQLQuery
from tests.plan_verification.exceptions import PlanMismatchError


class PlanVerification:
    """Checks every query that runs on a kept plan against the same query built in full - its SQL
    text and its parameters must be the ones the plan gave. A query of a queryset's calls finds its
    plan in ``StatementPlanRuns.find_call_plan()`` before any query object is made; a query being
    built finds one by its description in ``AwaitableQuery._make_query_to_run()``;
    ``RawSQLQuery._get_statements()`` prepares a ``.raw()`` query. The full build runs with
    every plan lookup answering "none" and no plan kept.

    A run is compared right away, in the context it runs in. In a test marked
    ``plan_verification_after_test`` - one counting the builds or calls a run on a plan makes - the
    runs are compared after the test's body instead, each in the context it ran in.

    Installed for a pytest run with ``--verify-plans``; hare itself carries no part of it.
    """

    #: Whether the code running now builds in full - every plan lookup finds nothing.
    planless: ClassVar[contextvars.ContextVar[bool]] = contextvars.ContextVar(
        "plan_verification_planless", default=False
    )
    #: Whether the runs are compared after the test's body.
    defers: ClassVar[bool] = False
    #: A description of each mismatch found in the run.
    mismatches: ClassVar[list[str]] = []
    #: The runs on a plan not compared yet: the context each ran in, what ran it, the model, what gives
    #: the query to build in full and the statement the plan gave.
    pending: ClassVar[
        list[tuple[contextvars.Context, str, type, Callable[[], AwaitableQuery[Any]], tuple[str, list[Any]]]]
    ] = []
    #: The patched attributes, with the objects they held before.
    originals: ClassVar[list[tuple[Any, str, Any]]] = []

    @classmethod
    def install(cls) -> None:
        """Patches the places a query runs on a plan, and the plan lookups the full build bypasses."""
        if cls.originals:
            return
        try:
            cls.patch(AwaitableQuery, "_make_query_to_run", cls.get_verified_make_query_to_run)
            cls.patch(RawSQLQuery, "_get_statements", cls.get_verified_raw_sql_statements)
            cls.patch(StatementPlans, "find", cls.get_planless_lookup)
            cls.patch(StatementPlans, "record", cls.get_planless_record)
            cls.patch(CallSignaturePlans, "find_with_value_positions", cls.get_planless_lookup)
            cls.patch(CallSignaturePlans, "record", cls.get_planless_record)
            from hare.query.plans.statement.statement_plan_runs import StatementPlanRuns
            from hare.query.statements.write.matching_rows_query import MatchingRowsQuery

            cls.patch(StatementPlanRuns, "find_call_plan", cls.get_verified_call_plan)
            cls.patch(MatchingRowsQuery, "_get_statement_on", cls.get_verified_write_statement)
        except BaseException:
            cls.uninstall()
            raise

    @classmethod
    def uninstall(cls) -> None:
        """Puts the patched attributes back."""
        for owner, name, original in reversed(cls.originals):
            setattr(owner, name, original)
        cls.originals.clear()

    @classmethod
    def patch(cls, owner: type, name: str, make_replacement: Callable[[Any], Any]) -> None:
        """Replaces an attribute of a class, keeping the original to call and to restore.

        Args:
            owner: The class.
            name: The attribute.
            make_replacement: Makes the replacement from the original function.
        """
        original = owner.__dict__[name]
        function = original.__func__ if isinstance(original, (classmethod, staticmethod)) else original
        replacement = make_replacement(function)
        if isinstance(original, classmethod):
            replacement = classmethod(replacement)
        elif isinstance(original, staticmethod):
            replacement = staticmethod(replacement)
        cls.originals.append((owner, name, original))
        setattr(owner, name, replacement)

    @classmethod
    @contextmanager
    def deferring(cls, defers: bool) -> Iterator[None]:
        """Compares the runs of the block after it when ``defers`` is set - and at its end either way.

        Args:
            defers: Whether to compare the runs after the block instead of right away.

        Raises:
            PlanMismatchError: A run differs from its full build.
        """
        defers_before = cls.defers
        cls.defers = defers
        try:
            yield
        finally:
            cls.defers = defers_before
            cls.verify_pending()

    @classmethod
    @contextmanager
    def building_in_full(cls) -> Iterator[None]:
        """Builds every query of the block in full."""
        token = cls.planless.set(True)
        try:
            yield
        finally:
            cls.planless.reset(token)

    @classmethod
    def get_planless_lookup(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        """A plan lookup finding nothing while a query is built in full."""

        def lookup(*args: Any) -> Any:
            if cls.planless.get():
                return None
            return original(*args)

        return lookup

    @classmethod
    def get_planless_record(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        """Keeping a plan - skipped while a query is built in full."""

        def record(*args: Any) -> None:
            if not cls.planless.get():
                original(*args)

        return record

    @classmethod
    def get_verified_write_statement(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        """Verify writes with the actual restricted source, transaction and this run's stamps."""

        def get_statement(query: Any, connection: Any, source: Any = None, **kwargs: Any) -> Any:
            from hare.query.plans.statement.declarations import CallPlanHit

            statement = original(query, connection, source, **kwargs)
            if isinstance(statement, CallPlanHit) and not cls.planless.get():

                def make_query() -> AwaitableQuery[Any]:
                    unbuilt = copy(query)
                    if source is not None:
                        unbuilt._take_rows_of(source, keeps_ordering=True)
                    unbuilt._apply_connection(connection)
                    if kwargs.get("prepared") is not None:
                        unbuilt._prepared_write = kwargs["prepared"]
                    return unbuilt

                cls.verify(type(query).__name__, query.model, make_query, (statement.sql, statement.parameters))
            return statement

        return get_statement

    @classmethod
    def get_verified_call_plan(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        """``StatementPlanRuns.find_call_plan()`` comparing each hit with the query built in full - the
        one place a query runs on the plan of its queryset's calls."""

        def find_call_plan(query_class: type, specification: Any, connection: Any, *args: Any) -> Any:
            from hare.query.plans.statement.declarations import CallPlanHit
            from hare.query.statements.write.matching_rows_query import MatchingRowsQuery

            lookup = original(query_class, specification, connection, *args)
            if not isinstance(lookup, CallPlanHit) or cls.planless.get() or issubclass(query_class, MatchingRowsQuery):
                return lookup

            def make_query() -> AwaitableQuery[Any]:
                from hare.query.queryset.queryset import QuerySet
                from hare.query.queryset.selection.statement_selection import StatementSelection

                query = (
                    StatementSelection.get_select_query(specification)
                    if isinstance(specification, QuerySet)
                    else copy(specification)
                )
                query._apply_connection(connection)
                return query

            cls.verify(query_class.__name__, specification.model, make_query, (lookup.sql, lookup.parameters))
            return lookup

        return find_call_plan

    @classmethod
    def get_verified_make_query_to_run(cls, original: Callable[..., Any]) -> Callable[..., Any]:
        """``_make_query_to_run()`` comparing a run on a plan with the query built in full."""

        def make_query_to_run(query: AwaitableQuery[Any]) -> tuple[str, list[Any]]:
            if cls.planless.get():
                return original(query)
            unbuilt_query = copy(query)
            hits = StatementPlans.hits
            statement = original(query)
            if StatementPlans.hits != hits:
                if hasattr(type(query), "_prepared_write"):
                    unbuilt_query._prepared_write = query._prepared_write
                cls.verify(type(query).__name__, query.model, lambda: unbuilt_query, statement)
            return statement

        return make_query_to_run

    @classmethod
    def get_verified_raw_sql_statements(
        cls, original: Callable[[RawSQLQuery[Any], bool], list[tuple[str, list[Any]]]]
    ) -> Callable[[RawSQLQuery[Any], bool], list[tuple[str, list[Any]]]]:
        """``RawSQLQuery._get_statements()`` comparing a run on a plan with the text rendered in full."""

        def get_statements(query: RawSQLQuery[Any], parameters_inline: bool) -> list[tuple[str, list[Any]]]:
            statements = original(query, parameters_inline)
            if parameters_inline or query._raw_sql_plan is None or cls.planless.get():
                return statements
            unplanned_query = copy(query)
            unplanned_query._raw_sql_plan = None
            with cls.building_in_full():
                [built_statement] = original(unplanned_query, parameters_inline)
            cls.compare("RawSQLQuery", query.model, built_statement, statements[0])
            return statements

        return get_statements

    @classmethod
    def verify(
        cls,
        runner: str,
        model: type,
        make_unbuilt_query: Callable[[], AwaitableQuery[Any]],
        planned_statement: tuple[str, list[Any]],
    ) -> None:
        """Compares a run on a plan with the query built in full - now, or after the test's body.

        Args:
            runner: What ran the query on the plan, for the message.
            model: The model queried.
            make_unbuilt_query: Gives the query, not built yet - made only when it is built.
            planned_statement: The SQL and parameters of the run on the plan.

        Raises:
            PlanMismatchError: The statements differ.
        """
        if cls.defers:
            cls.pending.append((contextvars.copy_context(), runner, model, make_unbuilt_query, planned_statement))
            return
        cls.compare(runner, model, cls.build_in_full(make_unbuilt_query), planned_statement)

    @classmethod
    def verify_pending(cls) -> None:
        """Builds in full each query kept for a later comparison, in the context it ran in, and
        compares the statements.

        Raises:
            PlanMismatchError: A statement differs.
        """
        pending = list(cls.pending)
        cls.pending.clear()
        for context, runner, model, make_unbuilt_query, planned_statement in pending:
            built_statement = context.run(cls.build_in_full, make_unbuilt_query)
            cls.compare(runner, model, built_statement, planned_statement)

    @classmethod
    def build_in_full(cls, make_unbuilt_query: Callable[[], AwaitableQuery[Any]]) -> tuple[str, list[Any]]:
        """Builds a query without any plan.

        Args:
            make_unbuilt_query: Gives the query, not built yet.

        Returns:
            Its SQL and parameters.
        """
        with cls.building_in_full():
            query = make_unbuilt_query()
            query._make_query()
            return query._get_parameterized_sql()

    @classmethod
    def compare(
        cls,
        runner: str,
        model: type,
        built_statement: tuple[str, list[Any]],
        planned_statement: tuple[str, list[Any]],
    ) -> None:
        """Raises when the statement a plan gave differs from the one the full build made.

        Args:
            runner: What ran the query on the plan, for the message.
            model: The model queried.
            built_statement: The SQL and parameters of the full build.
            planned_statement: The SQL and parameters of the run on the plan.

        Raises:
            PlanMismatchError: They differ.
        """
        built_sql, built_parameters = built_statement
        planned_sql, planned_parameters = planned_statement
        if built_sql == planned_sql and cls.parameters_match(built_parameters, planned_parameters):
            return
        message = (
            f"{runner} of {model.__name__} ran on a plan giving another statement than its full build:\n"
            f"  built:   {built_sql}\n           {built_parameters!r}\n"
            f"  planned: {planned_sql}\n           {planned_parameters!r}"
        )
        cls.mismatches.append(message)
        raise PlanMismatchError(message)

    @classmethod
    def parameters_match(cls, built_parameters: list[Any], planned_parameters: list[Any]) -> bool:
        """Whether two parameter lists bind the same values.

        Args:
            built_parameters: The full build's parameters.
            planned_parameters: The plan's parameters.

        Returns:
            True when they match.
        """
        if len(built_parameters) != len(planned_parameters):
            return False
        return all(
            cls.parameter_matches(built_parameter, planned_parameter)
            for built_parameter, planned_parameter in zip(built_parameters, planned_parameters, strict=True)
        )

    @classmethod
    def parameter_matches(cls, built_parameter: Any, planned_parameter: Any) -> bool:
        """Whether two parameters bind the same value and type, with enums unwrapped.

        Automatic dates and randomized encodings must be prepared once and shared by
        both compilation paths. A timestamp tolerance or a token-shaped string cannot
        establish equivalence. NaN is compared explicitly because it is unequal to itself.

        Args:
            built_parameter: The full build's parameter.
            planned_parameter: The plan's parameter.

        Returns:
            True when they match.
        """
        while isinstance(built_parameter, Enum):
            built_parameter = built_parameter.value
        while isinstance(planned_parameter, Enum):
            planned_parameter = planned_parameter.value
        if type(built_parameter) is not type(planned_parameter):
            return False
        if isinstance(built_parameter, float) and math.isnan(built_parameter):
            return math.isnan(planned_parameter)
        return built_parameter == planned_parameter
