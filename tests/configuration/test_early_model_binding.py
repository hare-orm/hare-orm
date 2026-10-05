"""Hare.bind_models() binds a whole configuration's models without a connection: every
filter key and ordering name is described as after a full Hare.init(), no Hare context is left
current, and a full init afterwards works as usual.

Each check runs in a fresh interpreter - in the test process the models were fully initialised
long before, which would hide a description that early binding alone can't build. One interpreter
describes every model of one configuration.
"""

import json
import subprocess
import sys
import textwrap
from typing import Any

import pytest

pytestmark = pytest.mark.database_independent

DESCRIBE_SCRIPT = textwrap.dedent(
    """
    import asyncio, dataclasses, enum, functools, json, sys
    from hare import Hare
    from hare.core.config import HareConfig
    from hare.core.hare_context import HareContext
    from hare.fields import Field

    mode, db_url, module, cases = json.loads(sys.argv[1])

    def describe(value):
        if isinstance(value, Field):
            owner = value.model.__name__ if value.model is not None else type(value).__name__
            return f"{owner}.{value.model_field_name}"
        if isinstance(value, type):
            return value.__name__
        if isinstance(value, enum.Enum):
            return value.value
        if dataclasses.is_dataclass(value):
            return {item.name: describe(getattr(value, item.name)) for item in dataclasses.fields(value)}
        if isinstance(value, dict):
            return {str(key): describe(item) for key, item in value.items()}
        if isinstance(value, (tuple, list, frozenset, set)):
            return [describe(item) for item in value]
        if isinstance(value, functools.partial):
            return [describe(value.func), describe(list(value.args)), describe(value.keywords)]
        if isinstance(value, staticmethod):
            return describe(value.__func__)
        if callable(value):
            return getattr(value, "__qualname__", type(value).__qualname__)
        return value

    def describe_model(model_name, keys, orderings):
        model = HareContext.get_current().get_model("models", model_name) if mode == "full" else None
        if model is None:
            module_object = __import__(module, fromlist=[model_name])
            model = getattr(module_object, model_name)
        return {
            "lookups": {key: describe(model._meta.get_lookup_info(key)) for key in keys},
            "queryset_lookups": {key: describe(model.objects.all().get_lookup_info(key)) for key in keys},
            "orderings": {name: describe(model._meta.get_ordering_info(name)) for name in orderings},
        }

    def describe_all():
        return {model_name: describe_model(model_name, keys, orderings) for model_name, keys, orderings in cases}

    if mode == "early_app":
        Hare.bind_models(HareConfig.from_db_url(db_url, {"models": [module]}))
        result = describe_all()
    elif mode == "early":
        Hare.bind_models(HareConfig.from_db_url(db_url, {"models": [module]}))
        result = describe_all()
        result["current_context"] = HareContext.get_current() is not None

        async def full_init_afterwards():
            async with HareContext() as context:
                await context.init(HareConfig.from_db_url(db_url, {"models": [module]}))
                return describe_all()

        result["after_full_init"] = asyncio.run(full_init_afterwards())

        async def global_init_afterwards():
            context = await Hare.init(
                HareConfig.from_db_url(db_url, {"models": [module]}), _enable_global_fallback=True
            )
            try:
                # Another task - it sees the context through the global fallback only.
                return await asyncio.create_task(asyncio.to_thread(describe_all))
            finally:
                HareContext.clear_global_if(context)
                await context.close_connections()

        result["after_global_init"] = asyncio.run(global_init_afterwards())
    else:

        async def full_init():
            async with HareContext() as context:
                await context.init(HareConfig.from_db_url(db_url, {"models": [module]}))
                return describe_all()

        result = asyncio.run(full_init())
    print(json.dumps(result, sort_keys=True))
    """
)

SQLITE_URL = "sqlite+aiosqlite://:memory:"
# Hare.init() connects lazily - the URL needs no running server.
POSTGRESQL_URL = "postgresql://user:password@127.0.0.1:1/unused"

CASES = [
    pytest.param(
        SQLITE_URL,
        "tests.shared_models.testmodels",
        [
            # Fields, relations and date parts.
            (
                "Event",
                [
                    "name",
                    "name__icontains",
                    "event_id__in",
                    "tournament",
                    "tournament__in",
                    "tournament_id",
                    "tournament__name",
                    "tournament__name__startswith",
                    "tournament__events__name",
                    "participants",
                    "participants__in",
                    "participants__name",
                    "modified__year__gte",
                    "modified__date",
                    "reporter__isnull",
                ],
                ["-name", "tournament__name", "pk", "tournament"],
            ),
            # JSON paths.
            (
                "JSONFields",
                ["data", "data__contains", "data__owner__name", "data__rank__gt", "data__has_key"],
                ["data__owner__name", "-id"],
            ),
            # A relation to a composite key.
            (
                "DocumentRevisionNote",
                ["document", "document__in", "document__pk", "document__pk__in", "document_id", "document__isnull"],
                ["document", "-pk"],
            ),
            # A composite key.
            (
                "VersionedDocument",
                ["pk", "pk__in", "revision_notes__isnull", "revision_notes__note__icontains"],
                ["pk", "-pk"],
            ),
        ],
        id="testmodels",
    ),
    pytest.param(
        SQLITE_URL,
        "tests.contrib.request_query.models",
        [
            (
                "Book",
                ["status", "status__in", "author", "author__in", "author__name", "published_at__year__gte", "tags"],
                ["-author__name", "published_at"],
            ),
            # Registered lookups.
            ("Author", ["score__within", "score__within_on_postgresql"], ["score"]),
        ],
        id="request-query-models",
    ),
    pytest.param(
        POSTGRESQL_URL,
        "tests.dialects.postgresql.models_container_paths",
        [
            (
                "Crate",
                [
                    "numbers",
                    "numbers__contains",
                    "numbers__0",
                    "grid__0__1",
                    "span",
                    "span__contains",
                    "days__overlap",
                ],
                ["numbers__0", "-span"],
            ),
        ],
        id="arrays-and-ranges",
    ),
]


#: The descriptions after a full init, by the arguments of their run - the same for both tests of a case.
FULL_DESCRIPTIONS: dict[str, dict] = {}


def start_describe(mode: str, db_url: str, module: str, cases: list[tuple[str, list[str], list[str]]]) -> Any:
    arguments = json.dumps([mode, db_url, module, cases])
    return subprocess.Popen(
        [sys.executable, "-c", DESCRIBE_SCRIPT, arguments], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def read_describe(process: Any) -> dict:
    stdout, stderr = process.communicate()
    assert process.returncode == 0, stderr
    return json.loads(stdout.strip().splitlines()[-1])


def run_describe_beside_full(
    mode: str, db_url: str, module: str, cases: list[tuple[str, list[str], list[str]]]
) -> tuple[dict, dict]:
    """The descriptions of a mode and of a full init - the two processes run at once, the full one
    only once per case."""
    full_key = json.dumps([db_url, module, cases])
    process = start_describe(mode, db_url, module, cases)
    full_process = None if full_key in FULL_DESCRIPTIONS else start_describe("full", db_url, module, cases)
    described = read_describe(process)
    if full_process is not None:
        FULL_DESCRIPTIONS[full_key] = read_describe(full_process)
    return described, FULL_DESCRIPTIONS[full_key]


@pytest.mark.parametrize(("db_url", "module", "cases"), CASES)
def test_every_key_is_described_after_early_binding_as_after_a_full_init(db_url, module, cases):
    early, full = run_describe_beside_full("early", db_url, module, cases)
    assert early["current_context"] is False
    after_full_init = early.pop("after_full_init")
    after_global_init = early.pop("after_global_init")
    early.pop("current_context")
    assert early == full
    assert after_full_init == full
    assert after_global_init == full


@pytest.mark.parametrize(("db_url", "module", "cases"), CASES)
def test_every_key_is_described_after_binding_one_app(db_url, module, cases):
    early, full = run_describe_beside_full("early_app", db_url, module, cases)
    assert early == full
