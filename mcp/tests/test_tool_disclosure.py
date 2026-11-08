"""Disclosure tests for MCP tools.

Whatever an MCP tool returns ends up in the LLM's context and is sent to the
model provider. These tests pin down exactly which fields each tool may pass on
(allowlists) and check that nothing else leaks through (canaries).
"""

import json
import re

from pytest_httpx import HTTPXMock

from app.tools.sleep import get_sleep_summary

USER_ID = "00000000-0000-0000-0000-000000000000"

# Unique marker values placed in fields the LLM must never see.
# If one appears in a tool's output, that field leaked.
CANARY_EMAIL = "canary.pii@leak.test"
CANARY_SECRET = "CANARY_SECRET_7f3a"
CANARIES = [CANARY_EMAIL, CANARY_SECRET]


def assert_no_canaries(result: dict) -> None:
    """Fail if any canary appears anywhere in the tool output, including nested values and error strings."""
    blob = json.dumps(result, default=str)
    leaked = [c for c in CANARIES if c in blob]
    assert not leaked, f"Tool output leaked to LLM: {leaked}"


def mock_user(httpx_mock: HTTPXMock) -> None:
    """Mock the user lookup that every health tool does first, with PII the tool must drop."""
    httpx_mock.add_response(
        url=f"https://api.test.com/api/v1/users/{USER_ID}",
        json={
            "id": USER_ID,
            "first_name": "Test",
            "last_name": "User",
            "email": CANARY_EMAIL,  # health tools must not forward the email
            "external_user_id": CANARY_SECRET,  # internal identifier, never for the LLM
        },
    )


# Fields get_sleep_summary is approved to pass on to the LLM. Changing these is a
# deliberate decision about what the model sees.
SLEEP_TOP_KEYS = {"user", "period", "records", "summary"}
SLEEP_USER_KEYS = {"id", "first_name", "last_name"}
SLEEP_RECORD_KEYS = {"date", "start_datetime", "end_datetime", "duration_minutes", "source"}


async def test_sleep_summary_only_discloses_approved_fields(httpx_mock: HTTPXMock) -> None:
    mock_user(httpx_mock)
    # Regex so the mock matches whatever query string the client adds (dates, limit, ...).
    httpx_mock.add_response(
        url=re.compile(rf"https://api\.test\.com/api/v1/users/{USER_ID}/summaries/sleep.*"),
        json={"data": [{
            "date": "2026-01-02",
            "start_time": "2026-01-01T23:00:00Z",
            "end_time": "2026-01-02T07:00:00Z",
            "duration_minutes": 480,
            # The tool should reduce source to the provider name only.
            "source": {"provider": "whoop", "device_id": CANARY_SECRET},
            # A field the backend might add later; the tool must ignore unknown fields.
            "raw_payload": {"token": CANARY_SECRET},
        }]},
    )

    result = await get_sleep_summary(user_id=USER_ID, start_date="2026-01-01", end_date="2026-01-07")

    # Exact key matches: fail if a field is added or removed.
    assert set(result) == SLEEP_TOP_KEYS
    assert set(result["user"]) == SLEEP_USER_KEYS
    assert len(result["records"]) == 1  # otherwise the all() below passes on an empty list
    assert all(set(r) == SLEEP_RECORD_KEYS for r in result["records"])
    # Catch-all for leaks the key checks can't see.
    assert_no_canaries(result)
