import pytest

from hare.exceptions import ValidationError
from tests.shared_models import testmodels


@pytest.mark.parametrize("value", [2, -5, 1.5, [1], [], object(), b"x"])
@pytest.mark.asyncio
async def test_a_value_that_is_no_boolean_is_refused(db, value):
    """bool([1]) and bool(object()) are True - any object was stored as one."""
    with pytest.raises(ValidationError, match="boolean: expected a bool"):
        await testmodels.BooleanFields.objects.create(id=1, boolean=value)
