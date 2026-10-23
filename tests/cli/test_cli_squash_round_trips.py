"""squashmigrations through the CLI against a real database: the squashed migration applied from
zero leaves what the migrations it replaces left - makemigrations --check and drift find nothing -
and rolls back."""

from __future__ import annotations

import textwrap

import pytest

from tests.utils.cli_project import CliProject
from tests.utils.cli_project_factory import project_factory  # noqa: F401 - the fixture


async def squash_and_apply_from_zero(project: CliProject, *squashed_range: str) -> str:
    """Squashes the app's migrations (all of them without a range), applies the result to the
    empty database, checks it and rolls it back.

    Returns:
        The squashed migration's source.
    """
    await project.cli_ok("squashmigrations", "app", *(squashed_range or (project.migration_files()[-1],)))
    squashed_name = next(name for name in project.migration_files() if "squashed" in name)
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    await project.cli_ok("migrate", "app", "zero")
    await project.cli_ok("migrate")
    await project.assert_in_sync()
    return (project.root / "cli_rt_app" / "migrations" / f"{squashed_name}.py").read_text(encoding="utf-8")


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
    squashed_source = await squash_and_apply_from_zero(project)
    assert squashed_source.count("ops.") == 1
    assert "ops.CreateModel" in squashed_source


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
    squashed_source = await squash_and_apply_from_zero(project, names[1], names[2])
    assert "ops.AddField" in squashed_source
    assert "db_index=True" in squashed_source
    assert "ops.AddIndex" not in squashed_source


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
    squashed_source = await squash_and_apply_from_zero(project)
    assert 'name="Branch"' in squashed_source
    assert '"app.Branch"' in squashed_source
    assert "Node" not in squashed_source.split("operations = [")[1]
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
    squashed_source = await squash_and_apply_from_zero(project)
    assert squashed_source.count("ops.") == 1
    # The relation's uniqueness is its own - never written as a constraint.
    assert squashed_source.count("UniqueConstraint(") == 0


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
    squashed_source = await squash_and_apply_from_zero(project)
    assert "ops.RenameField(" in squashed_source
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
    squashed_source = await squash_and_apply_from_zero(project)
    assert squashed_source.count("ops.") == 1
    assert "Index(" not in squashed_source


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
