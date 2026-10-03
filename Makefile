checkfiles = hare/ tests/ conftest.py
mypy_checkfiles = hare/ conftest.py
py_warn = PYTHONDEVMODE=1
pytest_opts = -n auto --cov=hare --cov-append --cov-branch --tb=native -q --durations=25
# Postgres runs are bound by the server, not by local cores: past ~8 workers the extra backends
# only contend with each other (measured on a 16-core machine: -n 8 = 88 s, -n 12 = 121 s).
HARE_POSTGRES_WORKERS ?= 8
pytest_postgres_opts = $(subst -n auto,-n $(HARE_POSTGRES_WORKERS),$(pytest_opts))

# Overridable on the command line, e.g. `make test_postgres_asyncpg HARE_POSTGRES_HOST=db.local
# HARE_POSTGRES_PORT=5432` - defaults match the test server `make test_db_up` starts.
HARE_POSTGRES_USER ?= postgres
HARE_POSTGRES_PASS ?= postgres
HARE_POSTGRES_HOST ?= 127.0.0.1
HARE_POSTGRES_PORT ?= 5433

# Test Postgres server (tests/docker/postgres): PostGIS + pgvector + the contrib extensions, so every
# Postgres-only test runs against one server. Durability is switched off - every DROP DATABASE
# forces a checkpoint, and with fsync on those checkpoints dominated the Postgres suite's runtime
# (~3 s per DROP under parallel load). max_prepared_transactions enables the distributed
# transaction tests. Never point it at data you want to keep.
test_db_image = hare-orm-test-postgres:18
test_db_container = hare-orm-test-db

# Postgres driver tests (asyncpg/rust_pg) skip cleanly under PyPy - neither driver ships a PyPy
# wheel. `python -V | grep PyPy` exits 0 (match) only on PyPy, so `||` runs the real test command
# only when it's NOT PyPy.
skip_on_pypy = poetry run python -V | grep PyPy ||

# HARE_TEST_DB's "\{\}" is a literal, backslash-escaped "{}" placeholder - conftest.py's
# _resolve_db_url() and DbUrlConfigGenerator.expand() both unescape it once and fill it with a
# fresh per-run UUID, so parallel/repeated test runs never collide on the same throwaway database
# name (see _resolve_db_url()'s own docstring for the real "database does not exist" CI bug this
# fixed). MSYS2_ENV_CONV_EXCL is required here specifically for Windows Git Bash: without it, MSYS
# silently mangles "\{\}" into "/{/}" before Python ever sees it (verified directly - breaks
# test_postgres_asyncpg and test_postgres_rust identically); harmless no-op on real Linux/macOS
# shells, where this whole variable is simply unrecognized.
test_db_env = MSYS2_ENV_CONV_EXCL=HARE_TEST_DB HARE_TEST_DB

help:
	@echo  "Hare ORM development makefile"
	@echo
	@echo  "usage: make <target>"
	@echo  "Targets:"
	@echo  "    up                 Updates dev/test dependencies"
	@echo  "    deps               Ensure dev/test dependencies are installed"
	@echo  "    check              Checks that build is sane (style + mypy + bandit)"
	@echo  "    style              Auto-formats the code"
	@echo  "    lint               Auto-formats the code and check type hints"
	@echo  "    test               Runs the full test suite against sqlite"
	@echo  "    test_fast          Runs the test suite against sqlite (no coverage at all)"
	@echo  "    test_sqlite        Runs the test suite against sqlite (coverage off)"
	@echo  "    test_sqlite_regexp Runs the regexp tests against sqlite with install_regexp_functions"
	@echo  "    test_columnar      Runs the test suite against the columnar test dialect (a third-party dialect)"
	@echo  "    test_db_up         Builds and starts the test Postgres server (docker, port HARE_POSTGRES_PORT)"
	@echo  "    test_db_down       Stops and removes the test Postgres server"
	@echo  "    test_postgres_asyncpg  Runs the test suite against Postgres via asyncpg"
	@echo  "    build_native       Builds rust.native (the rust_pg driver, row codecs) for this OS into rust/ (needs a Rust toolchain)"
	@echo  "    build_native_linux Builds the Linux rust.native into rust/ in Docker"
	@echo  "    rust_check         Runs cargo fmt --check, clippy and the Rust unit tests"
	@echo  "    test_postgres_rust Runs the test suite against Postgres via the rust_pg driver"
	@echo  "    testall / ci       Runs check + test_sqlite, test_columnar and test_postgres_asyncpg"
	@echo  "    docs               Builds the docs into site/, failing on a broken link or anchor"
	@echo  "    build              Builds the sdist/wheel into dist/"
	@echo  "    publish            Builds and uploads to PyPI via twine"

up:
	poetry lock

deps:
	poetry install --extras asyncpg --extras opentelemetry $(options)


# check/style/lint/codeqc are pure code-quality checks (formatting, types, security) - none of
# them need a built package, only `deps`. The old versions all depended on `build` (a full
# `poetry build`, rebuilding dist/ from scratch every time) purely because `twine check dist/*`
# used to live inside _codeqc - conflating "is my code well-formatted/typed" with "is my BUILT
# PACKAGE valid for upload" and making every style check pay for a full package build it doesn't
# need. twine check now lives in `build` itself, the one target that actually produces dist/.
#
# Each public target still repeats `deps` as its own prerequisite rather than the private one
# depending on it: `$(MAKE) _codeqc` below is a fresh sub-make invocation that doesn't see the
# outer invocation's already-satisfied prerequisites, so a private target with its own `deps`
# prerequisite would re-run `poetry install` a second time for no reason.
check: deps _check
_check:
	poetry run ruff format --check $(checkfiles) || (echo "Please run 'make style' to auto-fix style issues" && false)
	poetry run ruff check $(checkfiles)
	"$(MAKE)" _codeqc
	"$(MAKE)" rust_check

style: deps
	poetry run ruff format $(checkfiles)
	poetry run ruff check --fix $(checkfiles)

lint: deps _lint
_lint:
	"$(MAKE)" style
	"$(MAKE)" _codeqc

codeqc: deps _codeqc
_codeqc:
	poetry run mypy $(mypy_checkfiles)
	poetry run bandit -c pyproject.toml -r $(checkfiles)

test: deps
	$(py_warn) HARE_TEST_DB=sqlite://:memory: poetry run pytest $(pytest_opts)

test_fast: deps
	$(py_warn) HARE_TEST_DB=sqlite://:memory: poetry run pytest -n auto --tb=native -q

test_sqlite:
	$(py_warn) HARE_TEST_DB=sqlite://:memory: poetry run pytest --cov-report= $(pytest_opts)

# Scoped to the only tests whose outcome actually depends on install_regexp_functions - the
# whole suite behaves identically with or without it otherwise, so running every test a second
# time here just to exercise this one query param buys nothing.
test_sqlite_regexp:
	$(py_warn) HARE_TEST_DB=sqlite://:memory:?install_regexp_functions=True poetry run pytest --cov-report= $(pytest_opts) tests/test_posix_regex_filter.py tests/backends/test_sqlite_client.py

# The columnar test dialect (tests/dialects/columnar) - a third-party dialect built only from
# hare's public API, without transactions, foreign keys or unique constraints. The whole suite runs
# against it, each test skipped where the dialect lacks what it needs.
test_columnar:
	$(py_warn) HARE_TEST_DB=columnar://:memory: poetry run pytest --cov-report= $(pytest_opts)

test_db_up:
	docker build -t $(test_db_image) tests/docker/postgres
	docker start $(test_db_container) 2>/dev/null || docker run -d --name $(test_db_container) --restart unless-stopped 		-p $(HARE_POSTGRES_PORT):5432 -e POSTGRES_USER=$(HARE_POSTGRES_USER) -e POSTGRES_PASSWORD=$(HARE_POSTGRES_PASS) 		$(test_db_image) -c fsync=off -c synchronous_commit=off -c full_page_writes=off 		-c max_prepared_transactions=64 -c max_connections=400 -c shared_buffers=512MB
	until docker exec $(test_db_container) pg_isready -q -h 127.0.0.1; do sleep 1; done

test_db_down:
	docker rm -f $(test_db_container)

test_postgres_asyncpg:
	$(skip_on_pypy) $(py_warn) $(test_db_env)="postgresql+asyncpg://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" poetry run pytest $(pytest_postgres_opts) --cov-report=

# rust.native (rust/native/) - the rust_pg driver, the row readers/writers and the field codecs - is
# a maturin project, not a poetry dependency. `maturin develop` installs it into poetry's venv; the
# build is then copied flat into rust/, where the checkout and the wheel import it from (`rust` is a
# namespace package, so a stale flat file would otherwise shadow the fresh build).
#
# PYO3_USE_RAW_DYLIB=0: Windows raw-dylib linking does not resolve the private CPython symbol the crate
# declares itself (_PyDict_NewPresized).
build_native:
	pip install maturin
	rm -f rust/native*.so rust/native*.pyd
	PYO3_USE_RAW_DYLIB=0 poetry run maturin develop --release --manifest-path rust/native/Cargo.toml
	cp rust/native/python/rust/native*.* rust/

# The Linux extension built in the maturin image, for a Windows or macOS checkout that commits both.
build_native_linux:
	rm -f rust/native*.so
	MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)":/io -w /io ghcr.io/pyo3/maturin build --release --manifest-path rust/native/Cargo.toml --interpreter python3.14 --out /io/rust/native/wheels
	poetry run python -c "import pathlib, zipfile; wheel = max(pathlib.Path('rust/native/wheels').glob('*linux*.whl')); archive = zipfile.ZipFile(wheel); member = next(name for name in archive.namelist() if name.startswith('rust/native.') and name.endswith('.so')); pathlib.Path('rust', pathlib.Path(member).name).write_bytes(archive.read(member))"
	rm -rf rust/native/wheels

rust_check:
	cd rust/native && cargo fmt --check
	cd rust/native && cargo clippy --all-targets -- -D warnings
	cd rust/native && PYO3_USE_RAW_DYLIB=0 cargo test --no-default-features

# The whole suite, as test_postgres_asyncpg - the driver decodes every value the ORM reads.
test_postgres_rust:
	$(skip_on_pypy) $(py_warn) $(test_db_env)="postgresql://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" poetry run pytest $(pytest_postgres_opts) --cov-report=

# test_postgres_rust is not part of _testall/testall/ci: it needs rust.native built (build_native),
# which `make deps` doesn't do. CI runs it as its own job.
_testall: test_sqlite test_columnar test_postgres_asyncpg
	poetry run coverage report

testall: deps _testall

ci: check _testall

docs: deps
	poetry run mkdocs build --strict

build: deps
	rm -fR dist/
	poetry build
	poetry run twine check dist/*

publish: build
	poetry run twine upload dist/*
