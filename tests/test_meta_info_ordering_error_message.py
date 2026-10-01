"""Repro/regression test for a hare/models/meta_info.py ordering-error finding."""

import pytest

from hare import fields
from hare.exceptions import ConfigurationError
from hare.models import Model


def test_ordering_error_message_normalizes_nested_relation_paths():
    """The ordering property's error message built `unknown_fields` from a raw (non-partitioned)
    set difference against self.fields, unlike finalise_fields()'s own validation check just
    above it (which correctly checks only the first "__"-segment) - a genuinely valid
    nested-relation ordering path (e.g. "related__name") got blamed in the error message
    alongside whatever field actually failed, even though it wasn't the problem."""

    class Related(Model):
        name = fields.CharField(max_length=10)

        class Meta:
            abstract = True

    class Main(Model):
        related = fields.ForeignKeyField("models.Related")

        class Meta:
            abstract = True
            ordering = ["related__name", "totally_bogus_field"]

    with pytest.raises(ConfigurationError) as exc_info:
        _ = Main._meta.ordering
    message = str(exc_info.value)
    assert "totally_bogus_field" in message
    assert "related__name" not in message
