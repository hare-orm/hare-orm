"""Exception x integration matrix: every exception hare raises, left to an integration's route, is answered with
the status of the nearest class the integrations name (404, 409, 422, 403, 502) or the framework's 500, and its
message - a database's, naming tables and columns - is in the answer only where the answer is the message (a
refused value, a forbidden parameter)."""

import pytest

from tests.contracts.constants import EXCEPTION_SECRET, EXCEPTIONS_NOT_HANDLED_BY_INTEGRATION
from tests.contracts.exceptions.fastapi_answers import FastApiAnswers
from tests.contracts.exceptions.hare_exceptions import HareExceptions
from tests.contracts.exceptions.litestar_answers import LitestarAnswers
from tests.contracts.exceptions.robyn_answers import RobynAnswers
from tests.contracts.hare_registry import HareRegistry

pytestmark = pytest.mark.database_independent

INTEGRATIONS = {
    "fastapi": (FastApiAnswers, ("fastapi", "http")),
    "litestar": (LitestarAnswers, ("litestar",)),
    "robyn": (RobynAnswers, ("robyn",)),
}
EXCEPTION_CLASSES = HareExceptions.get_classes()
CELLS = [
    pytest.param(
        integration,
        answers,
        exception_class,
        id=f"{integration}-{HareRegistry.get_path(exception_class)}",
        marks=pytest.mark.requires(*extras),
    )
    for integration, (answers, extras) in INTEGRATIONS.items()
    for exception_class in EXCEPTION_CLASSES
    if HareRegistry.get_path(exception_class) not in EXCEPTIONS_NOT_HANDLED_BY_INTEGRATION.get(integration, ())
]


@pytest.mark.parametrize(("integration", "answers", "exception_class"), CELLS)
def test_an_integration_answers_an_exception_by_its_status_and_hides_the_message(
    integration, answers, exception_class
):
    exception = HareExceptions.make(exception_class)
    status, body = answers.get_answer(exception)
    assert status == HareExceptions.get_expected_status(exception_class, integration)
    if not HareExceptions.answers_with_message(exception_class):
        assert EXCEPTION_SECRET not in body
        assert "secret_table" not in body


def test_there_are_exceptions_to_answer():
    assert len(EXCEPTION_CLASSES) > 50
