import json
import logging
import os
from dataclasses import asdict
from dataclasses import dataclass

logger = logging.getLogger(__name__)

CONNECTOR_OUTCOME_FD_ENV = "GUARD0_CARTOGRAPHY_CONNECTOR_OUTCOME_FD"


@dataclass(frozen=True)
class ConnectorOutcome:
    provider: str
    attempted: int
    succeeded: int
    failed: int


def emit_connector_outcome(outcome: ConnectorOutcome) -> None:
    """
    Emit a bounded, non-secret connector outcome to the caller-owned pipe.

    Standalone Cartography invocations do not provide the file descriptor and
    retain their existing behavior.
    """
    raw_fd = os.getenv(CONNECTOR_OUTCOME_FD_ENV)
    if not raw_fd:
        return
    try:
        fd = int(raw_fd)
        payload = json.dumps(asdict(outcome), separators=(",", ":")) + "\n"
        os.write(fd, payload.encode("utf-8"))
    except (OSError, TypeError, ValueError):
        logger.exception("Unable to emit the bounded connector outcome")
        raise
