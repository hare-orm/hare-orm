"""squashmigrations through the CLI against a real database: the squashed migration applied from
zero leaves what the migrations it replaces left - makemigrations --check and drift find nothing -
and rolls back."""

from __future__ import annotations

import textwrap

import pytest

from hare.ddl.constraints import UniqueConstraint
from hare.fields.relations.fields.one_to_one_field_instance import OneToOneFieldInstance
from hare.migrations import Migration
from hare.migrations.operations import AddField, CreateModel, RenameField
from tests.utils.cli_project import CliProject
from tests.utils.cli_project_factory import project_factory  # noqa: F401 - the fixture


async def squash_and_apply_from_zero(project: CliProject, *squashed_range: str) -> Migration:
    """Squashes the app's migrations (all of them without a range), applies the result to the
    empty database, checks it and rolls it back.

    Returns:
        The squashed migration's operations and metadata.
    """
    await project.cli_ok("squashmigrations", "app", *(squashed_range or (project.migration_files()[-1],)))
    squashed_name = next(name for name in project.migration_files() if "squashed" in name)
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "zero")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    return project.get_migration(squashed_name)


@pytest.mark.asyncio
async def test_fields_added_with_an_index_of_their_own_fold_into_the_created_model(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    versions = [
        "",
        """
        code = fields.IntField(null=True, db_index=True)
        parent = fields.ForeignKeyField("app.Node", related_name="children", null=True)
        """,
        """
        code = fields.IntField(null=True, db_index=True)
        parent = fields.ForeignKeyField("app.Node", related_name="children", null=True)
        rank = fields.IntField(null=True, db_index=True)
        """,
    ]
    for added_fields in versions:
        project.write_models(
            "class Node(Model):\n    id = fields.IntField(primary_key=True)\n"
            + textwrap.indent(textwrap.dedent(added_fields), "    ")
        )
        await project.cli_ok("makemigrations")
    migration = await squash_and_apply_from_zero(project)
    [operation] = migration.operations
    assert isinstance(operation, CreateModel)
    assert operation.name == "Node"
    columns = dict(operation.fields)
    assert set(columns) == {"id", "code", "parent", "rank"}
    assert columns["code"].index and columns["rank"].index
    assert columns["parent"].model_name == "app.Node"


@pytest.mark.asyncio
async def test_a_field_added_and_then_given_an_index_keeps_it_squashed_without_its_model(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    for field in (
        "",
        "    code = fields.IntField(null=True)\n",
        "    code = fields.IntField(null=True, db_index=True)\n",
    ):
        project.write_models("class Node(Model):\n    id = fields.IntField(primary_key=True)\n" + field)
        await project.cli_ok("makemigrations")
    names = project.migration_files()
    migration = await squash_and_apply_from_zero(project, names[1], names[2])
    [operation] = migration.operations
    assert isinstance(operation, AddField)
    assert (operation.model_name, operation.name) == ("Node", "code")
    assert operation.field.index
    assert migration.replaces == [("app", names[1]), ("app", names[2])]


@pytest.mark.asyncio
async def test_an_indexed_field_added_and_removed_leaves_a_squashed_migration_that_applies(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    for field in ("", "    code = fields.IntField(null=True, db_index=True)\n", ""):
        project.write_models("class Node(Model):\n    id = fields.IntField(primary_key=True)\n" + field)
        await project.cli_ok("makemigrations")
    await squash_and_apply_from_zero(project)


@pytest.mark.asyncio
async def test_a_renamed_model_is_created_under_its_new_name_and_table(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    template = """
    class {name}(Model):
        id = fields.IntField(primary_key=True)
        parent = fields.ForeignKeyField("app.{name}", related_name="children", null=True)
    """
    project.write_models(textwrap.dedent(template.format(name="Node")))
    await project.cli_ok("makemigrations")
    project.write_models(textwrap.dedent(template.format(name="Branch")))
    project.write_migration(
        "0002_rename_node",
        """
        from hare import migrations
        from hare.migrations import operations as ops

        class Migration(migrations.Migration):
            dependencies = [("app", "0001_initial")]
            operations = [ops.RenameModel(old_name="Node", new_name="Branch")]
        """,
    )
    migration = await squash_and_apply_from_zero(project)
    [operation] = migration.operations
    assert isinstance(operation, CreateModel)
    assert operation.name == "Branch"
    assert dict(operation.fields)["parent"].model_name == "app.Branch"
    await project.cli_ok("migrate")
    await project.query("INSERT INTO branch (id) VALUES (1)")
    await project.query("INSERT INTO branch (id, parent_id) VALUES (2, 1)")


@pytest.mark.asyncio
async def test_a_one_to_one_relation_to_a_composite_key_added_later_is_unique_once(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    template = """
    class Pair(Model):
        a = fields.IntField()
        b = fields.IntField()
        pk = fields.CompositePrimaryKey("a", "b")
    {relation}
    """
    relation = '    twin = fields.OneToOneField("app.Pair", related_name="twin_of", null=True)'
    for step in ("", relation):
        project.write_models(textwrap.dedent(template).format(relation=step))
        await project.cli_ok("makemigrations")
    migration = await squash_and_apply_from_zero(project)
    [operation] = migration.operations
    assert isinstance(operation, CreateModel)
    assert operation.name == "Pair"
    relation = dict(operation.fields)["twin"]
    assert isinstance(relation, OneToOneFieldInstance)
    assert relation.unique and relation.model_name == "app.Pair"
    assert not any(isinstance(constraint, UniqueConstraint) for constraint in operation.options.get("constraints", ()))


@pytest.mark.asyncio
async def test_a_rename_of_a_field_a_created_models_raw_sql_reads_stays_apart_squashed(project_factory) -> None:  # noqa: F811
    """The generated column's raw SQL names the column - the database renames the column under it, a
    creation under the new name would make the table without the column the SQL reads."""
    project = await project_factory()
    template = """
    class Item(Model):
        id = fields.IntField(primary_key=True)
        {name} = fields.IntField(db_default=5, unique=True)
        double = fields.GeneratedField(expression=RawSQLTerm("{name} * 2"), output_field=fields.IntField())
        base = fields.ForeignKeyField("app.Item", related_name="derived", null=True, to_field="{name}")
    """
    project.write_models(textwrap.dedent(template).format(name="price"))
    await project.cli_ok("makemigrations")
    project.write_models(textwrap.dedent(template).format(name="amount"))
    await project.cli_ok("makemigrations", "--rename", "app.Item.price=amount")
    migration = await squash_and_apply_from_zero(project)
    assert any(
        isinstance(operation, RenameField)
        and (operation.model_name, operation.old_name, operation.new_name) == ("Item", "price", "amount")
        for operation in migration.operations
    )
    await project.cli_ok("migrate")
    await project.query("INSERT INTO item (id, amount) VALUES (1, 7)")
    await project.query("INSERT INTO item (id, amount, base_id) VALUES (2, 8, 7)")
    assert await project.query("SELECT double FROM item ORDER BY id") == [{"double": 14}, {"double": 16}]


@pytest.mark.asyncio
async def test_a_field_created_with_an_index_and_left_without_it_is_created_without_it(project_factory) -> None:  # noqa: F811
    project = await project_factory()
    node = "class Node(Model):\n    id = fields.IntField(primary_key=True)\n"
    for field_options in ("null=True, db_index=True", "null=True"):
        project.write_models(f"{node}    code = fields.IntField({field_options})\n")
        await project.cli_ok("makemigrations")
    migration = await squash_and_apply_from_zero(project)
    [operation] = migration.operations
    assert isinstance(operation, CreateModel)
    assert operation.name == "Node"
    assert not dict(operation.fields)["code"].index
    assert not operation.options.get("indexes")


@pytest.mark.asyncio
async def test_a_range_another_apps_migration_sits_inside_is_refused_and_squashed_in_two(project_factory) -> None:  # noqa: F811
    """a.0002 depends on b.0001, which depends on a.0001: one migration replacing both a's wrote
    itself a dependency on b.0001 - a cycle no later command could load the migrations through."""
    project = await project_factory("a", "b")
    owner_source = "class Owner(Model):\n    id = fields.IntField(primary_key=True)\n"
    pet_source = (
        "class Pet(Model):\n    id = fields.IntField(primary_key=True)\n"
        '    owner = fields.ForeignKeyField("a.Owner", related_name="pets")\n'
    )
    project.write_models(owner_source, "a")
    await project.cli_ok("makemigrations")
    project.write_models(pet_source, "b")
    await project.cli_ok("makemigrations")
    project.write_models(
        owner_source + '    favorite = fields.ForeignKeyField("b.Pet", related_name="fans", null=True)\n', "a"
    )
    await project.cli_ok("makemigrations")
    project.write_models(
        owner_source
        + '    favorite = fields.ForeignKeyField("b.Pet", related_name="fans", null=True)\n'
        + "    name = fields.CharField(max_length=20, null=True)\n",
        "a",
    )
    await project.cli_ok("makemigrations")
    first_name, second_name, third_name = project.migration_files("a")

    result = await project.cli("squashmigrations", "a", third_name)

    assert result.exit_code not in (0, None)
    assert f"a.{second_name} depends on b.0001_initial, which depends on a.{first_name}" in result.output
    assert project.migration_files("a") == [first_name, second_name, third_name]
    await project.cli_ok("squashmigrations", "a", second_name, third_name)
    await project.cli_ok("migrate")
    await project.assert_in_sync()
