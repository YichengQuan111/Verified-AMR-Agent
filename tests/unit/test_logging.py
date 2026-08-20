from __future__ import annotations

import json

from services.config.settings import LoggingSettings
from services.observability import configure_logging, get_logger


def test_json_logging_emits_structured_event(capsys) -> None:
    configure_logging(LoggingSettings(level="INFO", json_output=True))
    get_logger("smoke", component="test").info("structured_log_smoke", run_id="r-1")

    line = capsys.readouterr().out.strip().splitlines()[-1]
    event = json.loads(line)
    assert event["event"] == "structured_log_smoke"
    assert event["component"] == "test"
    assert event["run_id"] == "r-1"
    assert event["level"] == "info"
    assert "timestamp" in event
