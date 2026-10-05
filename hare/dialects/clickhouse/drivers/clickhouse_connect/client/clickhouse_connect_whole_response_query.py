from __future__ import annotations

import asyncio
import importlib
from typing import Any, ClassVar

from clickhouse_connect.driver.asyncclient import AsyncClient
from clickhouse_connect.driver.common import dict_copy
from clickhouse_connect.driver.external import ExternalData

from hare.dialects.clickhouse.drivers.clickhouse_connect.constants import (
    CLICKHOUSE_CONNECT_ENCODING_HEADER,
    CLICKHOUSE_CONNECT_INLINE_PARSE_MAX_BYTES,
    CLICKHOUSE_CONNECT_TIME_ZONE_HEADER,
    CLICKHOUSE_CONNECT_WHOLE_RESPONSE_CLIENT_METHODS,
    CLICKHOUSE_CONNECT_WHOLE_RESPONSE_FUNCTIONS,
)


class ClickhouseConnectWholeResponseQuery:
    """A read through clickhouse-connect taking its response whole. The library's ``query()`` hands
    every response to a worker thread through a queue, a block at a time, and parses it there - for
    the few rows most reads return, more work than the read. The server finishes the result before it
    answers (``wait_end_of_query``), so the body read is the whole result; it is parsed here on the
    event loop by the library's own parser - a body over ``CLICKHOUSE_CONNECT_INLINE_PARSE_MAX_BYTES``
    on a worker thread, as the library parses it."""

    #: The library's functions the read is made of, by name - looked up by the first read; empty when
    #: the installed version lacks one, and the library's query() runs.
    library_functions: ClassVar[dict[str, Any]] = {}
    #: Whether the functions were looked up.
    looked_up: ClassVar[list[bool]] = []

    @classmethod
    def get_library_functions(cls) -> dict[str, Any]:
        """The library's functions the read is made of - looked up once.

        Returns:
            The functions by name; empty when the installed version lacks one.
        """
        if not cls.looked_up:
            cls.looked_up.append(True)
            try:
                functions = {
                    name: getattr(importlib.import_module(module_name), name)
                    for module_name, names in CLICKHOUSE_CONNECT_WHOLE_RESPONSE_FUNCTIONS
                    for name in names
                }
            except (ImportError, AttributeError):
                return cls.library_functions
            if all(hasattr(AsyncClient, name) for name in CLICKHOUSE_CONNECT_WHOLE_RESPONSE_CLIENT_METHODS):
                cls.library_functions.update(functions)
        return cls.library_functions

    @classmethod
    async def run(cls, library_client: AsyncClient, sql: str, external_data: ExternalData | None = None) -> Any:
        """Runs a read, as the library's ``query()`` runs it.

        Args:
            library_client: The library client.
            sql: The statement.
            external_data: The external tables it reads.

        Returns:
            The library's result.
        """
        functions = cls.get_library_functions()
        if not functions:
            return await library_client.query(sql, external_data=external_data)
        # Typed Any: the library's own types are there only when it is installed with them.
        context: Any = library_client.create_query_context(query=sql, external_data=external_data)
        context.rename_response_column = library_client._rename_response_column
        if library_client.protocol_version:
            context.block_info = True
        runtime = functions["QueryRuntime"](
            database=library_client.database,
            protocol_version=library_client.protocol_version,
            settings=library_client._validate_settings(context.settings),
            retries=library_client.query_retries,
            retryable=functions["_query_is_read_only"](context.final_query),
        )
        backend = library_client._backend
        plan = functions["plan_query_request"](
            context,
            runtime,
            form_encode_query_params=backend.form_encode_query_params,
            compression=backend.compression,
            send_comp_setting=backend.send_comp_setting,
            read_format=backend.read_format,
            prepped_query=library_client._prep_query(context),
        )
        if plan.columns_only:
            return await library_client.query(sql, external_data=external_data)
        files = functions["_plan_files"](plan)
        response = await backend.request(
            plan.body,
            plan.params,
            dict_copy(plan.headers, context.transport_settings),
            files=files,
            retries=runtime.retries,
            retryable=functions["_read_request_retryable"](
                runtime.retryable, plan.body, plan.params, backend.client_settings, files
            ),
        )
        try:
            body = await response.read()
            headers = response.headers
        finally:
            functions["release_lease"](response)
        encoding = headers.get(CLICKHOUSE_CONNECT_ENCODING_HEADER)
        context.set_response_tz(library_client._check_tz_change(headers.get(CLICKHOUSE_CONNECT_TIME_ZONE_HEADER)))

        def parse() -> Any:
            data = functions["decompress_response"](body, encoding) if encoding else body
            result: Any = library_client._transform.parse_response(
                functions["RespBuffCls"](functions["BytesSource"](data)), context
            )
            # The rows read now - a result reads them on first use.
            _ = result.result_set
            return result

        if len(body) > CLICKHOUSE_CONNECT_INLINE_PARSE_MAX_BYTES:
            result = await asyncio.get_running_loop().run_in_executor(None, parse)
        else:
            result = parse()
        result.summary = functions["summary_from_headers"](headers)
        return result
