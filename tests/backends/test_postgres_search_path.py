import pytest

from hare.dialects.base.connection.db_url_config_generator import DbUrlConfigGenerator
from hare.dialects.postgresql import SearchPath
from hare.exceptions import ConfigurationError

pytestmark = pytest.mark.database_independent


@pytest.mark.parametrize(
    "names",
    [(), ("public",), ("$user", "pg_temp", "MixedCase"), ("a,b", 'a"b', "a b", "a\\b"), ("схема",)],
)
def test_search_path_setting_round_trip(names):
    path = SearchPath(*names)
    assert SearchPath.parse(path.to_setting()) == path
    assert path.schemas == names


@pytest.mark.parametrize(
    "setting,names",
    [
        (' PUBLIC, "Mixed,Case", "quote""name" ', ("public", "Mixed,Case", 'quote"name')),
        ("СХЕМА, École, PUBLIC", ("СХЕМА", "École", "public")),
        ('name;Other, 1First, a-b, a"B', ("name;other", "1first", "a-b", 'a"b')),
        ("a\u00a0B, a\vB", ("a\u00a0b", "a\vb")),
        ("\tPUBLIC\n,\r Shared\f", ("public", "shared")),
    ],
)
def test_setting_names_follow_postgresql_quoting_and_case_folding(setting, names):
    path = SearchPath.parse(setting)
    assert path.schemas == names
    assert SearchPath.parse(path.to_setting()) == path


@pytest.mark.parametrize("setting", ["public,", "public, ", "public,,next", '"unterminated', "a b", '""', "a\0b"])
def test_invalid_search_path_setting_is_refused(setting):
    with pytest.raises(ConfigurationError, match="search_path"):
        SearchPath.parse(setting)


def test_tenant_replacement_changes_only_the_leading_schema():
    path = SearchPath("Tenant,Old", "Shared,Schema", "$user", "Tenant,Old")
    changed = path.prepended("Tenant,New", replacing="Tenant,Old")
    assert changed.schemas == ("Tenant,New", "Shared,Schema", "$user", "Tenant,Old")
    assert path.schemas[0] == "Tenant,Old"


@pytest.mark.parametrize("scheme", ["postgresql", "postgresql+asyncpg"])
def test_connection_url_keeps_schema_separate_from_search_path(scheme):
    credentials = DbUrlConfigGenerator.expand(
        f"{scheme}://localhost/db?schema=Shared%2CSchema&search_path=%22Tenant%2COne%22%2C%22Shared%2CSchema%22"
    )["credentials"]
    assert credentials["schema"] == "Shared,Schema"
    assert credentials["search_path"] == SearchPath("Tenant,One", "Shared,Schema")


def test_shell_option_escapes_argument_separators():
    path = SearchPath("a b", "c\\d")
    assert path.to_shell_option() == '-c search_path="a\\ b",\\ "c\\\\d"'
