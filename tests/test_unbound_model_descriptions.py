"""A model not bound yet refuses to describe a filter key or an ordering name with a
ConfigurationError naming the fix; Model._meta.is_bound tells whether it's bound, and once
Hare.bind_models() binds it every description works - through a queryset made before
binding too.

The check runs in a fresh interpreter - in the test process the models were bound long before.
"""

import json
import subprocess
import sys
import textwrap

DESCRIBE_UNBOUND_SCRIPT = textwrap.dedent(
    """
    import json
    from hare import Hare
    from hare.core.config import HareConfig
    from hare.dialects.dialect_registry import DialectRegistry
    from hare.exceptions import ConfigurationError
    from tests.testmodels import Event, Tournament

    dialect = DialectRegistry.get_dialect("sqlite")
    keys = ["name", "tournament", "tournament__name", "participants__in", "tournament__events__name__in"]
    orderings = ["name", "-tournament", "tournament__name"]
    paths = ["name", "tournament", "tournament__name"]
    queryset = Event.objects.all()

    def get_calls():
        calls = {}
        for key in keys:
            calls[f"meta.get_lookup_info({key})"] = lambda key=key: Event._meta.get_lookup_info(key)
            calls[f"queryset.get_lookup_info({key})"] = lambda key=key: queryset.get_lookup_info(key)
        for name in orderings:
            calls[f"meta.get_ordering_info({name})"] = lambda name=name: Event._meta.get_ordering_info(name)
            calls[f"queryset.get_ordering_info({name})"] = lambda name=name: queryset.get_ordering_info(name)
        for path in paths:
            calls[f"meta.get_lookups({path})"] = lambda path=path: Event._meta.get_lookups(path, dialect)
            calls[f"queryset.get_lookups({path})"] = lambda path=path: queryset.get_lookups(path, dialect)
        return calls

    result = {"bound_before": [Event._meta.is_bound, Tournament._meta.is_bound], "before": {}}
    for name, call in get_calls().items():
        try:
            call()
        except ConfigurationError as error:
            result["before"][name] = str(error)
        except Exception as error:
            result["before"][name] = f"{type(error).__name__}: {error}"
        else:
            result["before"][name] = "described"

    Hare.bind_models(HareConfig.from_db_url("sqlite+aiosqlite://:memory:", {"models": ["tests.testmodels"]}))
    result["bound_after"] = [Event._meta.is_bound, Tournament._meta.is_bound]
    result["after"] = {name: type(call()).__name__ for name, call in get_calls().items()}
    result["tournament__name"] = Event._meta.get_lookup_info("tournament__name").field.model_field_name
    result["participants__in"] = queryset.get_lookup_info("participants__in").value_shape.value
    result["ordering"] = [
        [field.model_field_name for field in Event._meta.get_ordering_info("-tournament").fields],
        Event._meta.get_ordering_info("-tournament").descending,
    ]
    result["name_lookups"] = sorted(Event._meta.get_lookups("name", dialect))[:3]
    print(json.dumps(result, sort_keys=True))
    """
)


def run_describe_unbound() -> dict:
    completed = subprocess.run(
        [sys.executable, "-c", DESCRIBE_UNBOUND_SCRIPT], capture_output=True, text=True, timeout=120
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_unbound_model_refuses_descriptions_and_describes_once_bound():
    result = run_describe_unbound()

    assert result["bound_before"] == [False, False]
    expected_error = (
        "Event is not bound yet: call Hare.bind_models() or Hare.init() before describing its filters or orderings"
    )
    assert result["before"] == {name: expected_error for name in result["before"]}
    assert len(result["before"]) == 22

    assert result["bound_after"] == [True, True]
    assert set(result["after"]) == set(result["before"])
    for name, type_name in result["after"].items():
        if "get_lookup_info" in name:
            assert type_name == "LookupInfo", name
        elif "get_ordering_info" in name:
            assert type_name == "OrderingInfo", name
        else:
            assert type_name == "dict", name
    assert result["tournament__name"] == "name"
    assert result["participants__in"] == "list"
    assert result["ordering"] == [["tournament_id"], True]
    assert result["name_lookups"][0] == ""
