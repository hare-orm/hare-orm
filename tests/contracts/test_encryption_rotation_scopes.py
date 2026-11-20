"""Rotation spans all tenants of its model while preserving the caller's other scopes."""

import datetime

import pytest

from hare import Model, fields
from hare.contrib.test import hare_test_context, requires_features
from hare.exceptions import QueryError
from hare.fields.encrypted.field_encryption import FieldEncryption
from hare.models.tenancy.tenancy import Tenancy
from hare.models.tenancy.tenant_schemas import TenantSchemas
from hare.query.statements.write.bulk.bulk_update_query import BulkUpdateQuery
from tests.contracts.constants import CONTRACT_APP, CONTRACT_CONNECTION
from tests.utils.database_under_test import DatabaseUnderTest

pytestmark = [pytest.mark.asyncio, pytest.mark.requires("encryption")]


@pytest.mark.parametrize("composite", [False, True], ids=["scalar", "composite"])
@pytest.mark.parametrize("mapped_scope", [False, True], ids=["global-scope", "model-scopes"])
@pytest.mark.parametrize("batch_size", [1, 2])
async def test_rotation_expands_only_its_model_scope_and_restores_it(
    contract_database, monkeypatch, composite, mapped_scope, batch_size
):
    class Note(Model):
        number = fields.IntField(primary_key=not composite, generated=False)
        region = fields.IntField(default=7)
        tenant = fields.IntField()
        deleted_at = fields.DatetimeField(null=True)
        body = fields.EncryptedTextField()
        if composite:
            pk = fields.CompositePrimaryKey("region", "number")

        class Meta:
            tenant_field = "tenant"
            soft_delete_field = "deleted_at"

    class Other(Model):
        number = fields.IntField(primary_key=True, generated=False)
        tenant = fields.IntField()

        class Meta:
            tenant_field = "tenant"

    contract_database.register_live_models([Note, Other], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    note_binding = contract_database.apps.get_binding(Note)
    other_binding = contract_database.apps.get_binding(Other)
    FieldEncryption.configure("rotation-fixture-old")
    with Tenancy.scope(Tenancy.ALL):
        for number, tenant in ((1, 1), (2, 2), (3, 1)):
            await Note.objects.create(
                number=number,
                tenant=tenant,
                body=f"note-{number}",
                deleted_at=datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC) if number == 3 else None,
            )
    FieldEncryption.configure("rotation-fixture-new", previous_keys=["rotation-fixture-old"])
    seen_scopes = []
    execute_bulk = BulkUpdateQuery._execute_bound

    async def record_scopes(query):
        seen_scopes.append((Tenancy.get_scope(note_binding), Tenancy.get_scope(other_binding)))
        return await execute_bulk(query)

    expected_other_scope = 9 if mapped_scope else 1
    with Tenancy.scope({Note: 1, Other: 9} if mapped_scope else 1):
        original_scope = Tenancy.current.get()
        with monkeypatch.context() as patch:
            patch.setattr(BulkUpdateQuery, "_execute_bound", record_scopes)
            assert await FieldEncryption.reencrypt(Note, batch_size=batch_size) == 3
        assert seen_scopes and all(
            note is Tenancy.ALL and other == expected_other_scope for note, other in seen_scopes
        )
        assert Tenancy.current.get() is original_scope
        FieldEncryption.configure("rotation-fixture-new")
        assert await Note.objects.all_tenants().include_deleted().order_by("number").values_list(
            "body", flat=True
        ) == ["note-1", "note-2", "note-3"]
        foreign = await Note.objects.all_tenants().get(number=2)
        foreign.body = "blocked"
        with pytest.raises(QueryError):
            await Note.objects.all_tenants().bulk_update([foreign], fields=["body"])

        async def fail_write(query):
            assert Tenancy.get_scope(note_binding) is Tenancy.ALL
            assert Tenancy.get_scope(other_binding) == expected_other_scope
            raise RuntimeError("failed rotation batch")

        with monkeypatch.context() as patch:
            patch.setattr(BulkUpdateQuery, "_execute_bound", fail_write)
            with pytest.raises(RuntimeError, match="failed rotation batch"):
                await FieldEncryption.reencrypt(Note, batch_size=batch_size)
        assert Tenancy.current.get() is original_scope


async def test_rotation_on_a_selected_schema_keeps_the_other_tenants_ciphertext():
    if DatabaseUnderTest.get_dialect().name != "postgresql":
        pytest.skip("The scenario creates two PostgreSQL tenant schemas")

    class Note(Model):
        number = fields.IntField(primary_key=True, generated=False)
        body = fields.EncryptedTextField()

        class Meta:
            table = "rotation_note"
            tenant_schema = True

    url = DatabaseUnderTest.get_url()
    url += ("&" if "?" in url else "?") + "tenant_schema_template=tenant_%7Btenant%7D"
    async with hare_test_context(
        [], db_url=url, connection_label=CONTRACT_CONNECTION, _generate_schemas=False
    ) as context:
        context.register_live_models([Note], CONTRACT_APP, CONTRACT_CONNECTION)
        connection = context.get_connection(CONTRACT_CONNECTION)
        FieldEncryption.configure("rotation-fixture-old")
        primary_clients = []
        for tenant in ("alpha", "beta"):
            await TenantSchemas.create(tenant, CONTRACT_CONNECTION)
            with Tenancy.scope(tenant):
                primary_clients.append(Note.get_connection())
                await Note.objects.create(number=1, body=tenant)
        sql = "SELECT a.body AS first, b.body AS second FROM tenant_alpha.rotation_note a, tenant_beta.rotation_note b"
        (before,) = await connection.execute_dicts(sql)
        FieldEncryption.configure("rotation-fixture-new", previous_keys=["rotation-fixture-old"])
        assert await FieldEncryption.reencrypt(Note, using=primary_clients[0], batch_size=1) == 1
        (after,) = await connection.execute_dicts(sql)
        assert after["first"] != before["first"]
        assert after["second"] == before["second"]
        assert [await Note.objects.using(client).values_list("body", flat=True) for client in primary_clients] == [
            ["alpha"],
            ["beta"],
        ]


@requires_features(dialect="postgresql")
async def test_rotation_respects_row_level_security_on_its_selected_client(contract_database):
    class Note(Model):
        number = fields.IntField(primary_key=True, generated=False)
        tenant = fields.IntField()
        body = fields.EncryptedTextField()

        class Meta:
            table = "rotation_note"
            tenant_field = "tenant"

    contract_database.register_live_models([Note], CONTRACT_APP, CONTRACT_CONNECTION)
    await contract_database.generate_schemas()
    connection = contract_database.get_connection(CONTRACT_CONNECTION)
    (database,) = await connection.execute_dicts("SELECT current_database() AS name")
    role_name = ("rotation_" + database["name"])[-62:]
    role = connection.dialect.literals.quote_identifier(role_name)
    FieldEncryption.configure("rotation-fixture-old")
    for tenant in (1, 2):
        with Tenancy.scope(tenant):
            await Note.objects.create(number=tenant, body=f"tenant {tenant}")
    sql = "SELECT number, body FROM rotation_note ORDER BY number"
    before = await connection.execute_dicts(sql)
    await connection.execute_script(
        f"CREATE ROLE {role} NOLOGIN; GRANT SELECT, UPDATE ON rotation_note TO {role}; "
        "ALTER TABLE rotation_note ENABLE ROW LEVEL SECURITY; "
        f"CREATE POLICY rotation_scope ON rotation_note TO {role} USING (tenant = 1) WITH CHECK (tenant = 1)"
    )
    try:
        FieldEncryption.configure("rotation-fixture-new", previous_keys=["rotation-fixture-old"])
        async with connection._in_transaction() as transaction:
            await transaction.execute(f"SET LOCAL ROLE {role}")
            assert await transaction.execute_dicts(sql) == before[:1]
            assert await FieldEncryption.reencrypt(Note, using=transaction, batch_size=1) == 1
        after = await connection.execute_dicts(sql)
        assert after[0]["number"] == 1 and after[0]["body"] != before[0]["body"]
        assert after[1:] == before[1:]
        assert await Note.objects.all_tenants().order_by("number").values_list("body", flat=True) == [
            "tenant 1",
            "tenant 2",
        ]
    finally:
        await connection.execute_script(
            f"DROP POLICY rotation_scope ON rotation_note; DROP OWNED BY {role}; DROP ROLE {role}"
        )
