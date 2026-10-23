checkfiles = hare/ tests/ conftest.py mkdocs_hooks/
# The tests of the static types (assert_type) are checked too - at run time assert_type checks nothing.
mypy_checkfiles = hare/ conftest.py mkdocs_hooks/ tests/queries/values/test_values_list_types.py tests/queries/retrieval/test_get_exceptions.py
py_warn = PYTHONDEVMODE=1
pytest_opts = -n auto --tb=native -q --durations=25 --verify-plans
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

# The test servers keep their data in memory (tmpfs) - a test database lives for one run, and written to
# Docker's virtual machine it grew the machine's disk for good (the disk never shrinks). A server started
# again begins empty. Postgres 18 keeps its data under /var/lib/postgresql (and refuses a mount at its
# data/), an older one under data/ - test_db_data_dir names it for a version. The write-ahead log is kept
# short: every CREATE DATABASE of a parallel run writes its template to it, and at its default size the
# log alone filled the memory disk.
test_db_data_tmpfs_options = rw,size=6g
test_db_wal_options = -c max_wal_size=256MB -c min_wal_size=64MB
test_db_data_tmpfs = --tmpfs /var/lib/postgresql:$(test_db_data_tmpfs_options)
test_db_data_dir = $$(if [ $$version -ge 18 ]; then echo /var/lib/postgresql; else echo /var/lib/postgresql/data; fi)
clickhouse_data_tmpfs = --tmpfs /var/lib/clickhouse:rw,size=3g --tmpfs /var/log/clickhouse-server:rw,size=256m
clickhouse_node_data_tmpfs = --tmpfs /var/lib/clickhouse:rw,size=1g --tmpfs /var/log/clickhouse-server:rw,size=128m

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
	@echo  "    test_fast          Runs the test suite against sqlite (no plan verification)"
	@echo  "    test_sqlite        Runs the test suite against sqlite (without the database-independent tests)"
	@echo  "    test_database_independent  Runs the tests reading no test database (mypy plugin, stubs, scans)"
	@echo  "    test_timing        Runs the tests whose outcome depends on time, left out of every other suite"
	@echo  "    test_sqlite_regexp Runs the regexp tests against sqlite with install_regexp_functions"
	@echo  "    test_poetry_add    Adds hare-orm from this checkout to a new poetry project"
	@echo  "    test_db_up         Builds and starts the test Postgres server (docker, port HARE_POSTGRES_PORT)"
	@echo  "    test_db_down       Stops and removes the test Postgres server"
	@echo  "    test_brokers_up    Starts the test Redis, RabbitMQ and Kafka (docker, ports 6390/5673/9095)"
	@echo  "    test_brokers_down  Stops and removes the test brokers"
	@echo  "    test_brokers       Runs the outbox wakeup and delivery tests against the test brokers"
	@echo  "    test_postgres_asyncpg  Runs the test suite against Postgres via asyncpg"
	@echo  "    test_db_matrix_up  Builds and starts a test Postgres server per matrix version (ports 5414-5418)"
	@echo  "    test_db_matrix_down Stops and removes the matrix Postgres servers"
	@echo  "    test_pgbouncer_matrix_up / test_pgbouncer_matrix_down  Starts / removes a PgBouncer per matrix version in front of each matrix Postgres (ports 1BBPP)"
	@echo  "    test_clickhouse_matrix_up / test_clickhouse_matrix_down  Starts / removes a ClickHouse server per matrix version (ports 3YYMM, 4YYMM, 6YYMM)"
	@echo  "    test_clickhouse_cluster_matrix_up / test_clickhouse_cluster_matrix_down  Starts / removes a ClickHouse cluster per matrix version (ports 5YYMM, 2YYMM)"
	@echo  "    test_brokers_matrix_up / test_brokers_matrix_down  Starts / removes Redis, RabbitMQ and Kafka of each matrix version (ports 16XYY, 15XYY, 19XYY)"
	@echo  "    matrix_venvs       Creates .venv-3.12/.venv-3.13/.venv-3.14 with every dependency group"
	@echo  "    sqlite_versions    Downloads and prepares the matrix SQLite versions for each matrix Python"
	@echo  "    spatialite         Downloads SpatiaLite for the SpatiaLite tests (Windows) and prints their variables"
	@echo  "    test_clickhouse_up / test_clickhouse_down  Starts / removes the ClickHouse test server (ports 8124, 9124)"
	@echo  "    test_clickhouse    Runs the ClickHouse dialect's tests over HTTP and over the native protocol"
	@echo  "    test_clickhouse_cluster_up / test_clickhouse_cluster_down  Starts / removes the two-server ClickHouse cluster (ports 8126-8127, 9126-9127)"
	@echo  "    test_clickhouse_cluster  Runs the ClickHouse cluster tests over HTTP and over the native protocol"
	@echo  "    test_matrix        Runs every suite on every Python x server version of the matrix, prints a table"
	@echo  "    lowest_dependencies_venv  Creates .venv-lowest: Python LOWEST_DEPENDENCIES_PYTHON (3.12), hare's direct dependencies at their lowest versions"
	@echo  "    test_lowest_dependencies  Runs the suite in .venv-lowest on SQLite and Postgres via both drivers"
	@echo  "    build_native       Builds rust.native (the rust_pg driver, row codecs) for this OS into rust/ (needs a Rust toolchain)"
	@echo  "    build_native_linux Builds the Linux rust.native of every matrix Python into rust/ in Docker"
	@echo  "    build_native_windows Builds the Windows rust.native of every matrix Python (.venv-3.X) into rust/"
	@echo  "    build_native_macos Builds the macOS rust.native (universal2) of every matrix Python (.venv-3.X) into rust/"
	@echo  "    rust_check         Runs cargo fmt --check, clippy and the Rust unit tests"
	@echo  "    test_postgres_rust Runs the test suite against Postgres via the rust_pg driver"
	@echo  "    testall / ci       Runs check + test_database_independent, test_sqlite, test_postgres_asyncpg"
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
	"$(MAKE)" _docs

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
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest $(pytest_database_opts)

# The tests reading no test database - once, whatever databases the other suites run on.
test_database_independent:
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest $(pytest_opts) -m database_independent

# The tests whose outcome depends on time (the timing marker: a timeout, a lease, a delay running out) -
# left out of every other suite, run on their own: HARE_TEST_DB picks the database, SQLite by default.
test_timing:
	$(py_warn) HARE_TEST_DB=$${HARE_TEST_DB:-sqlite+aiosqlite://:memory:} poetry run pytest $(pytest_opts) --run-timing -m timing

# Scoped to the only tests whose outcome actually depends on install_regexp_functions - the
# whole suite behaves identically with or without it otherwise, so running every test a second
# time here just to exercise this one query param buys nothing.
test_sqlite_regexp:
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory:?install_regexp_functions=True poetry run pytest $(pytest_opts) tests/queries/filtering/test_posix_regex_filter.py tests/backends/test_sqlite_client.py

# A new poetry project adding hare-orm from this checkout as its dependency (tests/package/test_version.py).
test_poetry_add:
	$(py_warn) HARE_TEST_POETRY_ADD=1 poetry run pytest -p no:cacheprovider --tb=native -q tests/package/test_version.py::test_added_by_poetry_v2

test_db_up:
	docker build -t $(test_db_image) tests/docker/postgres
	docker start $(test_db_container) 2>/dev/null || docker run -d --name $(test_db_container) --restart unless-stopped $(test_db_data_tmpfs) 		-p $(HARE_POSTGRES_PORT):5432 -e POSTGRES_USER=$(HARE_POSTGRES_USER) -e POSTGRES_PASSWORD=$(HARE_POSTGRES_PASS) 		$(test_db_image) -c fsync=off -c synchronous_commit=off -c full_page_writes=off 		-c max_prepared_transactions=64 -c max_connections=400 -c shared_buffers=512MB $(test_db_wal_options)
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
	$(py_warn) $(test_brokers_env) HARE_TEST_DB=sqlite+aiosqlite://:memory: poetry run pytest -p no:cacheprovider --tb=native -q \
		tests/contrib/outbox/test_brokers.py

# PgBouncer in transaction pooling in front of the test database (tests/docker/pgbouncer) on port
# 6432, with prepared statements across transactions (PgBouncer 1.22.1+, the oldest hare supports).
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
	$(py_warn) $(test_db_env)="postgresql+asyncpg://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):6432/test_\{\}?$(pgbouncer_options)" poetry run pytest $(pytest_postgres_opts)
	$(py_warn) $(test_db_env)="postgresql://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):6432/test_\{\}?$(pgbouncer_options)" poetry run pytest $(pytest_postgres_opts)

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
	docker start $(clickhouse_container) 2>/dev/null || docker run -d --name $(clickhouse_container) $(clickhouse_data_tmpfs) 		--restart unless-stopped -p 8124:8123 -p 9124:9000 -p 9182:9181 -e CLICKHOUSE_PASSWORD=$(HARE_CLICKHOUSE_PASS) 		--ulimit nofile=262144:262144 $(clickhouse_image)
	until docker exec $(clickhouse_container) clickhouse-client --password $(HARE_CLICKHOUSE_PASS) -q "SELECT 1" >/dev/null 2>&1; do sleep 1; done

test_clickhouse_down:
	docker rm -f $(clickhouse_container)

# Over both drivers: clickhouse-connect on the HTTP interface, clickhouse-driver on the native protocol.
test_clickhouse:
	$(py_warn) $(test_db_env)="clickhouse+clickhouse-connect://default:$(HARE_CLICKHOUSE_PASS)@127.0.0.1:8124/test_\{\}" poetry run pytest $(pytest_postgres_opts) tests/dialects/clickhouse tests/fields/value_types/test_ordered_values.py tests/fields/value_types/test_ip_address_text_lookups.py tests/fields/value_types/test_ip_address_zones.py
	$(py_warn) $(test_db_env)="clickhouse+clickhouse-driver://default:$(HARE_CLICKHOUSE_PASS)@127.0.0.1:9124/test_\{\}" poetry run pytest $(pytest_postgres_opts) tests/dialects/clickhouse tests/fields/value_types/test_ordered_values.py tests/fields/value_types/test_ip_address_text_lookups.py tests/fields/value_types/test_ip_address_zones.py

# The ClickHouse test cluster (tests/docker/clickhouse_cluster): two servers on a network of their own,
# one shard of two replicas (hare_replicated) and two shards (hare_sharded), the first server the
# cluster's Keeper. Their HTTP interfaces are on ports 8126 and 8127, their native protocol on 9126 and
# 9127. test_clickhouse_cluster runs the cluster tests through the first server.
clickhouse_cluster_network = hare-orm-test-clickhouse-cluster
clickhouse_cluster_nodes = 1 2

test_clickhouse_cluster_up:
	docker network create $(clickhouse_cluster_network) 2>/dev/null || true
	for node in $(clickhouse_cluster_nodes); do 		docker build -t hare-orm-test-clickhouse-node$$node:25.8 --build-arg CLICKHOUSE_VERSION=25.8 --build-arg NODE=node$$node tests/docker/clickhouse_cluster && 		(docker start hare-orm-test-clickhouse-node$$node 2>/dev/null || docker run -d --name hare-orm-test-clickhouse-node$$node 			--hostname hare-orm-test-clickhouse-node$$node --network $(clickhouse_cluster_network) --restart unless-stopped $(clickhouse_node_data_tmpfs) 			-p 812$$((5 + node)):8123 -p 912$$((5 + node)):9000 -e CLICKHOUSE_PASSWORD=$(HARE_CLICKHOUSE_PASS) 			--ulimit nofile=262144:262144 hare-orm-test-clickhouse-node$$node:25.8); 	done
	until docker exec hare-orm-test-clickhouse-node2 clickhouse-client --password $(HARE_CLICKHOUSE_PASS) -q "SELECT count() FROM system.zookeeper WHERE path = '/'" >/dev/null 2>&1; do sleep 1; done

test_clickhouse_cluster_down:
	for node in $(clickhouse_cluster_nodes); do docker rm -f hare-orm-test-clickhouse-node$$node; done
	docker network rm $(clickhouse_cluster_network)

test_clickhouse_cluster:
	$(py_warn) HARE_TEST_CLICKHOUSE_CLUSTER_DB="clickhouse+clickhouse-connect://default:$(HARE_CLICKHOUSE_PASS)@127.0.0.1:8126/test_cluster_\{\}?cluster=hare_sharded" poetry run pytest $(pytest_postgres_opts) tests/dialects/clickhouse/cluster
	$(py_warn) HARE_TEST_CLICKHOUSE_CLUSTER_DB="clickhouse+clickhouse-driver://default:$(HARE_CLICKHOUSE_PASS)@127.0.0.1:9126/test_cluster_\{\}?cluster=hare_sharded" poetry run pytest $(pytest_postgres_opts) tests/dialects/clickhouse/cluster

# The test matrix (tests/matrix): every Python of matrix_python_versions (each its own
# .venv-<version>), every SQLite of tests/sqlite_versions, every PostgreSQL of matrix_postgres_versions
# (each its own server, hare-orm-test-db-<version> on port 54<version>), every PgBouncer of
# matrix_pgbouncer_versions (in the pairs of matrix_pgbouncer_pairs), every ClickHouse of
# matrix_clickhouse_versions (a server and a cluster each) and the oldest and newest Redis, RabbitMQ and
# Kafka hare supports. Each server's port is made of its version's digits - tests/matrix/matrix_ports.py.
#
# Each CI test job runs one part of it through these same targets, the variables narrowed on the
# command line: `make matrix_venvs matrix_python_versions=3.13 matrix_interpreter=<its python>`,
# `make sqlite_versions matrix_python_versions=3.13`, `make test_db_matrix_up
# matrix_postgres_versions=16`, `make test_matrix MATRIX_OPTIONS="--python 3.13 --suite asyncpg-16"`.
matrix_python_versions = 3.12 3.13 3.14
matrix_postgres_versions = 14 15 16 17 18
# Each minor version from the oldest hare supports.
matrix_pgbouncer_versions = 1.22 1.23 1.24 1.25 1.26
# The PgBouncer image of each version, pinned: the newest patch release of the minor version.
matrix_pgbouncer_images = \
	1.22=edoburu/pgbouncer:1.22.1-p0@sha256:7f39cf2f872c8d9a0bb0e6a5440c3bf5814036eccf284102870ab08e2603a2c7 \
	1.23=edoburu/pgbouncer:v1.23.1-p3@sha256:377dec3c0e4a66a1077ec043e16a26ed5702a6d954011a7983a1457c2e070b1d \
	1.24=edoburu/pgbouncer:v1.24.1-p1@sha256:3db3d7223e93af52b4116f642951a1a5fa44702a88c2a59cf7562cac19320c9e \
	1.25=edoburu/pgbouncer:v1.25.2-p0@sha256:7d7a27d9e90985cab5cf42256f5c13a3120baa4b055b69df37beb272b89b2340 \
	1.26=edoburu/pgbouncer:v1.26.1-p0@sha256:488422e38cff912895d0d65d8963fcd306e6ac589a724398b6eb8e294e94d624
# Each version with a server image from the oldest hare supports.
matrix_clickhouse_versions = 25.8 25.10 25.12 26.3 26.7 26.8 26.9
# The oldest version of each broker hare supports and the newest one.
matrix_redis_versions = 7.2 8.10
matrix_rabbitmq_versions = 4.0 4.3
matrix_kafka_versions = 3.9 4.3
# Kafka's images are tagged by patch release only - the newest one of each minor version.
matrix_kafka_images = 3.9=apache/kafka:3.9.2 4.3=apache/kafka:4.3.1
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
			--restart unless-stopped --tmpfs $(test_db_data_dir):$(test_db_data_tmpfs_options) -p 54$$version:5432 -e POSTGRES_USER=$(HARE_POSTGRES_USER) \
			-e POSTGRES_PASSWORD=$(HARE_POSTGRES_PASS) hare-orm-test-postgres:$$version -c fsync=off \
			-c synchronous_commit=off -c full_page_writes=off -c max_prepared_transactions=64 \
			-c max_connections=400 -c shared_buffers=256MB $(test_db_wal_options) || exit 1; \
	done
	for version in $(matrix_postgres_versions); do \
		until docker exec hare-orm-test-db-$$version pg_isready -q -h 127.0.0.1; do sleep 1; done; \
	done

test_db_matrix_down:
	for version in $(matrix_postgres_versions); do docker rm -f hare-orm-test-db-$$version; done

# The PgBouncer/PostgreSQL pairs of the matrix: each PgBouncer in front of the newest PostgreSQL, and
# the newest PgBouncer in front of the oldest one - tests/matrix/matrix_runner.py runs the same pairs.
matrix_pgbouncer_pairs = $(sort $(foreach version,$(matrix_pgbouncer_versions),$(version)/$(lastword \
	$(matrix_postgres_versions))) $(lastword $(matrix_pgbouncer_versions))/$(firstword $(matrix_postgres_versions)))

# A PgBouncer of each pair in front of its PostgreSQL server (test_db_matrix_up first):
# hare-orm-test-pgbouncer-<pgbouncer>-<postgres> on port 1<pgbouncer minor><postgres> - 12218 for 1.22
# in front of PostgreSQL 18.
test_pgbouncer_matrix_up:
	for pair in $(matrix_pgbouncer_pairs); do \
		version=$${pair%/*}; postgres=$${pair#*/}; \
		image=$$(for entry in $(matrix_pgbouncer_images); do [ "$${entry%%=*}" = "$$version" ] && echo "$${entry#*=}"; done); \
		[ -n "$$image" ] || { echo "No PgBouncer image for $$version in matrix_pgbouncer_images"; exit 1; }; \
		docker build -t hare-orm-test-pgbouncer:$$version --build-arg PGBOUNCER_IMAGE=$$image tests/docker/pgbouncer || exit 1; \
		port=1$$(printf %02d $${version#*.})$$postgres; \
		docker start hare-orm-test-pgbouncer-$$version-$$postgres 2>/dev/null || docker run -d \
			--name hare-orm-test-pgbouncer-$$version-$$postgres --add-host=host.docker.internal:host-gateway \
			-p $$port:6432 -e PGBOUNCER_SERVER_PORT=54$$postgres hare-orm-test-pgbouncer:$$version || exit 1; \
	done

test_pgbouncer_matrix_down:
	for pair in $(matrix_pgbouncer_pairs); do docker rm -f hare-orm-test-pgbouncer-$${pair%/*}-$${pair#*/}; done

# A ClickHouse test server of each matrix version: hare-orm-test-clickhouse-<version>, its HTTP interface
# on 3YYMM, its native protocol on 4YYMM and its Keeper on 6YYMM - 32508, 42508, 62508 for 25.8.
test_clickhouse_matrix_up:
	for version in $(matrix_clickhouse_versions); do \
		digits=$$(printf %02d%02d $${version%%.*} $${version#*.}); \
		docker build -t hare-orm-test-clickhouse:$$version --build-arg CLICKHOUSE_VERSION=$$version tests/docker/clickhouse || exit 1; \
		docker start hare-orm-test-clickhouse-$$version 2>/dev/null || docker run -d --name hare-orm-test-clickhouse-$$version \
			$(clickhouse_data_tmpfs) -p 3$$digits:8123 -p 4$$digits:9000 -p 6$$digits:9181 \
			-e CLICKHOUSE_PASSWORD=$(HARE_CLICKHOUSE_PASS) --ulimit nofile=262144:262144 hare-orm-test-clickhouse:$$version || exit 1; \
	done
	for version in $(matrix_clickhouse_versions); do \
		until docker exec hare-orm-test-clickhouse-$$version clickhouse-client --password $(HARE_CLICKHOUSE_PASS) -q "SELECT 1" >/dev/null 2>&1; do sleep 1; done; \
	done

test_clickhouse_matrix_down:
	for version in $(matrix_clickhouse_versions); do docker rm -f hare-orm-test-clickhouse-$$version; done

# The ClickHouse test cluster of each matrix version: two servers on a network of their own,
# hare-orm-test-clickhouse-cluster-<version>, each known there by the name the cluster's configuration
# gives it; the first one's HTTP interface on 5YYMM, its native protocol on 2YYMM - 52508, 22508 for 25.8.
test_clickhouse_cluster_matrix_up:
	for version in $(matrix_clickhouse_versions); do \
		digits=$$(printf %02d%02d $${version%%.*} $${version#*.}); \
		docker network create hare-orm-test-clickhouse-cluster-$$version 2>/dev/null || true; \
		for node in $(clickhouse_cluster_nodes); do \
			docker build -t hare-orm-test-clickhouse-node$$node:$$version --build-arg CLICKHOUSE_VERSION=$$version \
				--build-arg NODE=node$$node tests/docker/clickhouse_cluster || exit 1; \
			if [ $$node = 1 ]; then ports="-p 5$$digits:8123 -p 2$$digits:9000"; else ports=; fi; \
			docker start hare-orm-test-clickhouse-$$version-node$$node 2>/dev/null || docker run -d \
				--name hare-orm-test-clickhouse-$$version-node$$node --hostname hare-orm-test-clickhouse-node$$node \
				--network hare-orm-test-clickhouse-cluster-$$version --network-alias hare-orm-test-clickhouse-node$$node \
				$(clickhouse_node_data_tmpfs) $$ports -e CLICKHOUSE_PASSWORD=$(HARE_CLICKHOUSE_PASS) \
				--ulimit nofile=262144:262144 hare-orm-test-clickhouse-node$$node:$$version || exit 1; \
		done; \
	done
	for version in $(matrix_clickhouse_versions); do \
		until docker exec hare-orm-test-clickhouse-$$version-node2 clickhouse-client --password $(HARE_CLICKHOUSE_PASS) \
			-q "SELECT count() FROM system.zookeeper WHERE path = '/'" >/dev/null 2>&1; do sleep 1; done; \
	done

test_clickhouse_cluster_matrix_down:
	for version in $(matrix_clickhouse_versions); do \
		for node in $(clickhouse_cluster_nodes); do docker rm -f hare-orm-test-clickhouse-$$version-node$$node; done; \
		docker network rm hare-orm-test-clickhouse-cluster-$$version; \
	done

# The brokers of each matrix version, their data in memory: Redis on 16XYY, RabbitMQ on 15XYY, Kafka
# (one KRaft node) on 19XYY - 16810 for Redis 8.10 - hare-orm-test-redis-<version>,
# hare-orm-test-rabbitmq-<version>, hare-orm-test-kafka-<version>.
test_brokers_matrix_up:
	for version in $(matrix_redis_versions); do \
		port=16$${version%%.*}$$(printf %02d $${version#*.}); \
		docker start hare-orm-test-redis-$$version 2>/dev/null || docker run -d --name hare-orm-test-redis-$$version \
			--tmpfs /data:rw,size=256m -p $$port:6379 redis:$$version-alpine || exit 1; \
	done
	for version in $(matrix_rabbitmq_versions); do \
		port=15$${version%%.*}$$(printf %02d $${version#*.}); \
		docker start hare-orm-test-rabbitmq-$$version 2>/dev/null || docker run -d --name hare-orm-test-rabbitmq-$$version \
			--tmpfs /var/lib/rabbitmq:rw,size=512m -p $$port:5672 rabbitmq:$$version-management-alpine || exit 1; \
	done
	for version in $(matrix_kafka_versions); do \
		image=$$(for entry in $(matrix_kafka_images); do [ "$${entry%%=*}" = "$$version" ] && echo "$${entry#*=}"; done); \
		[ -n "$$image" ] || { echo "No Kafka image for $$version in matrix_kafka_images"; exit 1; }; \
		port=19$${version%%.*}$$(printf %02d $${version#*.}); \
		docker start hare-orm-test-kafka-$$version 2>/dev/null || docker run -d --name hare-orm-test-kafka-$$version \
			--tmpfs /tmp/kraft-combined-logs:rw,size=1g -p $$port:$$port \
			-e KAFKA_NODE_ID=1 -e KAFKA_PROCESS_ROLES=broker,controller \
			-e KAFKA_LISTENERS=PLAINTEXT://0.0.0.0:$$port,CONTROLLER://0.0.0.0:9093 \
			-e KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://localhost:$$port \
			-e KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER \
			-e KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT \
			-e KAFKA_CONTROLLER_QUORUM_VOTERS=1@localhost:9093 \
			-e KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 -e KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR=1 \
			-e KAFKA_TRANSACTION_STATE_LOG_MIN_ISR=1 -e KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS=0 \
			$$image || exit 1; \
	done
	for version in $(matrix_rabbitmq_versions); do \
		until docker exec hare-orm-test-rabbitmq-$$version rabbitmq-diagnostics -q check_port_connectivity >/dev/null 2>&1; do sleep 1; done; \
	done

test_brokers_matrix_down:
	for version in $(matrix_redis_versions); do docker rm -f hare-orm-test-redis-$$version; done
	for version in $(matrix_rabbitmq_versions); do docker rm -f hare-orm-test-rabbitmq-$$version; done
	for version in $(matrix_kafka_versions); do docker rm -f hare-orm-test-kafka-$$version; done

# One virtualenv per Python of the matrix, with every dependency group - `py -X.Y` on Windows,
# `pythonX.Y` elsewhere.
matrix_venvs:
	for version in $(matrix_python_versions); do \
		if [ -n "$(matrix_interpreter)" ]; then "$(matrix_interpreter)" -m venv .venv-$$version; \
		elif command -v py >/dev/null 2>&1; then py -$$version -m venv .venv-$$version; \
		else python$$version -m venv .venv-$$version; fi || exit 1; \
		VIRTUAL_ENV="$(CURDIR)/.venv-$$version" poetry install --all-groups --extras asyncpg --extras opentelemetry \
			--extras clickhouse-connect --extras clickhouse-driver || exit 1; \
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
# their declared bounds allow, on Python LOWEST_DEPENDENCIES_PYTHON (each matrix Python in CI), resolved
# by uv (`--resolution lowest-direct`) into their own virtualenv; the test tools at their poetry.lock
# versions (tests/lowest_dependencies/locked_tool_requirements.py). LOWEST_DEPENDENCIES_VENV puts the
# virtualenv elsewhere.
LOWEST_DEPENDENCIES_PYTHON ?= 3.12
LOWEST_DEPENDENCIES_VENV ?= .venv-lowest
lowest_dependencies_python = $(LOWEST_DEPENDENCIES_VENV)/$(if $(filter Windows_NT,$(OS)),Scripts/python.exe,bin/python)

lowest_dependencies_venv:
	rm -rf "$(LOWEST_DEPENDENCIES_VENV)"
	uv venv --python $(LOWEST_DEPENDENCIES_PYTHON) "$(LOWEST_DEPENDENCIES_VENV)"
	"$(lowest_dependencies_python)" -m tests.lowest_dependencies.locked_tool_requirements > "$(LOWEST_DEPENDENCIES_VENV)/test-tools.in"
	uv pip compile pyproject.toml "$(LOWEST_DEPENDENCIES_VENV)/test-tools.in" --all-extras --resolution lowest-direct \
		--python "$(lowest_dependencies_python)" --output-file "$(LOWEST_DEPENDENCIES_VENV)/requirements.txt"
	uv pip install --python "$(lowest_dependencies_python)" -r "$(LOWEST_DEPENDENCIES_VENV)/requirements.txt"
	uv pip install --python "$(lowest_dependencies_python)" --no-deps -e .

# The whole suite in that virtualenv, on SQLite and on the test Postgres server (test_db_up) via both
# drivers - rust_pg needs rust.native for Python 3.12 in rust/.
test_lowest_dependencies:
	$(py_warn) HARE_TEST_DB=sqlite+aiosqlite://:memory: "$(lowest_dependencies_python)" -m pytest -n auto --tb=native -q
	$(py_warn) $(test_db_env)="postgresql+asyncpg://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" "$(lowest_dependencies_python)" -m pytest -n $(HARE_POSTGRES_WORKERS) --tb=native -q -m "not database_independent"
	$(py_warn) $(test_db_env)="postgresql://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" "$(lowest_dependencies_python)" -m pytest -n $(HARE_POSTGRES_WORKERS) --tb=native -q -m "not database_independent"

test_postgres_asyncpg:
	$(py_warn) $(test_db_env)="postgresql+asyncpg://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" poetry run pytest $(pytest_postgres_opts)

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
	$(py_warn) $(test_db_env)="postgresql://$(HARE_POSTGRES_USER):$(HARE_POSTGRES_PASS)@$(HARE_POSTGRES_HOST):$(HARE_POSTGRES_PORT)/test_\{\}" poetry run pytest $(pytest_postgres_opts)

# test_postgres_rust is not part of _testall/testall/ci: it needs rust.native built (build_native),
# which `make deps` doesn't do. CI runs the suite through both drivers in its test-postgres jobs.
_testall: test_database_independent test_sqlite test_postgres_asyncpg

testall: deps _testall

ci: check _testall

docs: deps _docs
# The documentation built in strict mode - a broken link or a docs style warning fails it.
_docs:
	poetry run mkdocs build --strict

build: deps
	rm -fR dist/
	poetry build
	poetry run twine check dist/*

publish: build
	poetry run twine upload dist/*
