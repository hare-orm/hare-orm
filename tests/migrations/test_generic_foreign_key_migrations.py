"""GenericForeignKeyField in migrations: its branches are plain foreign keys and its CHECK a plain
constraint, so makemigrations writes them, a new target adds a branch, a renamed branch key renames
its column, a removed one drops it - always with the CHECK replaced - and the targets of a
``swappable`` setting holding a dict become branches. Two apps with a model of the same name are
two branches."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from hare.ddl.conditions.exclusive_arc_condition import ExclusiveArcCondition
from hare.ddl.constraints import CheckConstraint
from hare.exceptions import ConfigurationError
from hare.migrations.autodetection.migration_autodetector import MigrationAutodetector
from hare.migrations.operations import AddConstraint, AddField, CreateModel, RemoveConstraint, RemoveField, RenameField
from hare.migrations.writer import MigrationWriter
from tests.migrations.test_swappable_models import (
    CONNECTION,
    build_config,
    get_connection_config,
    get_table_names,
    migrate,
    open_context,
)

BLOG_SOURCE = """
from hare import fields
from hare.models import Model


class Post(Model):
    id = fields.IntField(primary_key=True)
    title = fields.CharField(max_length=50, default="")

    class Meta:
        table = "gfk_blog_post"


class Photo(Model):
    id = fields.IntField(primary_key=True)
    url = fields.CharField(max_length=50, default="")

    class Meta:
        table = "gfk_blog_photo"
"""

FORUM_SOURCE = """
from hare import fields
from hare.models import Model


class Post(Model):
    id = fields.IntField(primary_key=True)
    subject = fields.CharField(max_length=50, default="")

    class Meta:
        table = "gfk_forum_post"
"""

NOTES_SOURCE = """
from hare import fields
from hare.models import Model, swappable


class Note(Model):
    id = fields.IntField(primary_key=True)
    target = fields.GenericForeignKeyField(TARGETS, related_name="notes")

    class Meta:
        table = "gfk_note"


class Tag(Model):
    id = fields.IntField(primary_key=True)
    tagged = fields.GenericForeignKeyField(swappable("TAG_TARGETS"), related_name="tags", null=True)

    class Meta:
        table = "gfk_tag"
"""

FIRST_TARGETS = '{"blog_post": "blog.Post", "forum_post": "forum.Post"}'
#: blog_post renamed to post, a photo branch added.
SECOND_TARGETS = '{"post": "blog.Post", "forum_post": "forum.Post", "photo": "blog.Photo"}'
#: The photo branch removed again.
THIRD_TARGETS = '{"post": "blog.Post", "forum_post": "forum.Post"}'
TAG_TARGETS = {"tagged_blog_post": "blog.Post", "tagged_photo": "blog.Photo"}


class GenericForeignKeyProject:
    """The packages of the three apps written to disk - fresh package names for every version, since
    model classes are process-wide, with the migrations written so far copied in."""

    def __init__(self, root: Path) -> None:
        self.root = root
        #: App label to the migration files written for it so far.
        self.migration_files: dict[str, list[Path]] = {"blog": [], "forum": [], "notes": []}
        #: App label to its package of the latest version.
        self.packages: dict[str, str] = {}

    def write_version(self, targets: str) -> dict[str, dict[str, Any]]:
        """Writes every app anew - the notes app with ``targets``.

        Returns:
            The apps config.
        """
        suffix = uuid.uuid4().hex[:10]
        sources = {
            "blog": BLOG_SOURCE,
            "forum": FORUM_SOURCE,
            "notes": NOTES_SOURCE.replace("TARGETS", targets, 1),
        }
        for app_label, source in sources.items():
            package = f"gfk_{app_label}_{suffix}"
            package_dir = self.root / package
            (package_dir / "migrations").mkdir(parents=True)
            (package_dir / "__init__.py").write_text("", encoding="utf-8")
            (package_dir / "models.py").write_text(source, encoding="utf-8")
            (package_dir / "migrations" / "__init__.py").write_text("", encoding="utf-8")
            for migration_file in self.migration_files[app_label]:
                (package_dir / "migrations" / migration_file.name).write_bytes(migration_file.read_bytes())
            self.packages[app_label] = package
        return {
            app_label: {
                "models": [f"{package}.models"],
                "default_connection": CONNECTION,
                "migrations": f"{package}.migrations",
            }
            for app_label, package in self.packages.items()
        }

    def keep_written(self, written: list[Path]) -> None:
        """Keeps the migration files written for the next versions."""
        for path in written:
            for app_label, package in self.packages.items():
                if package in str(path):
                    self.migration_files[app_label].append(path)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> GenericForeignKeyProject:
    monkeypatch.syspath_prepend(str(tmp_path))
    return GenericForeignKeyProject(tmp_path)


async def make_migrations(context: Any, apps_config: dict[str, dict[str, Any]]) -> tuple[list[Path], list[Any]]:
    """Writes the migrations of the changes - the files, and the operations of the notes app."""
    writers = await MigrationAutodetector(context.apps, apps_config).changes()
    written = [writer.write() for writer in writers]
    MigrationWriter.format_files(written)
    notes_operations = [
        operation for writer in writers if writer.app_label == "notes" for operation in writer.operations
    ]
    return written, notes_operations


@pytest.mark.asyncio
async def test_branches_follow_the_targets_through_migrations(project: GenericForeignKeyProject, tmp_path: Path):
    connection_config = get_connection_config(tmp_path)
    apps_config = project.write_version(FIRST_TARGETS)
    config = build_config(connection_config, apps_config, {"TAG_TARGETS": TAG_TARGETS})
    async with open_context(config, create_db=True) as context:
        written, operations = await make_migrations(context, apps_config)
        [create_note] = [
            operation for operation in operations if isinstance(operation, CreateModel) and operation.name == "Note"
        ]
        field_names = [name for name, _field in create_note.fields]
        assert {"blog_post", "forum_post"} <= set(field_names) and "target" not in field_names
        [arc] = [
            constraint
            for constraint in create_note.options["constraints"]
            if isinstance(constraint, CheckConstraint) and isinstance(constraint.check, ExclusiveArcCondition)
        ]
        assert arc.check == ExclusiveArcCondition(("blog_post", "forum_post"))
        project.keep_written(written)
        await migrate(context, apps_config)
        assert {"gfk_note", "gfk_tag"} <= await get_table_names(context)

        blog_post_model = context.apps.get_model("blog", "Post")
        forum_post_model = context.apps.get_model("forum", "Post")
        photo_model = context.apps.get_model("blog", "Photo")
        note_model = context.apps.get_model("notes", "Note")
        tag_model = context.apps.get_model("notes", "Tag")
        blog_post = await blog_post_model.objects.create(id=1, title="b")
        forum_post = await forum_post_model.objects.create(id=1, subject="f")
        photo = await photo_model.objects.create(id=1, url="u")
        await note_model.objects.create(id=1, target=blog_post)
        await note_model.objects.create(id=2, target=forum_post)
        assert await note_model.objects.filter(target__type="forum_post").values_list("id", flat=True) == [2]
        assert await note_model.objects.filter(target=blog_post).values_list("id", flat=True) == [1]
        await tag_model.objects.create(id=1, tagged=photo)
        assert await tag_model.objects.filter(tagged__type="tagged_photo").count() == 1
        assert [note.id for note in await forum_post.notes] == [2]

    # blog_post renamed to post, photo added.
    apps_config = project.write_version(SECOND_TARGETS)
    config = build_config(connection_config, apps_config, {"TAG_TARGETS": TAG_TARGETS})
    async with open_context(config) as context:
        written, operations = await make_migrations(context, apps_config)
        assert any(
            isinstance(operation, RenameField) and (operation.old_name, operation.new_name) == ("blog_post", "post")
            for operation in operations
        )
        assert any(isinstance(operation, AddField) and operation.name == "photo" for operation in operations)
        assert any(isinstance(operation, RemoveConstraint) for operation in operations)
        assert any(isinstance(operation, AddConstraint) for operation in operations)
        project.keep_written(written)
        await migrate(context, apps_config)
        note_model = context.apps.get_model("notes", "Note")
        photo_model = context.apps.get_model("blog", "Photo")
        assert await note_model.objects.order_by("id").values_list("id", "target__type") == [
            (1, "post"),
            (2, "forum_post"),
        ]
        await note_model.objects.create(id=3, target=await photo_model.objects.get(id=1))
        assert await note_model.objects.filter(target__type="photo").values_list("id", flat=True) == [3]
        assert await MigrationAutodetector(context.apps, apps_config).changes() == []

    # The photo branch removed.
    apps_config = project.write_version(THIRD_TARGETS)
    config = build_config(connection_config, apps_config, {"TAG_TARGETS": TAG_TARGETS})
    async with open_context(config, drop_db=True) as context:
        note_model = context.apps.get_model("notes", "Note")
        await note_model.objects.filter(id=3).delete()
        _written, operations = await make_migrations(context, apps_config)
        assert any(isinstance(operation, RemoveField) and operation.name == "photo" for operation in operations)
        await migrate(context, apps_config)
        assert await note_model.objects.order_by("id").values_list("id", "target__type") == [
            (1, "post"),
            (2, "forum_post"),
        ]


@pytest.mark.asyncio
async def test_a_swappable_setting_of_targets_must_be_configured(project: GenericForeignKeyProject, tmp_path: Path):
    apps_config = project.write_version(FIRST_TARGETS)
    config = build_config(get_connection_config(tmp_path), apps_config)
    with pytest.raises(ConfigurationError, match='swappable setting "TAG_TARGETS" isn\'t configured'):
        async with open_context(config, connect=False):
            pass
    config = build_config(get_connection_config(tmp_path), apps_config, {"TAG_TARGETS": {"x": "blog.Nobody"}})
    with pytest.raises(ConfigurationError, match='app "blog" has no such model'):
        async with open_context(config, connect=False):
            pass
