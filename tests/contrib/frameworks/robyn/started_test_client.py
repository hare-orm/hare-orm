from __future__ import annotations

from typing import Any, Self

from robyn.testing import TestClient


class StartedTestClient(TestClient):
    """Start and stop the Robyn application on the test client's loop."""

    def __enter__(self) -> Self:
        self._loop.run_until_complete(self.app.start_hare())
        return self

    def __exit__(self, *args: Any) -> None:
        self._loop.run_until_complete(self.app.stop_hare())
        super().__exit__(*args)
