import logging

import pytest

from hare.contrib.test import requires_features
from hare.instrumentation.observers import Observers
from hare.instrumentation.query_executed import QueryExecuted
from hare.instrumentation.query_tags import QueryTags
from hare.transactions.transactions import Transactions


def test_append_query_tags_is_a_noop_with_no_tags_active():
    sql = "SELECT 1"
    assert QueryTags.append(sql) is sql


def test_append_query_tags_is_a_noop_with_an_empty_dict():
    sql = "SELECT 1"
    with QueryTags.scope():
        assert QueryTags.append(sql) is sql


def test_append_query_tags_appends_a_trailing_comment():
    with QueryTags.scope(application="billing"):
        assert QueryTags.append("SELECT 1") == "SELECT 1 /*application='billing'*/"


def test_append_query_tags_sorts_keys():
    with QueryTags.scope(zebra="z", apple="a"):
        assert QueryTags.append("SELECT 1") == "SELECT 1 /*apple='a',zebra='z'*/"


def test_append_query_tags_url_encodes_special_characters_in_values():
    with QueryTags.scope(path="/parent/actions/do_something", note="a b&c"):
        tagged = QueryTags.append("SELECT 1")
        assert tagged == "SELECT 1 /*note='a%20b%26c',path='%2Fparent%2Factions%2Fdo_something'*/"


def test_append_query_tags_url_encodes_special_characters_in_keys():
    """scope(**tags) accepts any string key via dict-unpacking (Python only enforces identifier
    syntax for a literal keyword argument, not for **some_dict) and set() takes an arbitrary dict
    directly with no restriction at all - an unescaped key containing the comment terminator
    "*/" could close the sqlcommenter comment early, turning whatever text follows into live,
    appended SQL (a real SQL-injection-shaped gap, not just cosmetic corruption), unlike the
    value half, which was already escaped."""
    token = QueryTags.set({"a*/; DROP TABLE t; --": "1"})
    try:
        tagged = QueryTags.append("SELECT 1")
    finally:
        QueryTags.reset(token)

    assert "*/" not in tagged.removesuffix("*/")
    assert tagged == "SELECT 1 /*a%2A%2F%3B%20DROP%20TABLE%20t%3B%20--='1'*/"


def test_query_tags_resets_on_normal_exit():
    with QueryTags.scope(application="billing"):
        pass
    assert QueryTags.append("SELECT 1") == "SELECT 1"


def test_query_tags_resets_on_exception():
    with pytest.raises(ValueError):
        with QueryTags.scope(application="billing"):
            raise ValueError("boom")
    assert QueryTags.append("SELECT 1") == "SELECT 1"


def test_query_tags_nesting_does_not_merge_with_outer_tags():
    with QueryTags.scope(application="billing"):
        with QueryTags.scope(job="reconcile"):
            assert QueryTags.append("SELECT 1") == "SELECT 1 /*job='reconcile'*/"
        assert QueryTags.append("SELECT 1") == "SELECT 1 /*application='billing'*/"
    assert QueryTags.append("SELECT 1") == "SELECT 1"


def test_set_and_reset_query_tags_directly():
    token = QueryTags.set({"application": "billing"})
    try:
        assert QueryTags.append("SELECT 1") == "SELECT 1 /*application='billing'*/"
    finally:
        QueryTags.reset(token)
    assert QueryTags.append("SELECT 1") == "SELECT 1"


@pytest.mark.asyncio
async def test_query_tags_appear_on_a_real_select_query(db):
    from tests.testmodels import Author

    calls = []

    def hook(event):
        sql = event.sql
        calls.append(sql)

    Observers.observe(QueryExecuted, hook)
    try:
        with QueryTags.scope(application="billing"):
            await Author.objects.all()
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)

    assert calls, "the hook never recorded any query - the check below would be vacuous"
    assert calls[-1].endswith("/*application='billing'*/")


@pytest.mark.asyncio
@requires_features(supports_streaming=True)
async def test_query_tags_appear_on_a_stream_query(db, caplog):
    """`.stream()` (`stream` on both Postgres drivers) is deliberately not wrapped
    in `translate_exceptions` - the wrapper that normally applies `QueryTags.append()` - and so
    has to apply it by hand instead (hare.dialects.postgresql.drivers.asyncpg.client.AsyncpgClient.
    stream, hare.dialects.postgresql.drivers.rust_pg.client.RustPgClient.stream). This
    guards against that being silently skipped again. `QueryInstrumentation`'s own query hook
    isn't usable here (it also only fires via translate_exceptions, a separate, already-documented
    gap on this same streaming path) - the tagged SQL is instead observed off the client's own
    debug log, which every backend writes the (already-tagged) query string to.
    """
    from tests.testmodels import Author

    await Author.objects.create(name="Some Author")

    caplog.set_level(logging.DEBUG, logger="hare.db_client")
    with QueryTags.scope(application="billing"):
        async with Transactions.atomic():
            async for _ in Author.objects.all().stream():
                pass

    tagged_lines = [message for message in caplog.messages if "/*application='billing'*/" in message]
    assert tagged_lines, "the tagged SQL comment never appeared on the streamed query's debug log"


@pytest.mark.asyncio
async def test_query_tags_appear_on_a_ddl_execute_script_call(db):
    calls = []

    def hook(event):
        sql = event.sql
        calls.append(sql)

    Observers.observe(QueryExecuted, hook)
    try:
        with QueryTags.scope(application="billing"):
            await db.db().execute_script("SELECT 1")
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)

    assert calls, "the hook never recorded any query - the check below would be vacuous"
    assert calls[-1].endswith("/*application='billing'*/")
