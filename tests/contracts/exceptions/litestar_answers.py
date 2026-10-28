from __future__ import annotations


class LitestarAnswers:
    """What a Litestar application with hare's exception handlers answers a route raising an exception."""

    @staticmethod
    def get_answer(exception: Exception) -> tuple[int, str]:
        """The answer to a route raising an exception.

        Args:
            exception: The exception.

        Returns:
            The status and the body.
        """
        from litestar import Litestar, get
        from litestar.testing import TestClient

        from hare.contrib.frameworks.litestar import HareExceptionHandlers

        @get("/raise", sync_to_thread=False)
        def raise_exception() -> None:
            raise exception

        application = Litestar([raise_exception], exception_handlers=HareExceptionHandlers.get_handlers())
        with TestClient(application) as client:
            response = client.get("/raise")
        return response.status_code, response.text
