# SQLite/PostgreSQL schema parity (#197)

SQLite remains the only enabled production backend. `DatabaseTarget.require_enabled()` still
refuses PostgreSQL, and the SQLite database lifecycle, WAL configuration, backup path, and
create-versus-open authority are unchanged. This work qualifies the shared schema and Alembic
path using disposable test databases; it does not qualify a PostgreSQL coordinator or cutover.

## Measured defects and corrections

The active-position action index had only `sqlite_where`. PostgreSQL therefore created an
unconditional unique index: a completed action permanently prevented a later action for the
same account/symbol. Both head metadata and revision 0023 now specify the equivalent
`postgresql_where` predicate. Existing SQLite SQL/constraints remain unchanged. This is a
correction to a previously unsupported dialect; it does not repair a manually created old
PostgreSQL database in place. Recreate disposable qualification databases from zero.

PostgreSQL truncates long generated constraint names. Alembic 1.19's named CHECK comparison
reported three constraints as missing/replaced even immediately after creating the head schema.
The migration environment now compares a metadata copy whose names are rendered by SQLAlchemy's
own dialect preparer. Canonical metadata is not modified; names and constraints in existing
SQLite stores are not rewritten.

Alembic no longer defaults to `sqlite:///quantbot.db`. An unspecified target raises before engine
creation. Application-owned supplied connections continue to work. An explicit URL can be
supplied in Alembic configuration or with `-x database_url=<URL>`. Operational environment
variables are not used as an implicit migration target.

## Audit

| Area | Contract and verification |
| --- | --- |
| Migration chain | All revisions 0001 through 0027 run online from zero on both engines. Fresh head creation and migrated head pass schema comparison and Alembic check. |
| Columns, keys, constraints, indexes | Alembic compares columns, keys, names and indexes. Its CHECK comparison does not verify expressions, so a separate guard compares reflected migrated-head CHECK definitions with fresh-head definitions on each backend. A same-name expression mutation proves that guard fails. Runtime tests also exercise FK rejection, RESTRICT, CASCADE, uniqueness, and flag/version constraints. |
| Partial uniqueness | Every active state (PENDING, WORKING, UNCERTAIN, FILLED_UNVERIFIED, FAILED) blocks a second action; COMPLETED releases the account/symbol slot. |
| Exact values | UTC timestamps, prices, quantities, JSON documents and evidence identities retain existing canonical string/text representation. No native time, numeric, JSON, or UUID redesign. |
| Observation identity | Repository replay is idempotent; conflicting bars are refused without overwriting the original. Generated integer bar IDs work on both engines. |
| Research memory | Exact document bytes/hash and Boolean values round-trip; exact replay is accepted and conflicting replacement is refused by the existing application boundary. No database trigger or stronger raw-SQL immutability is claimed. |
| Defaults and flags | Existing quoted text defaults execute on both engines. SQLAlchemy Boolean binding produces Boolean reads; explicit integer flags retain their CHECK constraints. |
| Identifier limits | Dialect-rendered names prevent false drift; migration comparison does not rename canonical metadata. |
| Raw SQL | The migration chain's schema-version updates and revision-0027 lineage update execute on both engines. SQLite PRAGMAs, sqlite_master inspection and online backup are intentionally confined to the production SQLite lifecycle. |

## Run locally

Install the optional test driver:

```console
uv sync --frozen --group dev --group postgres
```

Point `QUANTBOT_TEST_POSTGRES_ADMIN_URL` at a **disposable** PostgreSQL test server whose user can
create databases, for example `postgresql+psycopg://elrond_test@127.0.0.1:55432/postgres`.
Run:

```console
uv run --frozen --group postgres pytest -q tests/integration/test_storage_parity.py
```

The fixture creates a random `elrond_test_<uuid>` database and only migrates/drops that database.
It strips any libpq `dbname` query override from the generated test URL and verifies
`current_database()` equals the newly created name before exposing the connection to tests.
It does not migrate or clear the admin database. Without the test URL, PostgreSQL cases are
explicitly skipped; SQLite cases still run. Ordinary `QUANTBOT_DATABASE_URL` is never used by
this fixture. The manual-only `Storage parity` workflow supplies a disposable PostgreSQL 16
service and this same command. Adding the workflow does not dispatch it or enable automatic CI.

These tests prove fresh creation and replay from zero. They do not establish preservation and
backfill correctness for every possible populated historical database, or replace a cutover
rehearsal against a backup of the selected authoritative store under #199.

## Alembic SQL generation

PostgreSQL offline generation of the complete chain is tested without connecting:

```console
uv run alembic -x database_url=postgresql://localhost/unused_offline upgrade head --sql
```

The output contains generated store-lineage identity values; do not reuse one rendered script
to create unrelated stores. Prefer online migration through the explicit application operation.
SQLite revision 0027 needs reflected table state for its batch alterations, so a complete SQLite
offline script is not supported; use the tested online chain. That limitation grants no authority
to run migrations against an unverified operator store.

References: [SQLAlchemy partial indexes](https://docs.sqlalchemy.org/en/20/dialects/postgresql.html#partial-indexes)
and [Alembic supplied connections](https://alembic.sqlalchemy.org/en/latest/cookbook.html#sharing-a-connection-across-one-or-more-programmatic-migration-commands).
