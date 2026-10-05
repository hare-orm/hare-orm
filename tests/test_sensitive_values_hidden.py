"""A ``sensitive=True`` field's value never reaches a log, a ``QueryExecuted`` event or an error's
text: its parameter is shown as ``<hidden>`` - written, filtered by, updated, in bulk and through a
query plan alike. A statement binding no such value keeps a plain list of parameters."""

import os
import traceback

import pytest
import pytest_asyncio

from hare.contrib.test import hare_test_context
from hare.exceptions import IntegrityError, OperationalError, QueryError
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.query.expressions import Q
from hare.query.expressions.conditions.hidden_values_q import HiddenValuesQ
from hare.sql.constants import HIDDEN_PARAMETER_TEXT
from hare.sql.terms.parameters.query_parameters import QueryParameters
from tests.sensitive_values_models import Account, Badge, Passport, Profile, RegionAccount, Trip, Visa

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
async def test_a_cursor_boundary_shows_no_sensitive_value(accounts, events, caplog, monkeypatch):
    monkeypatch.setattr(Observers, "slow_query_threshold_ms", -1.0)
    await Account.objects.bulk_create(
        [Account(id=index, name=f"n{index}", passport=secret) for index, secret in enumerate(SECRETS[:3], 1)]
    )
    ordered = Account.objects.order_by("passport", "id")
    with caplog.at_level("DEBUG", logger="hare.db_client"):
        for _ in range(2):
            # The second page runs through the query plan the first one built.
            assert len(await ordered.after_cursor(SECRETS[0], 1)) == 2
            assert len(await ordered.before_cursor(SECRETS[2], 2)) == 2
    text = shown_text(caplog, events)
    assert HIDDEN_PARAMETER_TEXT in text
    for secret in SECRETS[:3]:
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


def get_error_text(error: BaseException) -> str:
    return "".join(traceback.format_exception(error))


@pytest.mark.asyncio
async def test_a_refused_write_shows_no_sensitive_value_in_its_error_or_its_cause(accounts):
    """The driver's error, chained as the cause, carried the database's DETAIL with the value; a
    bulk insert binding a column of values hid the column but not each value of it; and the checks
    hare runs in a database's place wrote the value into their messages."""
    features = Account._meta.connection.features
    if not (features.supports_unique_constraints or features.checks_constraints_before_write):
        pytest.skip("the database keeps no uniqueness, and hare checks none")
    await Account.objects.create(id=1, name="ann", passport=SECRETS[0])
    await Badge.objects.create(id=1, code=SECRETS[1])
    renamed_badge = await Badge.objects.create(id=6, code=SECRETS[0])
    renamed_badge.code = SECRETS[1]
    refused_writes = (
        lambda: Badge.objects.bulk_update([renamed_badge], ["code"]),
        lambda: Badge.objects.create(id=2, code=SECRETS[1]),
        lambda: Badge.objects.bulk_create([Badge(id=3, code=SECRETS[1]), Badge(id=4, code=SECRETS[3])]),
        lambda: Badge.objects.create(id=5, code=SECRETS[4], holder_id=7560000),
    )
    for refused_write in refused_writes:
        with pytest.raises(IntegrityError) as error_info:
            await refused_write()
        error_text = get_error_text(error_info.value)
        # Neither chained nor kept as the context - the driver's error holds the value.
        assert error_info.value.__context__ is None
        for secret in (SECRETS[1], "7560000"):
            assert secret not in error_text, error_text
            assert secret not in repr(error_info.value)


@pytest.mark.asyncio
async def test_a_conflict_of_get_or_create_values_shows_no_sensitive_value(accounts):
    with pytest.raises(QueryError) as error_info:
        await Account.objects.get_or_create(passport=SECRETS[0], defaults={"name": "ann", "passport": SECRETS[1]})
    error_text = get_error_text(error_info.value)
    assert HIDDEN_PARAMETER_TEXT in error_text
    assert SECRETS[0] not in error_text
    assert SECRETS[1] not in error_text
    with pytest.raises(QueryError, match="default_value='bob' vs query_value='ann'"):
        await Account.objects.get_or_create(name="ann", defaults={"name": "bob", "passport": SECRETS[1]})


def test_an_error_shows_no_value_of_a_hidden_column_or_row():
    column_parameters = QueryParameters([["ann", "bob"], [SECRETS[0], None]], [1])
    row_parameters = [QueryParameters(["ann", SECRETS[1]], [1]), QueryParameters(["bob", SECRETS[3]], [1])]
    for parameters, secret in ((column_parameters, SECRETS[0]), (row_parameters, SECRETS[3])):
        error = IntegrityError(Exception(f"Key (passport)=({secret}) already exists."), parameters=parameters)
        assert secret not in str(error)


@pytest.mark.asyncio
async def test_no_logger_of_a_driver_shows_a_sensitive_value(accounts, caplog):
    """clickhouse-driver logs every statement it sends, and hare writes the values into its text."""
    with caplog.at_level("DEBUG"):
        account = await Account.objects.create(id=1, name="ann", passport=SECRETS[0], token=SECRETS[2])
        account.passport = SECRETS[1]
        await account.save()
        assert await Account.objects.filter(passport=SECRETS[1]).count() == 1
        await Account.objects.filter(id=1).update(token=SECRETS[3])
        await Account.objects.bulk_create([Account(id=2, name="bob", passport=SECRETS[4])])
    assert "ann" in caplog.text
    for secret in SECRETS:
        assert secret not in caplog.text, secret


@pytest.mark.asyncio
async def test_a_check_of_a_sensitive_relation_shows_no_value(accounts, events, caplog, monkeypatch):
    """A database keeping no foreign keys has hare look the related rows up by the relation's values -
    on the related key, a field that isn't sensitive itself, so the lookup showed them."""
    monkeypatch.setattr(Observers, "slow_query_threshold_ms", -1.0)
    await Account.objects.create(id=7560001, name="ann", passport=SECRETS[0])
    await Account.objects.create(id=7560002, name="bob", passport=SECRETS[1])
    events.clear()
    with caplog.at_level("DEBUG", logger="hare.db_client"):
        # The second check runs through the query plan the first one built.
        await Badge.objects.create(id=1, code=SECRETS[0], holder_id=7560001)
        await Badge.objects.create(id=2, code=SECRETS[1], holder_id=7560002)
        await Badge.objects.bulk_create([Badge(id=3, code=SECRETS[3], holder_id=7560001)])
    text = shown_text(caplog, events)
    assert HIDDEN_PARAMETER_TEXT in text
    for secret in ("7560001", "7560002"):
        assert secret not in text, text
    assert await Badge.objects.filter(holder_id=7560001).count() == 2


@pytest.mark.asyncio
async def test_a_hidden_values_condition_keeps_its_class_through_filter_calls(accounts, events, monkeypatch):
    """filter() and ~ copied a condition as a plain Q - a subclass resolving otherwise was lost."""
    monkeypatch.setattr(Observers, "slow_query_threshold_ms", -1.0)
    await Account.objects.create(id=1, name="ann-7560003", passport=SECRETS[0])
    events.clear()
    for _ in range(2):
        # The second filter runs through the query plan the first one built.
        assert await Account.objects.filter(HiddenValuesQ(name="ann-7560003")).count() == 1
        assert await Account.objects.exclude(~HiddenValuesQ(name__in=["ann-7560003"])).count() == 1
    assert len(events) == 4
    assert all("7560003" not in repr(event.parameters) for event in events)
    assert await Account.objects.filter(Q(name="ann-7560003")).count() == 1
    assert "7560003" in repr(events[-1].parameters)


@pytest.mark.asyncio
async def test_a_key_reaching_a_sensitive_field_shows_no_value(accounts, events, caplog, monkeypatch):
    """A part of a composite key, a key inside a sensitive JSON field, a sensitive primary key and the
    relations keyed by it - filtered, saved, deleted, linked - bound their values shown."""
    monkeypatch.setattr(Observers, "slow_query_threshold_ms", -1.0)
    assert "passport_id" in Visa._meta.sensitive_fields
    with caplog.at_level("DEBUG", logger="hare.db_client"):
        await Profile.objects.create(id=1, settings={"token": "tok-secret-1"})
        assert await Profile.objects.filter(settings__token="tok-secret-1").count() == 1
        assert await Profile.objects.filter(settings__token__in=["tok-secret-1"]).count() == 1
        region_account = await RegionAccount.objects.create(region="P-100200", number=1)
        assert await RegionAccount.objects.filter(pk__in=[("P-100200", 1)]).count() == 1
        assert await RegionAccount.objects.exclude(pk__in=[("P-100200", 1)]).count() == 0
        region_account.note = "changed"
        await region_account.save()
        await region_account.refresh_from_db()
        await region_account.delete()
        passport = await Passport.objects.create(code="P-300400", name="ann")
        await Visa.objects.create(id=1, passport=passport)
        assert await Visa.objects.filter(passport=passport).count() == 1
        assert await Visa.objects.filter(passport_id="P-300400").count() == 1
        assert await passport.visas.all().count() == 1
        trip = await Trip.objects.create(id=1)
        await trip.passports.add(passport)
        assert [linked.code for linked in await trip.passports.all()] == ["P-300400"]
        await trip.passports.remove(passport)
        passport.name = "bob"
        await passport.save()
        await passport.delete()
    text = shown_text(caplog, events)
    for secret in ("tok-secret-1", "P-100200", "P-300400"):
        assert secret not in text, secret
