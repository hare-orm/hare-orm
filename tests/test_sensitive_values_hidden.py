"""A ``sensitive=True`` field's value never reaches a log, a ``QueryExecuted`` event or an error's
text: its parameter is shown as ``<hidden>`` - written, filtered by, updated, in bulk and through a
query plan alike. A statement binding no such value keeps a plain list of parameters."""

import os

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context
from hare.exceptions import OperationalError
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.sql.constants import HIDDEN_PARAMETER_TEXT
from hare.sql.terms.parameters.query_parameters import QueryParameters
from tests.sensitive_values_models import Account

SECRETS = ("P-100200", "P-300400", "tok-secret-1", "P-500600", "P-700800")


@pytest_asyncio.fixture
async def accounts():
    async with hare_test_context(
        modules=["tests.sensitive_values_models"],
        db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:"),
        app_label="models",
        connection_label="models",
    ) as context:
        yield context


@pytest.fixture
def events():
    collected: list[QueryExecuted] = []
    Observers.observe(QueryExecuted, collected.append)
    yield collected
    Observers.unobserve(QueryExecuted, collected.append)


def shown_text(caplog, events) -> str:
    return "\n".join([*caplog.messages, *(repr(event.parameters) for event in events)])


@pytest.mark.asyncio
async def test_no_write_or_filter_shows_a_sensitive_value(accounts, events, caplog, monkeypatch):
    monkeypatch.setattr(Observers, "slow_query_threshold_ms", -1.0)
    with caplog.at_level("DEBUG", logger="hare.db_client"):
        account = await Account.objects.create(id=1, name="ann", passport=SECRETS[0], token=SECRETS[2])
        account.passport = SECRETS[1]
        await account.save()
        for _ in range(2):
            # The second filter runs through the query plan the first one built.
            assert await Account.objects.filter(passport=SECRETS[1]).values_list("name", flat=True) == ["ann"]
            assert await Account.objects.filter(passport__contains="300").count() == 1
            assert await Account.objects.filter(passport__in=[SECRETS[1], SECRETS[3]]).count() == 1
        await Account.objects.filter(id=1).update(token=SECRETS[3])
        await Account.objects.bulk_create(
            [Account(id=2, name="bob", passport=SECRETS[4]), Account(id=3, name="cid", passport=SECRETS[4])]
        )
        others = await Account.objects.filter(id__in=[2, 3])
        for other in others:
            other.passport = SECRETS[3]
        await Account.objects.bulk_update(others, ["passport"])
    text = shown_text(caplog, events)
    assert HIDDEN_PARAMETER_TEXT in text
    assert "ann" in text
    for secret in (*SECRETS, "300"):
        assert secret not in text, secret


@pytest.mark.asyncio
async def test_a_statement_without_a_sensitive_value_binds_a_plain_list(accounts, events):
    await Account.objects.create(id=1, name="ann", passport=SECRETS[0])
    events.clear()
    assert await Account.objects.filter(name="ann").count() == 1
    assert [type(event.parameters) for event in events] == [list]


def test_an_error_shows_no_sensitive_value():
    params = QueryParameters(["ann", SECRETS[0]], [1])
    error = OperationalError(
        Exception(f"invalid input for query argument $2: '{SECRETS[0]}'"), sql="SELECT $1, $2", parameters=params
    )
    assert SECRETS[0] not in str(error)
    assert HIDDEN_PARAMETER_TEXT in str(error)
    assert error.parameters[1] == SECRETS[0]
    assert repr(params) == f"['ann', '{HIDDEN_PARAMETER_TEXT}']"
    assert params.copy().hidden_indexes == {1}
