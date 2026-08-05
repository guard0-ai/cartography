import json
import os

from cartography.connector_outcome import CONNECTOR_OUTCOME_FD_ENV
from cartography.connector_outcome import ConnectorOutcome
from cartography.connector_outcome import emit_connector_outcome


def test_emit_connector_outcome_writes_bounded_non_secret_event(monkeypatch) -> None:
    reader, writer = os.pipe()
    try:
        monkeypatch.setenv(CONNECTOR_OUTCOME_FD_ENV, str(writer))
        emit_connector_outcome(
            ConnectorOutcome(
                provider="aws",
                attempted=3,
                succeeded=2,
                failed=1,
            ),
        )
        payload = json.loads(os.read(reader, 4096))
    finally:
        os.close(reader)
        os.close(writer)

    assert payload == {
        "provider": "aws",
        "attempted": 3,
        "succeeded": 2,
        "failed": 1,
    }
