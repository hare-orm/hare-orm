import logging

import pytest

from hare.contrib.test import requires_features
from hare.exceptions import ConfigurationError
from hare.instrumentation.declarations import QueryExecuted
from hare.instrumentation.observers.observers import Observers
from hare.instrumentation.queries.query_tags import QueryTags
from hare.transactions.transactions import Transactions


@pytest.mark.database_independent
def test_append_query_tags_is_a_noop_with_no_tags_active():
    sql = "SELECT 1"
    assert QueryTags.append(sql) is sql


@pytest.mark.database_independent
def test_append_query_tags_is_a_noop_with_an_empty_dict():
    sql = "SELECT 1"
    with QueryTags.scope():
        assert QueryTags.append(sql) is sql


@pytest.mark.database_independent
def test_append_query_tags_appends_a_trailing_comment():
    with QueryTags.scope(application="billing"):
        assert QueryTags.append("SELECT 1") == "SELECT 1 /*application='billing'*/"


@pytest.mark.database_independent
def test_a_tag_after_a_line_comment_goes_on_a_separate_line():
    """Appended to the same line, the tag ended up inside the comment - lost."""
    with QueryTags.scope(application="billing"):
        assert QueryTags.append("SELECT 1 -- total") == "SELECT 1 -- total\n/*application='billing'*/"
        assert QueryTags.append("-- head\nSELECT 1") == "-- head\nSELECT 1 /*application='billing'*/"


@pytest.mark.database_independent
def test_append_query_tags_sorts_keys():
    with QueryTags.scope(zebra="z", apple="a"):
        assert QueryTags.append("SELECT 1") == "SELECT 1 /*apple='a',zebra='z'*/"


@pytest.mark.database_independent
def test_append_query_tags_url_encodes_special_characters_in_values():
    with QueryTags.scope(path="/parent/actions/do_something", note="a b&c"):
        tagged = QueryTags.append("SELECT 1")
        assert tagged == "SELECT 1 /*note='a%20b%26c',path='%2Fparent%2Factions%2Fdo_something'*/"


@pytest.mark.database_independent
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


@pytest.mark.database_independent
def test_query_tags_resets_on_normal_exit():
    with QueryTags.scope(application="billing"):
        pass
    assert QueryTags.append("SELECT 1") == "SELECT 1"


@pytest.mark.database_independent
def test_query_tags_resets_on_exception():
    with pytest.raises(ValueError):
        with QueryTags.scope(application="billing"):
            raise ValueError("boom")
    assert QueryTags.append("SELECT 1") == "SELECT 1"


@pytest.mark.database_independent
def test_query_tags_nesting_does_not_merge_with_outer_tags():
    with QueryTags.scope(application="billing"):
        with QueryTags.scope(job="reconcile"):
            assert QueryTags.append("SELECT 1") == "SELECT 1 /*job='reconcile'*/"
        assert QueryTags.append("SELECT 1") == "SELECT 1 /*application='billing'*/"
    assert QueryTags.append("SELECT 1") == "SELECT 1"


@pytest.mark.database_independent
def test_set_and_reset_query_tags_directly():
    token = QueryTags.set({"application": "billing"})
    try:
        assert QueryTags.append("SELECT 1") == "SELECT 1 /*application='billing'*/"
    finally:
        QueryTags.reset(token)
    assert QueryTags.append("SELECT 1") == "SELECT 1"


@pytest.mark.asyncio
async def test_query_tags_appear_on_a_real_select_query(db):
    from tests.shared_models.testmodels import Author

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
    from tests.shared_models.testmodels import Author

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
            await db.get_connection().execute_script("SELECT 1")
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)

    assert calls, "the hook never recorded any query - the check below would be vacuous"
    assert calls[-1].endswith("/*application='billing'*/")


@requires_features(dialect="postgresql")
@pytest.mark.asyncio
async def test_query_tags_appear_on_a_copy_bulk_load(db):
    """A COPY went out without the tags - the statement the driver writes, and the reported one."""
    from tests.shared_models.testmodels import Tournament

    calls = []

    def hook(event):
        calls.append(event.sql)

    Observers.observe(QueryExecuted, hook)
    try:
        with QueryTags.scope(application="billing", note="a */ b"):
            await Tournament.objects.bulk_create(
                [Tournament(id=1, name="A"), Tournament(id=2, name="B")], use_copy=True
            )
        await Observers.wait_for_pending()
    finally:
        Observers.unobserve(QueryExecuted, hook)

    copy_calls = [sql for sql in calls if sql.startswith("COPY")]
    assert copy_calls and copy_calls[-1].endswith("/*application='billing',note='a%20%2A%2F%20b'*/")
    assert await Tournament.objects.order_by("id").values_list("name", flat=True) == ["A", "B"]


@pytest.mark.database_independent
@pytest.mark.parametrize("tags", [{"": "v"}, {"job": None}])
def test_a_tag_of_no_name_or_no_value_is_refused(tags):
    with pytest.raises(ConfigurationError), QueryTags.scope(**tags):
        pass


@pytest.mark.database_independent
def test_a_key_of_any_characters_is_written_escaped():
    with QueryTags.scope(**{"bad key*/": "v"}):
        assert QueryTags.append("SELECT 1") == "SELECT 1 /*bad%20key%2A%2F='v'*/"


@pytest.mark.database_independent
@pytest.mark.parametrize("tags", [{"job": "\ud800"}, {"\udfff": "v"}], ids=["value", "key"])
def test_a_tag_holding_a_lone_surrogate_is_refused(tags):
    """It was taken, and every query then failed with a raw UnicodeEncodeError from the comment."""
    with pytest.raises(ConfigurationError, match="lone surrogate"), QueryTags.scope(**tags):
        pass
