import pytest

from hare import fields
from hare.exceptions import ConfigurationError
from hare.models import Model


def test_field_name_conflicts_with_model_attributes():
    """Test that using reserved model attribute names as field names raises ConfigurationError."""
    with pytest.raises(ConfigurationError) as exc_info:

        class BadModel(Model):
            save = fields.IntField()
            get_table = fields.IntField()

    message = str(exc_info.value)
    assert "save" in message
    assert "get_table" in message
