from __future__ import annotations

import inspect
import typing
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, cast
from unittest import SkipTest

from hare.contrib.test.features.feature_conditions import FeatureConditions
from hare.core.hare_context import HareContext

if TYPE_CHECKING:
    pass

TestItem = Callable[..., typing.Any]


def requires_features(connection_alias: str | None = None, **conditions: typing.Any) -> Callable[[TestItem], TestItem]:
    """Skips a test unless the connection's features match.

    The database must be initialized before the decorated test runs.

    Args:
        connection_alias: The connection to check - the current context's default connection
            (else its first configured one) when None.
        conditions: Values by name, all of which must match for the test to run - of the
            connection's ``Features`` (``supports_transactions``, ``supports_partial_indexes``), else
            of its dialect's ``SqlLiterals`` (``identifier_quote_char``), else of its ``Dialect``, and
            ``dialect`` for the dialect's name.
            A test needing a capability names the capability, so any dialect having it runs the
            test; ``dialect`` is for a test of one dialect's own SQL or types.

    Example:
        @requires_features(dialect="sqlite")
        @pytest.mark.asyncio
        async def test_run_sqlite_only(db): ...

        Or to conditionally skip a class:

        @requires_features(supports_transactions=True)
        class TestTransactions:
            @pytest.mark.asyncio
            async def test_something(self, db): ...
    """

    def decorator(test_item: TestItem) -> TestItem:
        if not isinstance(test_item, type):

            def check_features() -> None:
                connection = FeatureConditions.get_connection(HareContext.require_current(), connection_alias)
                mismatch = FeatureConditions.get_mismatch(connection, conditions)
                if mismatch is not None:
                    raise SkipTest(mismatch)

            if inspect.iscoroutinefunction(test_item):

                @wraps(test_item)
                async def skip_wrapper(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
                    check_features()
                    return await test_item(*args, **kwargs)

            else:

                @wraps(test_item)
                def skip_wrapper(*args: typing.Any, **kwargs: typing.Any) -> typing.Any:
                    check_features()
                    return test_item(*args, **kwargs)

            return cast("TestItem", skip_wrapper)

        # Assume a class is decorated
        test_functions = {
            attribute_name: attribute
            for attribute_name in dir(test_item)
            if attribute_name.startswith("test_") and callable(attribute := getattr(test_item, attribute_name))
        }
        for name, test_function in test_functions.items():
            setattr(
                test_item,
                name,
                requires_features(connection_alias=connection_alias, **conditions)(test_function),
            )

        return test_item

    return decorator
