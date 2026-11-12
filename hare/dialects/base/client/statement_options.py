from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Any, ClassVar

from hare.core.caching.cache import Cache

if TYPE_CHECKING:
    from hare.dialects.base.client.database_client import DatabaseClient


class StatementOptions:
    """Compose the configured driver-call layers without combining their responsibilities.

    Chains retain unbound functions only. The client is supplied on every call, including
    a transaction wrapper; a shared bucket therefore retains no transaction or connection.
    Timeout and password renewal keep their existing implementations and nesting order.
    """

    operations: ClassVar[Cache[Callable[..., Any]]] = Cache(holds_sql=False)

    @staticmethod
    def get_operation(client: DatabaseClient, function: Callable[..., Any], observed: bool) -> Callable[..., Any]:
        """The unbound call chain for this client's current settings.

        A changed timeout value is read by its layer when called. Enabling or disabling
        a layer selects another chain, so settings changed after construction take effect.
        """
        if not client.runs_statement_options:
            return function
        timeout = observed and client.command_timeout is not None
        renews_password = client.password_provider is not None and not client.is_transaction_client
        if not timeout and not renews_password:
            return function
        key = (function, timeout, renews_password)
        operations = StatementOptions.operations.get_bucket(type(client))
        operation = operations.get(key)
        if operation is None:
            operation = function
            if timeout:
                operation = partial(StatementOptions.run_with_command_timeout, operation)
            if renews_password:
                renewal: Callable[..., Any] = type(client).run_renewing_refused_password  # type: ignore[assignment]
                operation = partial(renewal, operation)
            operations[key] = operation
        return operation

    @staticmethod
    async def run_with_command_timeout(
        function: Callable[..., Any], client: DatabaseClient, *args: Any, **kwargs: Any
    ) -> Any:
        """Apply the configured deadline to one statement attempt.

        TimeoutError raised by the operation itself retains its original message. Only an expired
        deadline is reported as a command timeout; transaction control statements use no layer.
        """
        try:
            async with asyncio.timeout(client.command_timeout) as timeout_scope:
                return await function(client, *args, **kwargs)
        except TimeoutError:
            if not timeout_scope.expired():
                raise
            raise TimeoutError(f"Command timed out after {client.command_timeout}s") from None
