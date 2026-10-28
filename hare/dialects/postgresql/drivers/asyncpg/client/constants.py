import inspect

import asyncpg

#: Every keyword asyncpg.create_pool() accepts, its own and those it forwards to asyncpg.connect()
#: - read from the installed asyncpg, so a credential outside it is rejected at configuration
#: time instead of by the first query.
ASYNCPG_CONNECTION_PARAMETERS = frozenset(
    name
    for function in (asyncpg.connect, asyncpg.create_pool)
    for argument_specification in (inspect.getfullargspec(function),)
    for name in (*argument_specification.args, *argument_specification.kwonlyargs)
)
