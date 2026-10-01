import re

import pytest

from hare.dialects.base.db_url import DbUrlConfigGenerator
from hare.exceptions import ConfigurationError, UnSupportedError

# These are pure logic tests - no database fixture needed

_postgres_scheme_engines = {
    "postgresql": "postgresql",
    "postgresql+asyncpg": "postgresql+asyncpg",
}


def test_unknown_scheme():
    with pytest.raises(ConfigurationError):
        DbUrlConfigGenerator.expand("moo://baa")


@pytest.mark.parametrize(
    ("db_url", "file_path"),
    [
        pytest.param("sqlite:///some/test.sqlite", "/some/test.sqlite", id="sqlite_basic"),
        pytest.param("sqlite://test.sqlite", "test.sqlite", id="sqlite_relative"),
        pytest.param("sqlite://data/db.sqlite", "data/db.sqlite", id="sqlite_relative_with_subdir"),
    ],
)
def test_sqlite_url_is_expanded(db_url, file_path):
    res = DbUrlConfigGenerator.expand(db_url)
    assert res == {
        "engine": "sqlite",
        "credentials": {
            "file_path": file_path,
            "journal_mode": "WAL",
            "journal_size_limit": 16384,
        },
    }


def test_sqlite_no_db():
    with pytest.raises(ConfigurationError, match="No path specified for DB_URL"):
        DbUrlConfigGenerator.expand("sqlite://")


def test_sqlite_testing():
    res = DbUrlConfigGenerator.expand(db_url="sqlite:///some/test-{}.sqlite", testing=True)
    file_path = res["credentials"]["file_path"]
    assert "/some/test-" in file_path
    assert ".sqlite" in file_path
    assert "sqlite:///some/test-{}.sqlite" != file_path
    assert res == {
        "engine": "sqlite",
        "credentials": {
            "file_path": file_path,
            "journal_mode": "WAL",
            "journal_size_limit": 16384,
        },
    }


def test_sqlite_params():
    res = DbUrlConfigGenerator.expand("sqlite:///some/test.sqlite?AHA=5&moo=yes&journal_mode=TRUNCATE")
    assert res == {
        "engine": "sqlite",
        "credentials": {
            "file_path": "/some/test.sqlite",
            "AHA": "5",
            "moo": "yes",
            "journal_mode": "TRUNCATE",
            "journal_size_limit": 16384,
        },
    }


def test_sqlite_invalid():
    with pytest.raises(ConfigurationError):
        DbUrlConfigGenerator.expand("sqlite://")


@pytest.mark.parametrize(
    ("db_url", "file_path"),
    [
        # `sqlite:///C:/some/test.sqlite` parses to a raw url.path of "/C:/some/test.sqlite" - the
        # leading "/" before the drive letter must be stripped so `file_path` is a real Windows
        # absolute path, matching exactly what SQLite itself opens. Left unstripped (confirmed live on
        # Windows), SqliteClient.db_delete()'s own os.remove() silently no-ops against that
        # non-existent path instead of deleting the real file - see config_generator.py's own comment
        # at this stripping step.
        pytest.param(
            "sqlite:///C:/some/test.sqlite",
            "C:/some/test.sqlite",
            id="sqlite_windows_drive_letter_path_strips_extra_leading_slash",
        ),
        pytest.param(
            "sqlite:///d:/some/test.sqlite",
            "d:/some/test.sqlite",
            id="sqlite_windows_drive_letter_path_case_insensitive",
        ),
        # A path that merely happens to have a colon somewhere past its 2nd character (not a
        # Windows drive-letter prefix) must NOT be mistaken for one.
        pytest.param(
            "sqlite:///some/weird:path.sqlite",
            "/some/weird:path.sqlite",
            id="sqlite_non_drive_letter_path_keeps_leading_slash",
        ),
        pytest.param(
            "sqlite:///some/user@host/db.sqlite", "/some/user@host/db.sqlite", id="sqlite_path_with_at_sign_is_kept"
        ),
    ],
)
def test_sqlite_url_path_is_read_as_written(db_url, file_path):
    res = DbUrlConfigGenerator.expand(db_url)
    assert res["credentials"]["file_path"] == file_path


@pytest.mark.parametrize(
    ("url_tail", "credentials"),
    [
        pytest.param(
            "postgres:moo@127.0.0.1:54321/test",
            {"database": "test", "host": "127.0.0.1", "password": "moo", "port": 54321, "user": "postgres"},
            id="basic",
        ),
        pytest.param(
            "postgres:kx%25jj5%2Fg@127.0.0.1:54321/test",
            {"database": "test", "host": "127.0.0.1", "password": "kx%jj5/g", "port": 54321, "user": "postgres"},
            id="encoded_password",
        ),
        # A literal '#' in a password (an entirely ordinary character, unlike '['/']') used to be
        # passed straight through to urlparse, which treats it as introducing the URL fragment -
        # silently truncating everything after it (host/port/database lost, username/password
        # resolved to None) instead of raising anything that points at the real cause.
        pytest.param(
            "postgres:sec#ret@127.0.0.1:54321/test",
            {"database": "test", "host": "127.0.0.1", "password": "sec#ret", "port": 54321, "user": "postgres"},
            id="hash_in_password",
        ),
        pytest.param(
            "postgres:sec?ret@127.0.0.1:54321/test",
            {"database": "test", "host": "127.0.0.1", "password": "sec?ret", "port": 54321, "user": "postgres"},
            id="question_mark_in_password",
        ),
        pytest.param(
            "postgres:moo@127.0.0.1:54321",
            {"database": None, "host": "127.0.0.1", "password": "moo", "port": 54321, "user": "postgres"},
            id="no_db",
        ),
        pytest.param(
            "postgres@127.0.0.1/test",
            {"database": "test", "host": "127.0.0.1", "password": None, "port": 5432, "user": "postgres"},
            id="no_port",
        ),
    ],
)
def test_postgres_url_is_expanded(url_tail, credentials):
    for scheme, engine in _postgres_scheme_engines.items():
        assert DbUrlConfigGenerator.expand(f"{scheme}://{url_tail}") == {"engine": engine, "credentials": credentials}


def test_postgres_nonint_port():
    for scheme in _postgres_scheme_engines:
        with pytest.raises(ConfigurationError):
            DbUrlConfigGenerator.expand(f"{scheme}://postgres:@127.0.0.1:moo/test")


def test_postgres_testing():
    for scheme, engine in _postgres_scheme_engines.items():
        res = DbUrlConfigGenerator.expand(db_url=(f"{scheme}://postgres@127.0.0.1:5432/" + r"test_\{\}"), testing=True)
        database = res["credentials"]["database"]
        assert "test_" in database
        assert "test_{}" != database
        assert res == {
            "engine": engine,
            "credentials": {
                "database": database,
                "host": "127.0.0.1",
                "password": None,
                "port": 5432,
                "user": "postgres",
            },
        }


def test_postgres_params():
    """asyncpg itself rejects an unknown connect kwarg, so unknown query params pass through to it."""
    res = DbUrlConfigGenerator.expand("postgresql+asyncpg://postgres@127.0.0.1:5432/test?AHA=5&moo=yes")
    expected_credentials = {
        "database": "test",
        "host": "127.0.0.1",
        "password": None,
        "port": 5432,
        "user": "postgres",
        "AHA": "5",
        "moo": "yes",
    }
    assert res == {"engine": "postgresql+asyncpg", "credentials": expected_credentials}


@pytest.mark.parametrize("scheme", ["postgresql"])
def test_rust_driver_rejects_unknown_query_param(scheme):
    """The rust driver has no passthrough - an unknown query param used to be dropped silently."""
    with pytest.raises(ConfigurationError, match="max_queries"):
        DbUrlConfigGenerator.expand(f"{scheme}://postgres@127.0.0.1:5432/test?max_queries=5")


@pytest.mark.parametrize(
    ("query", "expected_ssl_mode"),
    [
        ("ssl=true", "require"),
        ("ssl=1", "require"),
        ("ssl=false", "disable"),
        ("ssl=verify-full", "verify-full"),
        ("sslmode=require", "require"),
        ("sslmode=verify-ca", "verify-ca"),
        ("sslmode=disable", "disable"),
        ("ssl_mode=prefer", "prefer"),
    ],
)
def test_rust_driver_ssl_query_params_map_to_ssl_mode(query, expected_ssl_mode):
    """`?ssl=true`/`?sslmode=require` used to be silently dropped by the rust driver, connecting
    without TLS."""
    for scheme in ("postgresql",):
        credentials = DbUrlConfigGenerator.expand(f"{scheme}://postgres@127.0.0.1:5432/test?{query}")["credentials"]
        assert credentials["ssl_mode"] == expected_ssl_mode
        assert "ssl" not in credentials
        assert "sslmode" not in credentials


@pytest.mark.parametrize(
    ("query", "expected_ssl"),
    [
        ("ssl=true", True),
        ("ssl=false", False),
        ("sslmode=verify-full", "verify-full"),
        ("ssl_mode=require", "require"),
    ],
)
def test_asyncpg_driver_ssl_query_params_map_to_ssl(query, expected_ssl):
    for scheme in ("postgresql+asyncpg",):
        credentials = DbUrlConfigGenerator.expand(f"{scheme}://postgres@127.0.0.1:5432/test?{query}")["credentials"]
        assert credentials["ssl"] == expected_ssl
        assert "sslmode" not in credentials
        assert "ssl_mode" not in credentials


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("postgresql://postgres@127.0.0.1:5432/test?ssl=maybe", ConfigurationError),
        ("postgresql://postgres@127.0.0.1:5432/test?sslmode=true", ConfigurationError),
        ("postgresql://postgres@127.0.0.1:5432/test?sslmode=allow", UnSupportedError),
        ("postgresql://postgres@127.0.0.1:5432/test?ssl=true&sslmode=disable", ConfigurationError),
        ("postgresql+asyncpg://postgres@127.0.0.1:5432/test?ssl=maybe", ConfigurationError),
    ],
)
def test_invalid_ssl_query_params_raise(url, error):
    with pytest.raises(error):
        DbUrlConfigGenerator.expand(url)


def test_postgres_asyncpg_suffix_applies_asyncpg_casts():
    """postgresql+asyncpg:// used the rust entry's casts, handing asyncpg strings for max_queries/timeout."""
    credentials = DbUrlConfigGenerator.expand(
        "postgresql+asyncpg://postgres@127.0.0.1:5432/test?max_queries=5&timeout=7&max_inactive_connection_lifetime=1.5"
    )["credentials"]
    assert credentials["max_queries"] == 5
    assert credentials["timeout"] == 7
    assert credentials["max_inactive_connection_lifetime"] == 1.5


def test_documented_retry_query_params_are_cast():
    for scheme in ("postgresql", "postgresql+asyncpg"):
        credentials = DbUrlConfigGenerator.expand(
            f"{scheme}://postgres@127.0.0.1:5432/test?read_retry_max_retries=3&read_retry_backoff_base_seconds=0.5"
        )["credentials"]
        assert credentials["read_retry_max_retries"] == 3
        assert credentials["read_retry_backoff_base_seconds"] == 0.5


@pytest.mark.parametrize(
    ("url", "expected_credentials"),
    [
        ("postgresql://user:pa/ss@localhost:5432/db", {"password": "pa/ss", "host": "localhost", "database": "db"}),
        (
            "postgresql://user:pass@localhost:5432/db?application_name=me@work",
            {"password": "pass", "host": "localhost", "database": "db", "application_name": "me@work"},
        ),
        (
            "postgresql://user:pass@/db?host=/var/run/postgresql",
            {"host": "/var/run/postgresql", "database": "db", "password": "pass"},
        ),
        ("postgresql://user:pass@localhost/my%20db", {"database": "my db", "host": "localhost"}),
        ("postgresql://user:p@ss/w@localhost/db", {"password": "p@ss/w", "host": "localhost", "database": "db"}),
        ("postgresql://user:pass@[::1]:5433/db", {"host": "::1", "port": 5433, "database": "db"}),
    ],
)
def test_postgres_url_parsing_edge_cases(url, expected_credentials):
    credentials = DbUrlConfigGenerator.expand(url)["credentials"]
    for key, value in expected_credentials.items():
        assert credentials[key] == value, key


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user@localhost/db?min_size=abc",
        "postgresql+asyncpg://user@localhost/db?timeout=soon",
        "sqlite:///some/test.sqlite?journal_size_limit=big",
        "postgresql://localhost:99999/db",
    ],
)
def test_invalid_url_values_raise_configuration_error(url):
    with pytest.raises(ConfigurationError):
        DbUrlConfigGenerator.expand(url)


def test_postgres_plus_sign_in_password():
    for scheme, engine in _postgres_scheme_engines.items():
        res = DbUrlConfigGenerator.expand(f"{scheme}://postgres:p%2Bss+word@127.0.0.1:54321/test")
        assert res["credentials"]["password"] == "p+ss+word"


@pytest.mark.parametrize("scheme", ["postgres", "postgresql+rust", "asyncpg", "postgres+asyncpg"])
def test_only_one_scheme_per_driver(scheme):
    with pytest.raises(ConfigurationError, match=f"Unknown DB scheme: {re.escape(scheme)}$"):
        DbUrlConfigGenerator.expand(f"{scheme}://postgres:moo@127.0.0.1:54321/test")


def test_postgres_connect_retry_params_are_cast():
    for scheme in _postgres_scheme_engines:
        res = DbUrlConfigGenerator.expand(
            f"{scheme}://postgres@127.0.0.1:5432/test?connect_max_retries=5&connect_retry_backoff_base_seconds=0.25"
        )
        assert res["credentials"]["connect_max_retries"] == 5
        assert isinstance(res["credentials"]["connect_max_retries"], int)
        assert res["credentials"]["connect_retry_backoff_base_seconds"] == 0.25
        assert isinstance(res["credentials"]["connect_retry_backoff_base_seconds"], float)


def test_postgres_pool_acquire_timeout_is_cast():
    for scheme in _postgres_scheme_engines:
        res = DbUrlConfigGenerator.expand(f"{scheme}://postgres@127.0.0.1:5432/test?pool_acquire_timeout=2.5")
        assert res["credentials"]["pool_acquire_timeout"] == 2.5
        assert isinstance(res["credentials"]["pool_acquire_timeout"], float)


def test_unknown_driver_suffix():
    for scheme in ("postgresql+bogus", "sqlite+rust"):
        with pytest.raises(ConfigurationError, match="Unknown DB scheme"):
            DbUrlConfigGenerator.expand(f"{scheme}://postgres@127.0.0.1:54321/test")


def test_driver_query_param_rejected():
    """The engine picks the driver - a ``?driver=`` query parameter is an unknown one."""
    with pytest.raises(ConfigurationError, match="driver"):
        DbUrlConfigGenerator.expand("postgresql://postgres@127.0.0.1:54321/test?driver=asyncpg")


def test_bool_query_param_false_string():
    """`bool("false")` is `True` - any non-empty string is truthy - so `?ssl=false` used to
    silently coerce to `ssl: True` instead of `False`."""
    res = DbUrlConfigGenerator.expand("postgresql+asyncpg://postgres@127.0.0.1:5432/test?ssl=false")
    assert res["credentials"]["ssl"] is False


def test_sqlite_fixed_path_unchanged_outside_xdist(monkeypatch):
    """No PYTEST_XDIST_WORKER (a plain, non-`-n` run) - a fixed testing db_url is used exactly as
    given, matching the pre-fix behavior."""
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    res = DbUrlConfigGenerator.expand("sqlite:///some/fixed/path.sqlite", testing=True)
    assert res["credentials"]["file_path"] == "/some/fixed/path.sqlite"


@pytest.mark.parametrize(
    ("worker_id", "db_url", "file_path"),
    [
        pytest.param(
            "gw3",
            "sqlite:///some/fixed/path.sqlite",
            "/some/fixed/path.gw3.sqlite",
            id="sqlite_fixed_path_gets_xdist_worker_suffix",
        ),
        pytest.param(
            "gw1",
            "sqlite:///some/fixed/path_no_ext",
            "/some/fixed/path_no_ext.gw1",
            id="sqlite_fixed_path_without_extension_gets_xdist_worker_suffix",
        ),
        # ":memory:" already gives every connection its own separate, empty database - no fixed-path
        # collision is possible, so it must never be rewritten.
        pytest.param("gw0", "sqlite://:memory:", ":memory:", id="sqlite_memory_untouched_by_xdist_worker"),
    ],
)
def test_sqlite_testing_path_of_an_xdist_worker(monkeypatch, worker_id, db_url, file_path):
    """A literal, non-templated sqlite db_url is otherwise the SAME file for every pytest-xdist
    worker - sqlite serializes writers even across separate connections to one file, so two
    workers hitting it at once get "database is locked" (confirmed live via a real `-n 2` run,
    see test_xdist_sqlite_locking.py). Under `-n`, the worker id is folded into the path so each
    worker gets its own file instead."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", worker_id)
    res = DbUrlConfigGenerator.expand(db_url, testing=True)
    assert res["credentials"]["file_path"] == file_path


def test_sqlite_templated_path_ignores_xdist_worker(monkeypatch):
    """The `{}` placeholder already gets a fresh random uuid on every call, which is unique
    regardless of worker - the xdist-worker suffix must not also apply here."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")
    res = DbUrlConfigGenerator.expand("sqlite:///some/test-{}.sqlite", testing=True)
    file_path = res["credentials"]["file_path"]
    assert "gw2" not in file_path
    assert "/some/test-" in file_path


def test_bool_query_param_true_variants():
    for value in ("true", "True", "1", "yes", "YES"):
        res = DbUrlConfigGenerator.expand(f"postgresql+asyncpg://postgres@127.0.0.1:5432/test?ssl={value}")
        assert res["credentials"]["ssl"] is True


def test_username_percent_encoded():
    """password was already unquoted via urlparse.unquote(); username wasn't - a percent-encoded
    username (e.g. containing an escaped '@') used to reach the driver still escaped."""
    res = DbUrlConfigGenerator.expand("postgresql+asyncpg://us%40er:pw@127.0.0.1:5432/test")
    assert res["credentials"]["user"] == "us@er"


def test_userinfo_literal_at_sign_with_brackets():
    """_quote_url_userinfo used to split on the FIRST '@', not the last - a literal '@' inside
    the password (before the real userinfo/host boundary) combined with a bracket character
    needing escaping would leave the bracket in the host portion and crash urlparse."""
    res = DbUrlConfigGenerator.expand("postgresql+asyncpg://user:p@ss[1]@127.0.0.1:5432/test")
    assert res["credentials"]["password"] == "p@ss[1]"
    assert res["credentials"]["host"] == "127.0.0.1"


def test_generate_config_basic():
    res = DbUrlConfigGenerator.build(
        db_url="sqlite:///some/test.sqlite",
        app_modules={"models": ["one.models", "two.models"]},
    )
    assert res == {
        "connections": {
            "default": {
                "credentials": {
                    "file_path": "/some/test.sqlite",
                    "journal_mode": "WAL",
                    "journal_size_limit": 16384,
                },
                "engine": "sqlite",
            }
        },
        "apps": {
            "models": {
                "models": ["one.models", "two.models"],
                "default_connection": "default",
            }
        },
    }


def test_generate_config_explicit(monkeypatch):
    # This asserts on an exact, un-suffixed `file_path` below - unrelated to the xdist-worker
    # suffixing tested separately above, so pinned to run as if outside `-n` regardless of
    # whether the outer suite invoking this very test is itself running under `-n`.
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    res = DbUrlConfigGenerator.build(
        db_url="sqlite:///some/test.sqlite",
        app_modules={"models": ["one.models", "two.models"]},
        connection_label="models",
        testing=True,
    )
    assert res == {
        "connections": {
            "models": {
                "credentials": {
                    "file_path": "/some/test.sqlite",
                    "journal_mode": "WAL",
                    "journal_size_limit": 16384,
                },
                "engine": "sqlite",
            }
        },
        "apps": {
            "models": {
                "models": ["one.models", "two.models"],
                "default_connection": "models",
            }
        },
    }


def test_generate_config_many_apps():
    res = DbUrlConfigGenerator.build(
        db_url="sqlite:///some/test.sqlite",
        app_modules={"models": ["one.models", "two.models"], "peanuts": ["peanut.models"]},
    )
    assert res == {
        "connections": {
            "default": {
                "credentials": {
                    "file_path": "/some/test.sqlite",
                    "journal_mode": "WAL",
                    "journal_size_limit": 16384,
                },
                "engine": "sqlite",
            }
        },
        "apps": {
            "models": {
                "models": ["one.models", "two.models"],
                "default_connection": "default",
            },
            "peanuts": {"models": ["peanut.models"], "default_connection": "default"},
        },
    }
