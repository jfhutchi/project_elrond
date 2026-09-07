# Storage dialect parity (#197)

**Goal:** Exercise the authoritative schema and all migrations on SQLite and disposable PostgreSQL
without enabling a PostgreSQL runtime or changing canonical evidence representations.

**Architecture:** Use SQLAlchemy engines and supplied Alembic connections in tests. PostgreSQL
uses a separate optional dependency group and uniquely named test databases, never the configured
operational store. The normal SQLite `Database` authority gate remains unchanged.

- [x] Read #197, inspect schema, migration environment, existing SQLite tests and backend guard.
- [x] Add a four-way fresh-head/migration-chain and SQLite/PostgreSQL test matrix. Check reflected
  schema parity, migration head, canonical bar round trips/idempotency, immutable observations,
  Boolean/check/FK/cascade behavior, and active-position partial uniqueness.
- [x] Observe failures on the unmodified schema, then correct dialect assumptions in head and
  the historical migration that introduced them. Preserve emitted SQLite schema semantics.
- [x] Require explicit Alembic connection/URL selection rather than an implicit operator DB path;
  verify configured online and offline behavior and document any offline limitation.
- [x] Document the schema audit, exact test commands and production-disabled boundary; provide
  explicit manual CI support without enabling automatic hosted runs.
- [x] Run both real engines, existing SQLite migration tests, Ruff, both mypy targets and the full
  isolated SQLite suite. Obtain independent review, address findings.

Design choice: retain canonical TEXT timestamps/decimals/documents and string identifiers.
Do not introduce native money/JSON/time types, a new database service architecture, or an
operational cutover. SQLite PRAGMAs and backup logic are intentional backend-specific runtime
code; this issue covers the shared authoritative SQLAlchemy/Alembic schema path.


- [ ] Publish the verified branch as a draft PR.

## Verification

- Repository suite: **2,725 passed, 6 skipped**, 408.13s. This run collected the initial 48 parity cases before the last 10 review regressions were added; production code was unchanged afterward.
- Final real SQLite/PostgreSQL matrix after review fixes: **58 passed** in 11.72s, including a connection URL containing `?dbname=postgres`.
- Earlier focused storage/migration checks plus the initial matrix: **79 passed**.
- Ruff and both mypy targets passed (226 source files each); diff checks passed.
- Reviewer independently ran 8 final regression/SQLite cases: passed.
- Six full-suite skips require live model endpoints or Windows symlink privileges; one existing Starlette warning remains. WSL integration passed.
- Workflow YAML verified as manual-only, with read-only repository permissions and no repository secrets. Hosted execution was not requested.

Independent review identified the libpq target override and CHECK-expression coverage gaps.
Both are corrected and regression-tested; follow-up review found no actionable issues.
The disposable test cluster was stopped with no remaining generated databases; the existing
PostgreSQL 16 main cluster remains online. No operational store was modified.

