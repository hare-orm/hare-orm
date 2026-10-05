checkfiles = hare/ tests/ conftest.py mkdocs_hooks/
# The tests of the static types (assert_type) are checked too - at run time assert_type checks nothing.
mypy_checkfiles = hare/ conftest.py mkdocs_hooks/ tests/test_values_list_types.py tests/test_get_exceptions.py
py_warn = PYTHONDEVMODE=1
pytest_opts = -n auto --cov=hare --cov-append --cov-branch --tb=native -q --durations=25 --verify-plans
# The tests marked database_independent read no test database - the mypy plugin's runs, the stubs,
# the scans of hare's own source, the programs run in a process of their own. They are a suite of
# their own (test_database_independent); the suite of every database leaves them out.
pytest_database_opts = $(pytest_opts) -m "not database_independent"
# Postgres runs are bound by the server, not by local cores: past ~8 workers the extra backends
# only contend with each other (measured on a 16-core machine: -n 8 = 88 s, -n 12 = 121 s).
HARE_POSTGRES_WORKERS ?= 8
pytest_postgres_opts = $(subst -n auto,-n $(HARE_POSTGRES_WORKERS),$(pytest_database_opts))

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
	@echo  "    check              Checks that build is sane (style + mypy + bandit + rust_check + actionlint)"
	@echo  "    actionlint         Lints the GitHub Actions workflows (docker, or ACTIONLINT=actionlint)"
	@echo  "    style              Auto-formats the code"
	@echo  "    lint               Auto-formats the code and check type hints"
	@echo  "    test               Runs the full test suite against sqlite"
	@echo  "    test_fast          Runs the test suite against sqlite (no coverage at all)"
	@echo  "    test_sqlite        Runs the test suite against sqlite (coverage off)"
	@echo  "    test_database_independent  Runs the tests reading no test database (mypy plugin, stubs, scans)"
	@echo  "    test_sqlite_regexp Runs the regexp tests against sqlite with install_regexp_functions"
	@echo  "    test_columnar      Runs the test suite against the columnar test dialect (a third-party dialect)"
	@echo  "    test_poetry_add    Adds hare-orm from this checkout to a new poetry project"
	@echo  "    test_db_up         Builds and starts the test Postgres server (docker, port HARE_POSTGRES_PORT)"
	@echo  "    test_db_down       Stops and removes the test Postgres server"
	@echo  "    test_brokers_up    Starts the test Redis, RabbitMQ and Kafka (docker, ports 6390/5673/9095)"
	@echo  "    test_brokers_down  Stops and removes the test brokers"
	@echo  "    test_brokers       Runs the outbox wakeup and delivery tests against the test brokers"
	@echo  "    test_postgres_asyncpg  Runs the test suite against Postgres via asyncpg"
	@echo  "    test_db_matrix_up  Builds and starts a test Postgres server per matrix version (ports 5414-5418)"
	@echo  "    test_db_matrix_down Stops and removes the matrix Postgres servers"
	@echo  "    matrix_venvs       Creates .venv-3.12/.venv-3.13/.venv-3.14 with every dependency group"
	@echo  "    sqlite_versions    Downloads and prepares the matrix SQLite versions for each matrix Python"
	@echo  "    spatialite         Downloads SpatiaLite for the SpatiaLite tests (Windows) and prints their variables"
	@echo  "    test_clickhouse_up / test_clickhouse_down  Starts / removes the ClickHouse test server (ports 8124, 9124)"
	@echo  "    test_clickhouse    Runs the ClickHouse dialect's tests over HTTP and over the native protocol"
	@echo  "    test_clickhouse_cluster_up / test_clickhouse_cluster_down  Starts / removes the two-server ClickHouse cluster (ports 8126-8127, 9126-9127)"
	@echo  "    test_clickhouse_cluster  Runs the ClickHouse cluster tests over HTTP and over the native protocol"
	@echo  "    test_matrix        Runs the suite on every Python x SQLite/columnar/Postgres version, prints a table"
	@echo  "    lowest_dependencies_venv  Creates .venv-lowest: Python 3.12, hare's direct dependencies at their lowest versions"
	@echo  "    test_lowest_dependencies  Runs the suite in .venv-lowest on SQLite and Postgres via both drivers"
	@echo  "    build_native       Builds rust.native (the rust_pg driver, row codecs) for this OS into rust/ (needs a Rust toolchain)"
	@echo  "    build_native_linux Builds the Linux rust.native of every matrix Python into rust/ in Docker"
	@echo  "    build_native_windows Builds the Windows rust.native of every matrix Python (.venv-3.X) into rust/"
	@echo  "    build_native_macos Builds the macOS rust.native (universal2) of every matrix Python (.venv-3.X) into rust/"
	@echo  "    rust_check         Runs cargo fmt --check, clippy and the Rust unit tests"
	@echo  "    test_postgres_rust Runs the test suite against Postgres via the rust_pg driver"
	@echo  "    testall / ci       Runs check + test_database_independent, test_sqlite, test_columnar, test_postgres_asyncpg"
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
	"$(MAKE)" actionlint

# actionlint checks the workflows of .github/workflows - their syntax, expressions, job dependencies,
# and their shell scripts through shellcheck. The default runs the pinned actionlint image, which
# carries shellcheck; `make actionlint ACTIONLINT=actionlint` runs an installed binary instead.
actionlint_image = rhysd/actionlint:1.7.12
ACTIONLINT ?= MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)":/repo -w /repo $(actionlint_image)
actionlint:
	$(ACTIONLINT) -color

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
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest $(pytest_opts)

test_fast: deps
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest -n auto --tb=native -q

test_sqlite:
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest --cov-report= $(pytest_database_opts)

# The tests reading no test database - once, whatever databases the other suites run on.
test_database_independent:
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest --cov-report= $(pytest_opts) -m database_independent

# Scoped to the only tests whose outcome actually depends on install_regexp_functions - the
# whole suite behaves identically with or without it otherwise, so running every test a second
# time here just to exercise this one query param buys nothing.
test_sqlite_regexp:
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory:?install_regexp_functions=True poetry run pytest --cov-report= $(pytest_opts) tests/test_posix_regex_filter.py tests/backends/test_sqlite_client.py

# A new poetry project adding hare-orm from this checkout as its dependency (tests/test_version.py).
test_poetry_add:
	$(py_warn) HARE_TEST_POETRY_ADD=1 poetry run pytest -p no:cacheprovider --no-cov --tb=native -q tests/test_version.py::test_added_by_poetry_v2

# The columnar test dialect (tests/dialects/columnar) - a third-party dialect built only from
# hare's public API, without transactions, foreign keys or unique constraints. The whole suite runs
# against it, each test skipped where the dialect lacks what it needs.
test_columnar:
	$(py_warn) HARE_TEST_DB=columnar://:memory: poetry run pytest --cov-report= $(pytest_database_opts)

test_db_up:
	docker build -t $(test_db_image) tests/docker/postgres
	docker start $(test_db_container) 2>/dev/null || docker run -d --name $(test_db_container) --restart unless-stopped 		-p $(HARE_POSTGRES_PORT):5432 -e POSTGRES_USER=$(HARE_POSTGRES_USER) -e POSTGRES_PASSWORD=$(HARE_POSTGRES_PASS) 		$(test_db_image) -c fsync=off -c synchronous_commit=off -c full_page_writes=off 		-c max_prepared_transactions=64 -c max_connections=400 -c shared_buffers=512MB
	until docker exec $(test_db_container) pg_isready -q -h 127.0.0.1; do sleep 1; done

test_db_down:
	docker rm -f $(test_db_container)

# Test brokers of the outbox's wakeups and deliveries (tests/contrib/outbox/test_brokers.py) - Redis on
# port 6390, RabbitMQ on 5673, Kafka (one KRaft node) on 9095. test_brokers runs those tests against
# them; without the variables the tests are skipped.
test_brokers_env = HARE_TEST_REDIS_URL=redis://localhost:6390/0 HARE_TEST_RABBITMQ_URL=amqp://guest:guest@localhost:5673/ \
	HARE_TEST_KAFKA_BOOTSTRAP=localhost:9095

test_brokers_up:
	docker start hare-orm-test-redis 2>/dev/null || docker run -d --name hare-orm-test-redis -p 6390:6379 redis:7-alpine
	docker start hare-orm-test-rabbitmq 2>/dev/null || docker run -d --name hare-orm-test-rabbitmq -p 5673:5672 \
		rabbitmq:4-management-alpine
	docker start hare-orm-test-kafka 2>/dev/null || docker run -d --name hare-orm-test-kafka -p 9095:9095 \
		-e KAFKA_NODE_ID=1 -e KAFKA_PROCESS_ROLES=broker,controller \
		-e KAFKA_LISTENERS=PLAINTEXT://0.0.0.0:9095,CONTROLLER://0.0.0.0:9093 \
		-e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://localhost:9095 \
		-e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
		-e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
		-e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
		-e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
		-e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 -e KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0 \
		apache/kafka:3.9.1
	until docker exec hare-orm-test-rabbitmq rabbitmq-diagnostics -q check_port_connectivity >/dev/null 2>&1; do sleep 1; done

test_brokers_down:
	docker rm -f hare-orm-test-redis hare-orm-test-rabbitmq hare-orm-test-kafka

test_brokers:
	$(py_warn) $(test_brokers_env) HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest -p no:cacheprovider --no-cov --tb=native -q \
		tests/contrib/outbox/test_brokers.py

# PgBouncer in transaction pooling in front of the test database (tests/docker/pgbouncer) on port
# 6432, with prepared statements across transactions (PgBouncer 1.21+, the oldest hare supports).
# test_pgbouncer runs the Postgres suite through it with both drivers; what needs a session of its
# own (LISTEN, a session lock timeout) goes straight to the server (direct_host/direct_port).
pgbouncer_image = hare-orm-test-pgbouncer
pgbouncer_container = hare-orm-test-pgbouncer
pgbouncer_options = transaction_pooling=true&direct_host=$(HARE_POSTGRES_HOST)&direct_port=$(HARE_POSTGRES_PORT)

test_pgbouncer_up:
	docker build -t $(pgbouncer_image) tests/docker/pgbouncer
	docker start $(pgbouncer_container) 2>/dev/null || docker run -d --name $(pgbouncer_container) \
		--add-host=host.docker.internal:host-gateway -p 6432:6432 -e PGBOUNCER_SERVER_PORT=$(HARE_POSTGRES_PORT) \
		$(pgbouncer_image)

test_pgbouncer_down:
	docker rm -f $(pgbouncer_container)

test_pgbouncer:
	$(py_warn) $(test_db_env)="postgresql+asyncpg://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):6432/test_\{\}?$(pgbouncer_options)" poetry run pytest $(pytest_postgres_opts) --cov-report=
	$(py_warn) $(test_db_env)="postgresql://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):6432/test_\{\}?$(pgbouncer_options)" poetry run pytest $(pytest_postgres_opts) --cov-report=

# The ClickHouse test server (hare.dialects.clickhouse) - its HTTP interface on port 8124 and its
# native protocol on 9124 (the benchmark's SQLAlchemy and Django participants use it), the
# default user's password HARE_CLICKHOUSE_PASS. The server is its own ClickHouse Keeper (port 9182 on
# the host) with transactions on (tests/docker/clickhouse). test_clickhouse runs the dialect's own tests:
# the shared test models have keys the database generates, which ClickHouse doesn't.
clickhouse_image = hare-orm-test-clickhouse:25.8
clickhouse_container = hare-orm-test-clickhouse
HARE_CLICKHOUSE_PASS ?= clickhouse

test_clickhouse_up:
	docker build -t $(clickhouse_image) --build-arg CLICKHOUSE_VERSION=25.8 tests/docker/clickhouse
	docker start $(clickhouse_container) 2>/dev/null || docker run -d --name $(clickhouse_container) 		--restart unless-stopped -p 8124:8123 -p 9124:9000 -p 9182:9181 -e CLICKHOUSE_PASSWORD=$(HARE_CLICKHOUSE_PASS) 		--ulimit nofile=262144:262144 $(clickhouse_image)
	until docker exec $(clickhouse_container) clickhouse-client --password $(HARE_CLICKHOUSE_PASS) -q "SELECT 1" >/dev/null 2>&1; do sleep 1; done

test_clickhouse_down:
	docker rm -f $(clickhouse_container)

# Over both drivers: clickhouse-connect on the HTTP interface, clickhouse-driver on the native protocol.
test_clickhouse:
	$(py_warn) $(test_db_env)="clickhouse+clickhouse-connect://default:$(HARE_CLICKHOUSE_PASS)@127.0.0.1:8124/test_\{\}" poetry run pytest $(pytest_postgres_opts) --cov-report= tests/dialects/clickhouse
	$(py_warn) $(test_db_env)="clickhouse+clickhouse-driver://default:$(HARE_CLICKHOUSE_PASS)@127.0.0.1:9124/test_\{\}" poetry run pytest $(pytest_postgres_opts) --cov-report= tests/dialects/clickhouse

# The ClickHouse test cluster (tests/docker/clickhouse_cluster): two servers on a network of their own,
# one shard of two replicas (hare_replicated) and two shards (hare_sharded), the first server the
# cluster's Keeper. Their HTTP interfaces are on ports 8126 and 8127, their native protocol on 9126 and
# 9127. test_clickhouse_cluster runs the cluster tests through the first server.
clickhouse_cluster_network = hare-orm-test-clickhouse-cluster
clickhouse_cluster_nodes = 1 2

test_clickhouse_cluster_up:
	docker network create $(clickhouse_cluster_network) 2>/dev/null || true
	for node in $(clickhouse_cluster_nodes); do 		docker build -t hare-orm-test-clickhouse-node$$node:25.8 --build-arg CLICKHOUSE_VERSION=25.8 --build-arg NODE=node$$node tests/docker/clickhouse_cluster && 		(docker start hare-orm-test-clickhouse-node$$node 2>/dev/null || docker run -d --name hare-orm-test-clickhouse-node$$node 			--hostname hare-orm-test-clickhouse-node$$node --network $(clickhouse_cluster_network) --restart unless-stopped 			-p 812$$((5 + node)):8123 -p 912$$((5 + node)):9000 -e CLICKHOUSE_PASSWORD=$(HARE_CLICKHOUSE_PASS) 			--ulimit nofile=262144:262144 hare-orm-test-clickhouse-node$$node:25.8); 	done
	until docker exec hare-orm-test-clickhouse-node2 clickhouse-client --password $(HARE_CLICKHOUSE_PASS) -q "SELECT count() FROM system.zookeeper WHERE path = '/'" >/dev/null 2>&1; do sleep 1; done

test_clickhouse_cluster_down:
	for node in $(clickhouse_cluster_nodes); do docker rm -f hare-orm-test-clickhouse-node$$node; done
	docker network rm $(clickhouse_cluster_network)

test_clickhouse_cluster:
	$(py_warn) HARE_TEST_CLICKHOUSE_CLUSTER_DB="clickhouse+clickhouse-connect://default:$(HARE_CLICKHOUSE_PASS)@127.0.0.1:8126/test_cluster_\{\}?cluster=hare_sharded" poetry run pytest $(pytest_postgres_opts) --cov-report= tests/dialects/clickhouse/cluster
	$(py_warn) HARE_TEST_CLICKHOUSE_CLUSTER_DB="clickhouse+clickhouse-driver://default:$(HARE_CLICKHOUSE_PASS)@127.0.0.1:9126/test_cluster_\{\}?cluster=hare_sharded" poetry run pytest $(pytest_postgres_opts) --cov-report= tests/dialects/clickhouse/cluster

# The test matrix (tests/matrix): every Python of matrix_python_versions (each its own
# .venv-<version>), every SQLite of tests/sqlite_versions and every PostgreSQL of
# matrix_postgres_versions (each its own server, hare-orm-test-db-<version> on port 54<version>).
#
# Each CI test job runs one part of it through these same targets, the variables narrowed on the
# command line: `make matrix_venvs matrix_python_versions=3.13 matrix_interpreter=<its python>`,
# `make sqlite_versions matrix_python_versions=3.13`, `make test_db_matrix_up
# matrix_postgres_versions=16`, `make test_matrix MATRIX_OPTIONS="--python 3.13 --suite asyncpg-16"`.
matrix_python_versions = 3.12 3.13 3.14
matrix_postgres_versions = 14 15 16 17 18
# The SQLite versions sqlite_versions prepares - empty for every one of tests/sqlite_versions, newest
# for the newest one.
matrix_sqlite_versions =
# The interpreter matrix_venvs creates the virtualenvs from - empty for `py -X.Y` on Windows and
# `pythonX.Y` elsewhere; a path only makes sense with one version in matrix_python_versions.
matrix_interpreter =
# Options of tests/matrix/matrix_runner.py: --python, --suite, --workers, --print-failed-logs, --newest.
MATRIX_OPTIONS ?=

test_db_matrix_up:
	for version in $(matrix_postgres_versions); do \
		docker build -t hare-orm-test-postgres:$$version --build-arg PG_MAJOR=$$version tests/docker/postgres || exit 1; \
		docker start hare-orm-test-db-$$version 2>/dev/null || docker run -d --name hare-orm-test-db-$$version \
			--restart unless-stopped -p 54$$version:5432 -e POSTGRES_USER=$(HARE_POSTGRES_USER) \
			-e POSTGRES_PASSWORD=$(HARE_POSTGRES_PASS) hare-orm-test-postgres:$$version -c fsync=off \
			-c synchronous_commit=off -c full_page_writes=off -c max_prepared_transactions=64 \
			-c max_connections=400 -c shared_buffers=256MB || exit 1; \
	done
	for version in $(matrix_postgres_versions); do \
		until docker exec hare-orm-test-db-$$version pg_isready -q -h 127.0.0.1; do sleep 1; done; \
	done

test_db_matrix_down:
	for version in $(matrix_postgres_versions); do docker rm -f hare-orm-test-db-$$version; done

# One virtualenv per Python of the matrix, with every dependency group - `py -X.Y` on Windows,
# `pythonX.Y` elsewhere.
matrix_venvs:
	for version in $(matrix_python_versions); do \
		if [ -n "$(matrix_interpreter)" ]; then "$(matrix_interpreter)" -m venv .venv-$$version; \
		elif command -v py >/dev/null 2>&1; then py -$$version -m venv .venv-$$version; \
		else python$$version -m venv .venv-$$version; fi || exit 1; \
		VIRTUAL_ENV="$(CURDIR)/.venv-$$version" poetry install --all-groups --extras asyncpg --extras opentelemetry || exit 1; \
	done

# The SQLite versions of the matrix, prepared for each matrix Python - the run fails when a Python
# would still load another SQLite with the prepared environment.
sqlite_versions:
	for version in $(matrix_python_versions); do \
		if [ -x .venv-$$version/Scripts/python.exe ]; then python=.venv-$$version/Scripts/python.exe; \
		else python=.venv-$$version/bin/python; fi; \
		$$python -m tests.sqlite_versions.download_sqlite_versions $(matrix_sqlite_versions) || exit 1; \
	done

# SpatiaLite for the SpatiaLite tests on Windows, downloaded into tests/spatialite/ - prints only the
# environment variables pointing the tests at it (CI appends them to $GITHUB_ENV). Linux and macOS
# install it from their packages.
spatialite:
	@python=.venv-$(firstword $(matrix_python_versions))/Scripts/python.exe; \
	if [ ! -x $$python ]; then python=.venv-$(firstword $(matrix_python_versions))/bin/python; fi; \
	$$python -m tests.spatialite.download_spatialite

test_matrix:
	$(py_warn) poetry run python -m tests.matrix.matrix_runner $(MATRIX_OPTIONS)

# hare's direct dependencies - the [project] dependencies and every extra - at the lowest versions
# their declared bounds allow, on Python 3.12, resolved by uv (`--resolution lowest-direct`) into
# their own virtualenv; the test tools at their poetry.lock versions
# (tests/lowest_dependencies/locked_tool_requirements.py). LOWEST_DEPENDENCIES_VENV puts the
# virtualenv elsewhere.
LOWEST_DEPENDENCIES_VENV ?= .venv-lowest
lowest_dependencies_python = $(LOWEST_DEPENDENCIES_VENV)/$(if $(filter Windows_NT,$(OS)),Scripts/python.exe,bin/python)

lowest_dependencies_venv:
	rm -rf "$(LOWEST_DEPENDENCIES_VENV)"
	uv venv --python 3.12 "$(LOWEST_DEPENDENCIES_VENV)"
	"$(lowest_dependencies_python)" -m tests.lowest_dependencies.locked_tool_requirements > "$(LOWEST_DEPENDENCIES_VENV)/test-tools.in"
	uv pip compile pyproject.toml "$(LOWEST_DEPENDENCIES_VENV)/test-tools.in" --all-extras --resolution lowest-direct \
		--python "$(lowest_dependencies_python)" --output-file "$(LOWEST_DEPENDENCIES_VENV)/requirements.txt"
	uv pip install --python "$(lowest_dependencies_python)" -r "$(LOWEST_DEPENDENCIES_VENV)/requirements.txt"
	uv pip install --python "$(lowest_dependencies_python)" --no-deps -e .

# The whole suite in that virtualenv, on SQLite and on the test Postgres server (test_db_up) via both
# drivers - rust_pg needs rust.native for Python 3.12 in rust/.
test_lowest_dependencies:
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory: "$(lowest_dependencies_python)" -m pytest -n auto --tb=native -q --no-cov
	$(py_warn) $(test_db_env)="postgresql+asyncpg://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" "$(lowest_dependencies_python)" -m pytest -n $(HARE_POSTGRES_WORKERS) --tb=native -q --no-cov -m "not database_independent"
	$(py_warn) $(test_db_env)="postgresql://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" "$(lowest_dependencies_python)" -m pytest -n $(HARE_POSTGRES_WORKERS) --tb=native -q --no-cov -m "not database_independent"

test_postgres_asyncpg:
	$(py_warn) $(test_db_env)="postgresql+asyncpg://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" poetry run pytest $(pytest_postgres_opts) --cov-report=

# rust.native (rust/native/) - the rust_pg driver, the row readers/writers and the field codecs - is
# a maturin project, not a poetry dependency. `maturin develop` installs it into poetry's venv; the
# build is then copied flat into rust/, where the checkout and the wheel import it from (`rust` is a
# namespace package, so a stale flat file would otherwise shadow the fresh build).
#
# PYO3_USE_RAW_DYLIB=0: Windows raw-dylib linking does not resolve the private CPython symbol the crate
# declares itself (_PyDict_NewPresized).
build_native:
	rm -f rust/native*.so rust/native*.pyd
	PYO3_USE_RAW_DYLIB=0 poetry run maturin develop --release --manifest-path rust/native/Cargo.toml
	cp rust/native/python/rust/native*.* rust/

# The Linux extension of every matrix Python (rust/native.cpython-3XX-x86_64-linux-gnu.so), built in
# the maturin image (manylinux2014, glibc 2.17+) - what CI commits for Linux, and how a Windows or
# macOS checkout gets it. Everything runs inside the container, which also takes the extensions out
# of the wheels and removes them, so no file the container's root owns is left to delete outside it.
build_native_linux:
	rm -f rust/native*-linux-gnu.so
	MSYS_NO_PATHCONV=1 docker run --rm -v "$(CURDIR)":/io -w /io --entrypoint sh ghcr.io/pyo3/maturin -c \
		"maturin build --release --manifest-path rust/native/Cargo.toml --interpreter $(addprefix python,$(matrix_python_versions)) --out rust/native/wheels \
		&& python$(lastword $(matrix_python_versions)) .github/scripts/extract_native_extensions.py rust/native/wheels \
		&& rm -rf rust/native/wheels"

# The interpreters build_native_windows and build_native_macos build for - the .venv-<version> of
# every matrix Python; CI passes the runner's own Pythons.
native_interpreters ?= $(foreach version,$(matrix_python_versions),.venv-$(version)/$(if $(filter Windows_NT,$(OS)),Scripts/python.exe,bin/python))

# The Windows extension of every matrix Python (rust/native.cp3XX-win_amd64.pyd) - what CI commits for
# Windows. Only the Windows files are replaced; the Linux and macOS ones stay. A build `maturin
# develop` left in rust/native/python is removed: on the venv's path it would shadow these for a
# script run outside the repository.
build_native_windows:
	rm -f rust/native*.pyd rust/native/python/rust/native*.pyd
	PYO3_USE_RAW_DYLIB=0 poetry run maturin build --release --manifest-path rust/native/Cargo.toml 		$(foreach interpreter,$(native_interpreters),--interpreter '$(interpreter)') --out rust/native/wheels
	poetry run python .github/scripts/extract_native_extensions.py rust/native/wheels
	rm -rf rust/native/wheels

# The macOS extension of every matrix Python (rust/native.cpython-3XX-darwin.so), one universal2 file
# for Apple silicon and Intel Macs - what CI commits for macOS. Only the macOS files are replaced.
build_native_macos:
	rm -f rust/native*-darwin.so rust/native/python/rust/native*.so
	rustup target add aarch64-apple-darwin x86_64-apple-darwin
	poetry run maturin build --release --target universal2-apple-darwin --manifest-path rust/native/Cargo.toml 		$(foreach interpreter,$(native_interpreters),--interpreter '$(interpreter)') --out rust/native/wheels
	poetry run python .github/scripts/extract_native_extensions.py rust/native/wheels
	rm -rf rust/native/wheels

rust_check:
	cd rust/native && cargo fmt --check
	cd rust/native && cargo clippy --all-targets -- -D warnings
	cd rust/native && PYO3_USE_RAW_DYLIB=0 cargo test --no-default-features

# The whole suite, as test_postgres_asyncpg - the driver decodes every value the ORM reads.
test_postgres_rust:
	$(py_warn) $(test_db_env)="postgresql://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" poetry run pytest $(pytest_postgres_opts) --cov-report=

# test_postgres_rust is not part of _testall/testall/ci: it needs rust.native built (build_native),
# which `make deps` doesn't do. CI runs the suite through both drivers in its test-postgres jobs.
_testall: test_database_independent test_sqlite test_columnar test_postgres_asyncpg
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
