"""Incident details contain stable diagnostics, never broker message text."""

from quantbot.brokers.base import BrokerRequestError
from quantbot.operations.cycle import _exception_detail


def test_broker_http_status_is_saved_without_exception_message() -> None:
    error = BrokerRequestError("secret-bearing broker response", status_code=403)

    detail = _exception_detail(error)

    assert detail["exception_type"] == "BrokerRequestError"
    assert detail["status_code"] == 403
    assert "secret-bearing" not in repr(detail)
