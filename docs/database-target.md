# Database target: the seam, and why it enables nothing

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
| `QUANTBOT_DB_PATH` | SQLite at that path — unchanged |
| `QUANTBOT_DATABASE_URL=sqlite:///path` | the same store, named as a URL |
| `QUANTBOT_DATABASE_URL=postgresql://…` | recognised, and refused with `BACKEND_NOT_ENABLED` |
| **both** | `DATABASE_TARGET_AMBIGUOUS` |

**Both set is a hard error, and no precedence is invented.** A precedence rule means one of the
two settings silently does nothing while the operator who set it believes it took effect — which
is how a system ends up writing to a store nobody meant to use, with both settings present and
looking deliberate.

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

All three entry points refuse a disabled backend **before touching the filesystem**, so a
PostgreSQL URL cannot leave a directory or a file named after itself behind — the quieter form of
the same wrong signal.

## Credentials

A PostgreSQL URL carries a password, and this project prints its configuration into CLI output,
doctor reports, exceptions and the console.

`__str__`, `__repr__`, `display` and every message in the module go through the redactor. `__repr__`
matters as much as `__str__` and is the easier one to forget: a dataclass prints its fields
verbatim, and a target reaches a log line or a traceback through `repr` far more often than
through `str`. One test asserts the property against every renderer at once rather than trusting
each call site to remember.

The username, host, port and database name survive redaction. They are not the secret, and a
diagnostic that removed them would be useless.

## One safety boundary this could have opened

`quantbot-kronos`'s `_database_paths_are_isolated` refuses to write a shadow forecast ledger over
the active trading store. It built its protected list from `QUANTBOT_DB_PATH` alone — so a
`sqlite:///quantbot.db` URL, which leaves that variable unset, would have dropped the active store
out of the list and turned the check green for exactly the collision it exists to refuse.

It now resolves the target, and refuses when the configuration cannot be resolved at all: if the
active store cannot be identified, isolation from it cannot be asserted.

## What this is not

- Not a PostgreSQL connection. There is no driver dependency, no service and no VM.
- Not a schema change. No column types moved.
- Not a change to the single-writer or coordinator authority model.

PostgreSQL joins `ENABLED_BACKENDS` when #197 has proved SQLite/PostgreSQL semantic parity on a
real instance — not when a connection string parses.
