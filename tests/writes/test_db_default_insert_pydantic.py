"""Pydantic schemas of models with database defaults: a field the database fills in is optional in the input
schema, and an unfetched one fails validation until refreshed."""

import pydantic
import pytest

from hare.contrib.pydantic import pydantic_model_creator
from tests.shared_models.testmodels import NoFetchDefaultModel
from tests.shared_models.testmodels_postgres import DefaultModel

REQUIRES = ("pydantic",)


class TestPydanticDbDefault:
    @pytest.mark.asyncio
    async def test_pydantic_no_fetch_requires_refresh(self, db):
        PydanticNoFetch = pydantic_model_creator(NoFetchDefaultModel)

        instance = await NoFetchDefaultModel.objects.create()
        conn = instance._meta.connection

        if not conn.features.supports_returning:
            # Without RETURNING, unfetched db_default fields are DatabaseDefault sentinels
            # and pydantic validation will fail
            with pytest.raises(pydantic.ValidationError):
                await PydanticNoFetch.from_hare_orm(instance)

        # After refreshing from db, pydantic model succeeds
        refreshed = await NoFetchDefaultModel.objects.get(pk=instance.pk)
        pydantic_instance = await PydanticNoFetch.from_hare_orm(refreshed)
        assert pydantic_instance.int_val == 1
        assert pydantic_instance.char_val == "test"

    @pytest.mark.postgresql_models
    @pytest.mark.database_independent
    def test_db_default_only_field_not_required_in_schema(self):
        """A field with only a db_default (no plain ``default``, not null, not generated) is
        NOT required by ``Field.required`` - the ORM lets .create() omit it and has the DB fill
        it in. The generated pydantic schema must agree, or a create-payload validated against
        it would wrongly reject an omitted db_default field."""
        PydanticDefault = pydantic_model_creator(DefaultModel, name="DefaultModelSchemaTest")
        schema = PydanticDefault.model_json_schema()

        for field_name in (
            "int_default",
            "float_default",
            "decimal_default",
            "bool_default",
            "char_default",
            "date_default",
            "datetime_default",
        ):
            assert field_name not in schema.get("required", []), (
                f"{field_name} has only a db_default and must not be required"
            )
            assert PydanticDefault.model_fields[field_name].default is None

        # Every db_default-only field must validate cleanly when omitted - only the
        # auto-generated `id` primary key is unrelated to this fix and still needs a value here.
        PydanticDefault.model_validate({"id": 1})
