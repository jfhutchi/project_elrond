# Database target: the seam, and why it enables nothing

> **PAPER core storage boundary.** The [PAPER core](paper-core.md) keeps one authoritative operational SQLite store and the existing intent outbox. Schema 36 is an explicit additive migration, not a side effect of a read or normal startup. The [Omen rehearsal](evidence/paper-core/omen-deployment/README.md) migrated restored schema-29 operational/learner copies only; the operating stores remain unmigrated. Legacy executables expect schema 29 at startup, so establish compatible protection/exits before migration. Historical stores, IDs, cost revisions, holdouts and trial charges are preserved; PostgreSQL is not enabled by the new core.

**Issue:** #196 (parent #195) · **Enabled backends:** SQLite only

---

## What changed

Every caller used to say `Path`. That is true of SQLite and only of SQLite, and the assumption
was spread across runtime paths, four CLI commands, the console, the shadow-forecast isolation
check and the deployment scripts.

`DatabaseTarget` names *where the store is* without assuming a filesystem:

```python
DatabaseTarget(backend=DatabaseBackend.SQLITE, location="quantbot.db")
```

`Database` remains the application-facing object and now accepts a path, a string or a target, so
no caller had to be rewritten.

## Configuration

| Setting | Meaning |
|---|---|
| neither | SQLite at `quantbot.db` — unchanged from every prior release |
| `QUANTBOT_DB_PATH` | SQLite at that filesystem path — unchanged for path values |
| `QUANTBOT_DATABASE_URL=sqlite:///path` | the same store, named as a URL |
| `QUANTBOT_DATABASE_URL=postgresql://…` | recognised, and refused with `BACKEND_NOT_ENABLED` |
| **both** | `DATABASE_TARGET_AMBIGUOUS` |

**Both set is a hard error, and no precedence is invented.** A precedence rule means one of the
two settings silently does nothing while the operator who set it believes it took effect — which
is how a system ends up writing to a store nobody meant to use, with both settings present and
looking deliberate.

Explicit command/API arguments use the same coercion rule. A URL-shaped string is parsed as a
URL, never treated as a quirky SQLite filename. This also applies if a URL is accidentally put in
the legacy path variable: a PostgreSQL URL is recognised and refused before filesystem access.
SQLite query parameters and fragments are rejected because this seam does not implement their
URI semantics; silently ignoring `?mode=ro`, for example, would open a writable store after the
operator asked for a read-only one.

## Why PostgreSQL is recognised rather than ignored

A backend with no host, no credentials and no proven schema parity must not become reachable by
setting an environment variable. The way that happens by accident is a **fallback**: point the
system at PostgreSQL, get SQLite, and watch a pristine empty store report itself healthy while
the real one goes untouched.

That is exactly the failure #86 exists to prevent, arriving through the configuration instead of
through a typo. So the URL is parsed, understood, and refused — with a stable code, and naming
what would have to be true first (#197's semantic parity, and a host).

`runtime_paths()` refuses at resolution rather than at the first query, so a disabled backend
stops the process with its own message instead of surfacing later as a path that does not exist.

## The authority model does not move

A target says *where*; it grants nothing.

- ordinary open still does not create (#86);
- ordinary open still does not migrate (#66);
- `create` and `migrate` remain separate named operations;
- a schema mismatch still fails closed.

All three entry points, including direct string arguments, refuse a disabled backend **before
touching the filesystem**, so a PostgreSQL URL cannot leave a directory or a file named after
itself behind — the quieter form of the same wrong signal.

`create-database` creates and seeds the store under a temporary sibling name, closes it, and only
then publishes the completed SQLite file without overwriting any path that appeared concurrently.
If prior-finding seeding fails, no final database or SQLite sidecar remains and the operator can
retry the same command. Schema creation followed by failed application seeding must not leave an
empty-but-apparently-valid store.

## Credentials

A PostgreSQL URL carries a password, and this project prints its configuration into CLI output,
doctor reports, exceptions and the console.

`__str__`, `__repr__`, `display` and every message in the module go through the redactor. It masks
userinfo passwords, credential-shaped query parameters, and the entire fragment. `__repr__`
matters as much as `__str__` and is the easier one to forget: a dataclass prints its fields
verbatim, and a target reaches a log line or a traceback through `repr` far more often than
through `str`. Tests assert the property against every renderer rather than trusting each call
site to remember.

The username, host, port and database name survive redaction. They are not the secret, and a
diagnostic that removed them would be useless.

## One safety boundary this could have opened

`quantbot-kronos`'s `_database_paths_are_isolated` refuses to write a shadow forecast ledger over
the active trading store. It built its protected list from `QUANTBOT_DB_PATH` alone — so a
`sqlite:///quantbot.db` URL, which leaves that variable unset, would have dropped the active store
out of the list and turned the check green for exactly the collision it exists to refuse.

It now resolves the target, requires that its backend is enabled, and refuses when the active
store cannot be identified: isolation from an invalid or disabled target cannot be asserted.

## What this is not

- Not a PostgreSQL connection. There is no driver dependency, no service and no VM.
- Not a schema change. No column types moved.
- Not a change to the single-writer or coordinator authority model.

PostgreSQL joins `ENABLED_BACKENDS` when #197 has proved SQLite/PostgreSQL semantic parity on a
real instance — not when a connection string parses.
