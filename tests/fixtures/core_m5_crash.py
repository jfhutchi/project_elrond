"""Test child dies at real SQLite transaction edges; no production fault hooks."""

import os
import sys

from sqlalchemy import event

from quantbot.paper_core.activation import ActivationAuthority, activate_model
from quantbot.paper_core.contracts import CoreConfig
from quantbot.paper_core.store import CoreStore
from quantbot.storage import Database

path, config_json, authority_json, parent_id, candidate_id, edge = sys.argv[1:]
authority = ActivationAuthority.model_validate_json(authority_json)
with Database(path) as db:
    store = CoreStore(db, CoreConfig.model_validate_json(config_json))

    def before(conn, cursor, statement, parameters, context, executemany):
        if edge == "before_event" and statement.startswith("INSERT INTO core_activations"):
            os._exit(91)

    def after(conn, cursor, statement, parameters, context, executemany):
        if edge == "after_event" and statement.startswith("INSERT INTO core_activations"):
            os._exit(91)
        if edge == "after_pointer" and statement.startswith("UPDATE core_active_model"):
            os._exit(91)

    def before_commit(conn):
        if edge == "before_commit":
            os._exit(91)

    event.listen(db.engine, "before_cursor_execute", before)
    event.listen(db.engine, "after_cursor_execute", after)
    event.listen(db.engine, "commit", before_commit)
    activate_model(
        store,
        expected_model_id=parent_id,
        candidate_id=candidate_id,
        authority=authority,
        at=authority.commissioned_at,
    )
    if edge == "after_commit":
        os._exit(91)
raise AssertionError("crash edge was not reached")
