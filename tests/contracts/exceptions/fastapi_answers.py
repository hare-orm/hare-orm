from __future__ import annotations


class FastApiAnswers:
    """What a FastAPI application with hare's exception handlers answers a route raising an exception."""

    @staticmethod
    def get_answer(exception: Exception) -> tuple[int, str]:
        """The answer to a route raising an exception.

        Args:
            exception: The exception.

        Returns:
            The status and the body.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from hare.contrib.frameworks.fastapi import HareExceptionHandlers

        application = FastAPI(exception_handlers=HareExceptionHandlers.get_handlers())

        @application.get("/raise")
        async def raise_exception() -> None:
            raise exception

        with TestClient(application, raise_server_exceptions=False) as client:
            response = client.get("/raise")
        return response.status_code, response.text
