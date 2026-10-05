"""The values of ``sensitive=True`` fields never reach a query observer - whichever write sends them."""

from collections.abc import Generator
from contextlib import contextmanager

import pytest

from hare.contrib.test import requires_features
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from tests.writes.rules.write_rules_models import RuleDocument, RuleTag

#: The sensitive value the tests write - it must show in no parameter.
SECRET_VALUE = "SECRET-7731"


@contextmanager
def observed_parameters() -> Generator[list[str]]:
    shown_parameters: list[str] = []
    with Observers.observing(QueryExecuted, lambda event: shown_parameters.append(repr(event.parameters))):
        yield shown_parameters


@requires_features(supports_merge=True)
@pytest.mark.asyncio
async def test_a_merge_hides_a_sensitive_value(rules_db):
    await RuleDocument.objects.create(id=1, title="a")
    with observed_parameters() as shown_parameters:
        await RuleDocument.objects.merge([{"id": 1}], on="id").when_matched(update={"secret": SECRET_VALUE})
    assert (await RuleDocument.objects.get(id=1)).secret == SECRET_VALUE
    assert shown_parameters
    assert all(SECRET_VALUE not in parameters for parameters in shown_parameters)


@pytest.mark.asyncio
async def test_through_defaults_hide_a_sensitive_value(rules_db):
    document = await RuleDocument.objects.create(id=1, title="a")
    tag = await RuleTag.objects.create(id=1, name="t")
    with observed_parameters() as shown_parameters:
        await document.tags.add(tag, through_defaults={"note": SECRET_VALUE})
    assert shown_parameters
    assert all(SECRET_VALUE not in parameters for parameters in shown_parameters)
