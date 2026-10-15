from __future__ import annotations

import contextvars
import datetime
import math
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from copy import copy
from enum import Enum
from functools import partial
from typing import Any, ClassVar

from hare.query.plans.call_signatures.call_signature_plans import CallSignaturePlans
from hare.query.plans.call_signatures.call_signature_runs import CallSignatureRuns
from hare.query.plans.statement.statement_plans import StatementPlans
from hare.query.queryset.queryset import QuerySet
from hare.query.queryset.selection.statement_selection import StatementSelection
from hare.query.statements.awaitable_query import AwaitableQuery
from hare.query.statements.select.model_rows_query import ModelRowsQuery
from hare.query.statements.select.raw_sql_query import RawSQLQuery
from tests.plan_verification.constants import CURRENT_MOMENT_TOLERANCE, FERNET_TOKEN_PREFIX
from tests.plan_verification.exceptions import PlanMismatchError


class PlanVerification:
    """Checks every query that runs on a kept plan against the same query built in full - its SQL
    text and its parameters must be the ones the plan gave. A query runs on a plan in three places:
    ``AwaitableQuery._make_query_to_run()`` (a plan found by the query's description or by the calls
    its queryset was made with), ``CallSignatureRuns.fetch_on_plan()`` (a queryset run without making a
    query at all) and ``RawSQLQuery._get_statements()`` (a ``.raw()`` query). The full build runs with
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
        cls.patch(AwaitableQuery, "_make_query_to_run", cls.get_verified_make_query_to_run)
        cls.patch(CallSignatureRuns, "fetch_on_plan", cls.get_verified_fetch_on_plan)
        cls.patch(CallSignatureRuns, "fetch_values_on_plan", cls.get_verified_fetch_values_on_plan)
        cls.patch(RawSQLQuery, "_get_statements", cls.get_verified_raw_sql_statements)
        cls.patch(StatementPlans, "find", cls.get_planless_lookup)
        cls.patch(StatementPlans, "find_for_model", cls.get_planless_lookup)
        cls.patch(StatementPlans, "record", cls.get_planless_record)
        cls.patch(CallSignaturePlans, "find", cls.get_planless_lookup)
        cls.patch(CallSignaturePlans, "record", cls.get_planless_record)

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
    def get_verified_make_query_to_run(cls, original: Callable[[AwaitableQuery[Any]], None]) -> Callable[..., None]:
        """``_make_query_to_run()`` comparing a run on a plan with the query built in full."""

        def make_query_to_run(query: AwaitableQuery[Any]) -> None:
            if cls.planless.get():
                original(query)
                return
            unbuilt_query = copy(query)
            original(query)
            if query._compiled_statement is not None:
                cls.verify(type(query).__name__, query.model, lambda: unbuilt_query, query._compiled_statement)

        return make_query_to_run

    @classmethod
    def get_verified_fetch_on_plan(
        cls, original: Callable[..., Coroutine[Any, Any, Any]]
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """``CallSignatureRuns.fetch_on_plan()`` comparing the run with the queryset's query built in full."""

        async def fetch_on_plan(queryset: QuerySet[Any, Any], plan: Any, parameters: list[Any], db: Any) -> Any:
            unbuilt_queryset = copy(queryset)
            cls.verify(
                "QuerySet",
                queryset.model,
                partial(cls.get_model_rows_query, unbuilt_queryset, db),
                (plan.sql, list(parameters)),
            )
            return await original(queryset, plan, parameters, db)

        return fetch_on_plan

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
    def get_verified_fetch_values_on_plan(
        cls, original: Callable[..., Coroutine[Any, Any, Any]]
    ) -> Callable[..., Coroutine[Any, Any, Any]]:
        """``CallSignatureRuns.fetch_values_on_plan()`` comparing the run with the queryset's values
        query built in full."""

        async def fetch_values_on_plan(
            queryset: QuerySet[Any, Any], plan: Any, values_reading: Any, parameters: list[Any], db: Any
        ) -> Any:
            unbuilt_queryset = copy(queryset)
            cls.verify(
                "QuerySet",
                queryset.model,
                partial(cls.get_values_query, unbuilt_queryset, db),
                (plan.sql, list(parameters)),
            )
            return await original(queryset, plan, values_reading, parameters, db)

        return fetch_values_on_plan

    @staticmethod
    def get_values_query(queryset: QuerySet[Any, Any], db: Any) -> AwaitableQuery[Any]:
        """The values query a queryset run without making one would build.

        Args:
            queryset: A copy of the queryset.
            db: The connection it ran on.

        Returns:
            The query, not built yet.
        """
        query = StatementSelection.get_values_query(queryset)
        query._is_execution_query = True
        query._apply_connection(db)
        return query

    @staticmethod
    def get_model_rows_query(queryset: QuerySet[Any, Any], db: Any) -> AwaitableQuery[Any]:
        """The query a queryset run without making one would build.

        Args:
            queryset: A copy of the queryset.
            db: The connection it ran on.

        Returns:
            The query, not built yet.
        """
        query = ModelRowsQuery(queryset)
        query._is_execution_query = True
        query._apply_connection(db)
        return query

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
        """Whether two parameters bind the same value - of the same type, an enum member as its value
        (as a plan binds it). Two values made anew by each build match by their type alone: the
        current moment (an ``auto_now`` field's) and a Fernet token, random per encryption.

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
        if built_parameter == planned_parameter:
            return True
        if isinstance(built_parameter, datetime.datetime):
            return cls.is_current_moment(built_parameter) and cls.is_current_moment(planned_parameter)
        if isinstance(built_parameter, str):
            return (
                built_parameter.startswith(FERNET_TOKEN_PREFIX)
                and planned_parameter.startswith(FERNET_TOKEN_PREFIX)
                and len(built_parameter) == len(planned_parameter)
            )
        return False

    @staticmethod
    def is_current_moment(moment: datetime.datetime) -> bool:
        """Whether a datetime is the current moment - one a build took just now.

        Args:
            moment: The datetime.

        Returns:
            True when it is within ``CURRENT_MOMENT_TOLERANCE`` of now.
        """
        now = datetime.datetime.now(moment.tzinfo) if moment.tzinfo else datetime.datetime.now()
        return abs(now - moment) <= CURRENT_MOMENT_TOLERANCE
