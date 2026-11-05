# PostgreSQL: testing

What the test tools of hare do on PostgreSQL beyond what [Test setup](../../testing/setup.md) describes
for every database.

## <a id="reuse-databases"></a>Reusable test databases: `reuse_databases`

Every `DROP DATABASE` makes PostgreSQL request a checkpoint, and on a server with `fsync` on that costs
seconds under a parallel test run — more than creating and filling the database. With
`reuse_databases=True`, a PostgreSQL `db_url` whose database name contains the `{}` placeholder takes a
database from a per-process pool (`ReusableTestDatabases`) instead of a new random name:

- the placeholder is filled with a slot id derived from a random run namespace, process id, worker and
  slot number. Names stay stable within a process; independent runs never share slots;
- on entering, the slot's database is created when it doesn't exist yet, reset when it is left over from
  an interrupted use in this process, and used as is when this process has already reset it;
- on exit, instead of `DROP DATABASE`, the database is reset and the slot goes back to the pool — also
  when the block raises or the reset itself fails (the database is then reset before its next use);
- a context entered while every slot is taken (nested contexts, several connections in one config) gets a
  new slot, so the pool grows to the largest number of databases used at the same time.

The reset brings the database back to the state of a freshly created one without dropping it: it ends the
other sessions connected to it (`pg_terminate_backend`), rolls back its prepared transactions, drops
every schema except the system ones (`DROP SCHEMA ... CASCADE`, which also drops the extensions installed
into them — `vector`, `postgis`, `citext`, `btree_gist`, ...), recreates `public` with the owner,
privileges and comment a new database gets, and clears database-level settings
(`ALTER DATABASE ... RESET ALL`, and `ALTER ROLE ... IN DATABASE ... RESET ALL` for every role that has
some). None of these statements requests a checkpoint. Objects that don't live in a schema — large
objects, event triggers, publications — are not removed.

Behind a [transaction pooler](../../connections/connection-poolers.md) the reset runs on the server
itself (`direct_host`) and ends the pooler's server sessions on the database too. PgBouncer notices a
session ended only when it lends it, so a test's first statement would fail on it: set
`server_check_delay = 0` in the PgBouncer of the tests, and it checks each server connection before
lending it and replaces one the reset ended.

`reuse_databases=None` (the default) reads the `HARE_TEST_REUSE_DATABASES` environment variable:
`1`/`true` turns the pool on, `0`/`false` or an unset variable leaves it off; any other value raises
`ConfigurationError`. An explicit `True`/`False` argument wins over the variable. SQLite URLs, and
PostgreSQL URLs without `{}`, are never pooled.

```bash
HARE_TEST_REUSE_DATABASES=1 pytest -n 8
```

The pool retains databases after release. Test-run infrastructure must clean up its completed run's
databases; a later independent run uses a different namespace. A test checking the database lifecycle —
`CREATE DATABASE` refusing
an existing name, `DROP DATABASE` with an active connection — should pass `reuse_databases=False`.

Lease bookkeeping lives in `hare.contrib.test`, not in production clients or connection credentials.
Each scope retains its exact leases, identified by endpoint and database with an opaque acquisition
token. Releasing a stale token cannot release another scope's slot. Taking a retained schema also
consumes the clean marker atomically, before any database work starts.
