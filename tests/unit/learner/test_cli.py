import io
import json

from quantbot.cli import main
from quantbot.storage import Database


def test_learner_status_uses_only_explicit_store_and_no_runtime(tmp_path, monkeypatch):
    def no_runtime():
        raise AssertionError("learner must never construct trading runtime")

    monkeypatch.setattr("quantbot.cli._default_context", no_runtime)
    path = tmp_path / "learner.db"
    Database.create(path).close()
    output = io.StringIO()
    assert main(["learner", "--database", str(path), "status"], output=output) == 0
    result = json.loads(output.getvalue())
    assert result["models"] == []
    assert result["positive_expectancy_justified"] is False
    assert result["protected_holdout_evidence"] == "NOT_EVALUATED"


def test_learner_does_not_create_a_mistyped_database(tmp_path):
    path = tmp_path / "missing.db"
    output = io.StringIO()
    assert main(["learner", "--database", str(path), "status"], output=output) == 2
    assert not path.exists()
    assert "does not create" in output.getvalue()
