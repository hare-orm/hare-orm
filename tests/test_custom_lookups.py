import pytest
import pytest_asyncio

from hare.contrib.test import requires_features
from hare.fields import CharField
from hare.query.filters import FieldLookup
from hare.sql.terms import Term
from tests.testmodels import CharFields


def _reversed_lookup(field: CharField | None) -> FieldLookup:
    def _operator(term: Term, value: str) -> Term:
        return term == value[::-1]

    return FieldLookup(_operator)


CharField.register_lookup("reversed", _reversed_lookup)


@pytest_asyncio.fixture
async def char_fields_data(db):
    await CharFields.objects.create(char="moo")
    await CharFields.objects.create(char="oom")


@requires_features(identifier_quote_char='"')
@pytest.mark.asyncio
async def test_register_lookup_generates_sql(db, char_fields_data):
    sql = CharFields.objects.filter(char__reversed="oom").sql()
    assert '"char"' in sql


@pytest.mark.asyncio
async def test_register_lookup_filters_correctly(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__reversed="oom").values_list("char", flat=True)) == {"moo"}


@pytest.mark.asyncio
async def test_register_lookup_does_not_shadow_builtin_suffixes(db, char_fields_data):
    assert set(await CharFields.objects.filter(char__not="moo").values_list("char", flat=True)) == {"oom"}
