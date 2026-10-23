from __future__ import annotations

import getpass
import socket
import time
from collections.abc import Callable
from typing import Any, ClassVar

from clickhouse_driver.clientinfo import ClientInfo
from clickhouse_driver.opentelemetry import OpenTelemetryTraceContext

from hare.dialects.clickhouse.drivers.clickhouse_driver.constants import CLICKHOUSE_DRIVER_MICROSECONDS_PER_SECOND


class ClickhouseDriverClientInfo:
    """The description of the client clickhouse-driver sends with each statement, made with the OS
    user and the host name read once - the library reads both, a system call each, for every
    statement."""

    #: The library's own making of the description - kept to tell it was replaced.
    library_initializers: ClassVar[list[Callable[..., None]]] = []
    #: The OS user and the host name, read by the first statement.
    user_and_host_names: ClassVar[list[tuple[str, str]]] = []

    @classmethod
    def install(cls) -> None:
        """Replaces the library's making of the description - once."""
        if cls.library_initializers:
            return
        cls.library_initializers.append(ClientInfo.__init__)
        ClientInfo.__init__ = cls.initialize

    @staticmethod
    def initialize(client_info: Any, client_name: str, context: Any, client_revision: int) -> None:
        """Makes the description as the library does, the OS user and the host name read once.

        Args:
            client_info: The library's description.
            client_name: The client's name.
            context: The connection's context.
            client_revision: The protocol revision of the client.
        """
        client_info.query_kind = ClientInfo.QueryKind.NO_QUERY
        client_info.os_user, client_info.client_hostname = ClickhouseDriverClientInfo.get_user_and_host_names()
        client_info.client_name = client_name
        client_info.client_revision = client_revision
        client_settings = context.client_settings
        client_info.client_trace_context = OpenTelemetryTraceContext(
            client_settings["opentelemetry_traceparent"], client_settings["opentelemetry_tracestate"]
        )
        client_info.quota_key = client_settings["quota_key"]
        client_info.distributed_depth = 0
        start_time = time.time()
        client_info.initial_query_start_time_microseconds = int(start_time * CLICKHOUSE_DRIVER_MICROSECONDS_PER_SECOND)

    @staticmethod
    def get_user_and_host_names() -> tuple[str, str]:
        """The OS user - empty when there is none - and the host name, read once.

        Returns:
            The names.
        """
        user_and_host_names = ClickhouseDriverClientInfo.user_and_host_names
        if not user_and_host_names:
            try:
                user = getpass.getuser()
            except (KeyError, OSError):
                user = ""
            user_and_host_names.append((user, socket.gethostname()))
        return user_and_host_names[0]
