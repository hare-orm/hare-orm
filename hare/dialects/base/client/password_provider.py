from __future__ import annotations

import asyncio
import contextlib
import inspect
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from hare.core.log import db_client_logger
from hare.dialects.base.connection.constants import DEFAULT_PASSWORD_REFRESH_SECONDS
from hare.exceptions import ConfigurationError


class PasswordProvider:
    """Where a connection takes its password from - a function returning it, plain or async: a cloud
    IAM token, a secret store's dynamic credential. The password is asked for again once it is
    older than ``refresh_seconds``, and right away after the server refused it.

    Args:
        get_password: The function.
        refresh_seconds: How long a password is used before the function is asked again.
    """

    __slots__ = ("get_password", "refresh_seconds", "password", "fetched_at", "_fetch_lock", "_refresh_task")

    def __init__(self, get_password: Callable[[], str | Awaitable[str]], refresh_seconds: float) -> None:
        self.get_password = get_password
        self.refresh_seconds = refresh_seconds
        #: The last password the function gave - None before the first one, or after a refusal.
        self.password: str | None = None
        #: ``time.monotonic()`` of when the function gave it.
        self.fetched_at = 0.0
        self._fetch_lock = asyncio.Lock()
        self._refresh_task: asyncio.Task[None] | None = None

    @classmethod
    def from_settings(cls, settings: Mapping[str, Any], password: str | None) -> PasswordProvider | None:
        """The provider a connection's checked settings name (``PASSWORD_PROVIDER_OPTIONS``).

        Args:
            settings: The checked settings - ``password_provider`` and ``password_refresh_seconds``.
            password: The connection's fixed password, None for none.

        Returns:
            The provider, None for a connection without one.

        Raises:
            ConfigurationError: The settings name a fixed password and a provider, or a refresh
                without a provider.
        """
        get_password = settings.get("password_provider")
        refresh_seconds = settings.get("password_refresh_seconds")
        if get_password is None:
            if refresh_seconds is not None:
                raise ConfigurationError(
                    "password_refresh_seconds needs password_provider - the function the password is asked for again"
                )
            return None
        if password is not None:
            raise ConfigurationError(
                "password and password_provider exclude each other - the provider gives the password"
            )
        return cls(get_password, DEFAULT_PASSWORD_REFRESH_SECONDS if refresh_seconds is None else refresh_seconds)

    async def get(self) -> str:
        """The password to open a connection with - asked for when there is none yet or it is
        older than ``refresh_seconds``.

        Returns:
            The password.

        Raises:
            ConfigurationError: The function gave no password.
        """
        password = self.password
        if password is not None and time.monotonic() - self.fetched_at < self.refresh_seconds:
            return password
        return await self.fetch(password)

    async def fetch(self, stale_password: str | None = None) -> str:
        """Asks the function for the password now - once for the connections waiting for it at the
        same time.

        Args:
            stale_password: The password the caller found too old or refused; a fetch that already
                replaced it while the caller waited is used as it is.

        Returns:
            The password.

        Raises:
            ConfigurationError: The function gave no password.
        """
        async with self._fetch_lock:
            if self.password is not None and self.password != stale_password:
                return self.password
            password = self.get_password()
            if inspect.isawaitable(password):
                password = await password
            if not isinstance(password, str) or not password:
                raise ConfigurationError(
                    f"password_provider must return a non-empty string, got {type(password).__name__}"
                )
            self.password = password
            self.fetched_at = time.monotonic()
            return password

    def start_refreshing(self, apply_password: Callable[[str], None]) -> None:
        """Gives ``apply_password`` a new password every ``refresh_seconds`` in the background - for a
        driver whose pool opens its connections itself.

        Args:
            apply_password: Takes the new password.
        """
        if self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self._refresh(apply_password))

    async def _refresh(self, apply_password: Callable[[str], None]) -> None:
        while True:
            await asyncio.sleep(self.refresh_seconds)
            try:
                apply_password(await self.fetch(self.password))
            except Exception as error:
                # The pool keeps the last password; the next refresh or a refused login asks again.
                db_client_logger.warning("password_provider failed to give a new password: %s", error)

    async def stop_refreshing(self) -> None:
        """Stops the background refresh."""
        refresh_task = self._refresh_task
        if refresh_task is None:
            return
        self._refresh_task = None
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
