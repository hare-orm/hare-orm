"""Scripts on ClickHouse: split on the semicolons outside quotes and comments, a statement of comments
only left out of a script; a statement of no SQL sent alone is the server's to refuse."""

import pytest

from hare.exceptions import OperationalError
from tests.dialects.clickhouse.models import Team


@pytest.mark.asyncio
async def test_a_script_is_split_outside_comments(clickhouse_db):
    connection = Team._meta.connection
    script = "SELECT 1; -- one; two\nSELECT 'a;b' /* three; */; /* only a comment */ ;"
    assert connection.split_script(script) == ["SELECT 1", "-- one; two\nSELECT 'a;b' /* three; */"]
    await connection.execute_script(script)


@pytest.mark.asyncio
@pytest.mark.parametrize("script", ["", "   ", "-- a comment", "/* a comment */", ";"])
async def test_a_text_of_no_statement_is_refused_by_the_server(clickhouse_db, script):
    connection = Team._meta.connection
    await connection.execute_script(script)
    with pytest.raises(OperationalError):
        await connection.execute(script)
