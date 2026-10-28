from __future__ import annotations

from tests.contracts.constants import SERVER_ERROR_STATUS


class RobynAnswers:
    """What hare's exception handler of Robyn answers an exception with - an exception it leaves to Robyn is
    raised again, and Robyn answers it with a 500."""

    @staticmethod
    def get_answer(exception: Exception) -> tuple[int, str]:
        """The answer to an exception.

        Args:
            exception: The exception.

        Returns:
            The status and the body.
        """
        from hare.contrib.frameworks.robyn import HareExceptionHandlers

        try:
            response = HareExceptionHandlers.handle(exception)
        except Exception as error:
            if error is not exception:
                raise
            return SERVER_ERROR_STATUS, ""
        description = response.description
        return int(response.status_code), description if isinstance(description, str) else description.decode()
