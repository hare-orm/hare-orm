"""Implicit value selections cannot compare randomized ciphertext in set operations."""

import os

import pytest

from hare import fields
from hare.contrib.test import hare_test_context
from hare.ddl.raw_sql_term import RawSQLTerm
from hare.exceptions import FieldError
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.models import Model
from hare.query.expressions import F, Value

REQUIRES = ("encryption",)


class SecretLog(Model):
    secret = fields.EncryptedTextField()

    class Meta:
        primary_key = None


class GeneratedSecret(Model):
    id = fields.IntField(primary_key=True)
    secret = fields.EncryptedTextField()
    plain = fields.TextField(default="")
    copied = fields.GeneratedField(expression=RawSQLTerm("secret"), output_field=fields.EncryptedTextField())


@pytest.mark.asyncio
@pytest.mark.parametrize("values_list", [False, True])
async def test_implicit_selections_refuse_comparing_encrypted_values(values_list):
    FieldEncryption.configure("test-set-operation-secret")
    try:
        async with hare_test_context([__name__], db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")):
            await SecretLog.objects.create(secret="same")
            await SecretLog.objects.create(secret="same")
            rows = SecretLog.objects.values_list() if values_list else SecretLog.objects.values()
            expected = ("same",) if values_list else {"secret": "same"}
            assert await rows == [expected, expected]
            for method in ("union", "intersection", "difference"):
                with pytest.raises(FieldError, match="can't use it"):
                    await getattr(rows, method)(rows)
            assert await rows.union(rows, all=True) == [expected] * 4
            copied = rows.annotate(copied_secret=F("secret"))
            with pytest.raises(FieldError, match="can't use it"):
                await copied.union(copied)
            replaced = SecretLog.objects.annotate(public=Value("safe"))
            replaced = replaced.values_list("public") if values_list else replaced.values("public")
            assert await replaced.union(replaced) == [("safe",) if values_list else {"public": "safe"}]
    finally:
        FieldEncryption.reset()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["ordering", "union", "plain_update", "encrypted_update"])
async def test_generated_encrypted_values_keep_the_output_fields_comparison_restrictions(operation):
    FieldEncryption.configure("test-generated-secret")
    try:
        async with hare_test_context([__name__], db_url=os.getenv("HARE_TEST_DB", "sqlite+aiosqlite://:memory:")):
            row = await GeneratedSecret.objects.create(secret="same")
            assert (await GeneratedSecret.objects.get(pk=row.pk)).copied == "same"
            if operation == "ordering":
                with pytest.raises(FieldError, match="ORDER BY can't use it"):
                    await GeneratedSecret.objects.order_by("copied")
            elif operation == "union":
                values = GeneratedSecret.objects.values("copied")
                with pytest.raises(FieldError, match="UNION can't use it"):
                    await values.union(values)
            elif operation == "plain_update":
                with pytest.raises(FieldError, match="encrypted"):
                    await GeneratedSecret.objects.filter(pk=row.pk).update(plain=F("copied"))
            else:
                await GeneratedSecret.objects.filter(pk=row.pk).update(secret=F("copied"))
                assert (await GeneratedSecret.objects.get(pk=row.pk)).secret == "same"
    finally:
        FieldEncryption.reset()
