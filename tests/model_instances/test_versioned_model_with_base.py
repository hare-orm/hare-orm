"""VersionedModel combined with a project base model declaring its own primary key `id`: VersionedModel first
gives the key id + version; the base first is refused with the order to write."""

import uuid

import pytest

from hare import fields
from hare.contrib.versioning import VersionedModel
from hare.exceptions import ConfigurationError
from hare.models import Model

pytestmark = pytest.mark.database_independent


class ProjectBase(Model):
    id = fields.UUIDField(primary_key=True, default=uuid.uuid4)

    class Meta:
        abstract = True


def test_versioned_model_first_keys_the_rows_by_id_and_version():
    class VersionedFirst(VersionedModel, ProjectBase):
        title = fields.CharField(max_length=10)

        class Meta(ProjectBase.Meta, VersionedModel.Meta):
            abstract = True

    class Article(VersionedFirst):
        pass

    assert [field.model_field_name for field in Article._meta.pk_fields] == ["id", "version"]
    assert Article._meta.fields_map["id"].version == 7


def test_the_base_first_is_refused_with_the_order_to_write():
    with pytest.raises(ConfigurationError, match=r"class Document\(VersionedModel, YourBaseModel\)"):

        class BaseFirst(ProjectBase, VersionedModel):
            title = fields.CharField(max_length=10)

            class Meta(ProjectBase.Meta, VersionedModel.Meta):
                abstract = False
