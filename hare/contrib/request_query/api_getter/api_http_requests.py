from __future__ import annotations

import asyncio
import time
import urllib.parse
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from hare.contrib.package_checks import PackageChecks
from hare.contrib.request_query.api_getter.constants import API_RETRY_AFTER_HEADER
from hare.contrib.request_query.api_getter.declarations import ApiRequestExecuted
from hare.core.log import logger
from hare.instrumentation.observers.observers import Observers

if TYPE_CHECKING:
    from hare.contrib.request_query.api_getter.api_source import ApiSource

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]


class ApiHttpRequests:
    """The HTTP requests of one run of ``ApiGetter`` - one client and its connections for every source, each
    source's requests at most ``ApiSource.concurrency`` at a time and repeated by its ``ApiSource.retry``,
    each attempt reported (``ApiRequestExecuted``). Needs the ``http`` extra.

    Raises:
        ConfigurationError: The ``httpx`` package isn't installed.
    """

    #: The ``httpx`` package, None without the ``http`` extra.
    httpx: Any = httpx
    #: The transport of the client - None for httpx's own; a test gives an ``httpx.MockTransport``.
    transport: Any = None

    def __init__(self) -> None:
        PackageChecks.require_package(self.httpx, "ApiGetter", "httpx", "http")
        self.client = self.httpx.AsyncClient(transport=self.transport)
        self.semaphores: dict[int, asyncio.Semaphore] = {}
        self.headers_by_source: dict[int, asyncio.Task[Mapping[str, str]]] = {}

    async def __aenter__(self) -> ApiHttpRequests:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        # A run ended by an error or a cancellation leaves no call of a headers function behind.
        pending_tasks = [task for task in self.headers_by_source.values() if not task.done()]
        for task in pending_tasks:
            task.cancel()
        await asyncio.gather(*pending_tasks, return_exceptions=True)
        await self.client.aclose()

    async def get(self, source: ApiSource, url: str, params: Mapping[str, Any] | None) -> tuple[Any, Exception | None]:
        """Asks a source - repeated as its ``retry`` says.

        Args:
            source: The source.
            url: The URL.
            params: The query parameters, None for none.

        Returns:
            The last answer and None, or None and what failed the request the last time - the connection, or
            reading the answer.
        """
        headers = await self.get_headers(source)
        semaphore = self.semaphores.setdefault(id(source), asyncio.Semaphore(source.concurrency))
        response: Any = None
        error: Exception | None = None
        async with semaphore:
            for attempt in range(source.retry.attempts + 1):
                started = time.perf_counter()
                response, error = None, None
                try:
                    response = await self.client.get(
                        url, params=params, headers=headers, timeout=source.timeout_seconds
                    )
                except self.httpx.HTTPError as http_error:
                    # A broken connection, and an answer that can't be read - a body that doesn't decode.
                    error = http_error
                ApiHttpRequests.report(source, url, response, error, started, attempt)
                if error is not None:
                    retried = isinstance(error, self.httpx.TransportError)
                else:
                    retried = response.status_code in source.retry.status_codes
                if not retried or attempt == source.retry.attempts:
                    break
                retry_after = response.headers.get(API_RETRY_AFTER_HEADER) if response is not None else None
                await asyncio.sleep(source.retry.get_delay(attempt, retry_after))
        return response, error

    async def get_headers(self, source: ApiSource) -> Mapping[str, str]:
        """The headers of a source's requests - its function giving them called once a run.

        Args:
            source: The source.

        Returns:
            The headers.
        """
        if isinstance(source.headers, Mapping):
            return source.headers
        task = self.headers_by_source.get(id(source))
        if task is None:
            task = asyncio.ensure_future(source.headers())
            self.headers_by_source[id(source)] = task
        return await asyncio.shield(task)

    @staticmethod
    def get_shown_url(url: str) -> str:
        """A URL as the log and the events show it - without its query, its fragment and the credentials
        in it, which may carry a key.

        Args:
            url: The URL.

        Returns:
            The URL shown.
        """
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc.rpartition("@")[2]
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, "", ""))

    @staticmethod
    def report(
        source: ApiSource, url: str, response: Any, error: Exception | None, started: float, attempt: int
    ) -> None:
        """Reports an attempt of a request - to hare's log and to the observers of ``ApiRequestExecuted``.

        Args:
            source: The source.
            url: The URL.
            response: The answer, None when the connection failed.
            error: What failed the connection, None with an answer.
            started: When the attempt started (``time.perf_counter()``).
            attempt: How many repeats came before it.
        """
        duration_seconds = time.perf_counter() - started
        status_code = response.status_code if response is not None else None
        error_text = f"{type(error).__name__}: {error}" if error is not None else None
        shown_url = ApiHttpRequests.get_shown_url(url)
        logger.debug(
            "ApiGetter: %s GET %s -> %s in %.3fs, attempt %d",
            source.get_name(),
            shown_url,
            status_code if error_text is None else error_text,
            duration_seconds,
            attempt,
        )
        if Observers.is_observed(ApiRequestExecuted):
            Observers.notify(
                ApiRequestExecuted(
                    source_name=source.get_name(),
                    url=shown_url,
                    status_code=status_code,
                    duration_seconds=duration_seconds,
                    attempt=attempt,
                    error=error_text,
                )
            )
