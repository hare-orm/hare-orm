"""A name or a list of names given as something else is refused with the method's own error - never
an AttributeError of the value, and never a string read letter by letter as a list of names."""

import pytest

from hare.exceptions import FieldError, QueryError
from tests.testmodels import Tournament


@pytest.mark.parametrize(
    ("make", "error", "message"),
    [
        (lambda: Tournament.objects.distinct(1), QueryError, r"distinct\(\) takes field names, got 1"),
        (lambda: Tournament.objects.only(1), QueryError, r"only\(\) takes field names, got 1"),
        (lambda: Tournament.objects.values("name", 1), QueryError, r"values\(\) takes field names, got 1"),
        (lambda: Tournament.objects.values_list(None), QueryError, r"values_list\(\) takes field names, got None"),
        (
            lambda: Tournament.objects.filter(id=1).update(name="b").returning("name", old="name"),
            FieldError,
            r"returning\(old=\.\.\.\) takes a list of field names, got the string 'name'",
        ),
        (
            lambda: Tournament.objects.select_for_update(of="self"),
            QueryError,
            r"select_for_update\(of=\.\.\.\) takes a tuple of paths, got the string 'self'",
        ),
    ],
)
@pytest.mark.asyncio
async def test_a_wrong_type_is_refused_with_the_methods_error(db, make, error, message):
    with pytest.raises(error, match=message):
        make()
