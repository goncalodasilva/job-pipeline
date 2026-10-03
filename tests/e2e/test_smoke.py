"""Smoke test: start the real app, check ``/health`` and ``/``, stop it (spec 4.1; M0.16).

AC-M0.1: the command serves ``GET /health`` with 200 within 5 seconds.
AC-M0.5 end to end: the launch token is registered with the redaction filter, so it never
reaches the log files or the console, even when a request puts it in the URL path. Strings
shaped like Anthropic keys are redacted too. Every value here is fictional.
"""

import json
import re
from pathlib import Path

import pytest

from tests.page_token import page_token
from tests.running_app import RunningApp

pytestmark = pytest.mark.smoke

HEALTH_DEADLINE_SECONDS = 5.0
TITLE = "<title>Job Search Pipeline</title>"
TOKEN_SHAPE = re.compile(r"[A-Za-z0-9_-]{43}")
REDACTED = "[REDACTED]"
PROTECTIVE_HEADERS = {
    "x-frame-options": "DENY",
    "content-security-policy": "frame-ancestors 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}
FICTIONAL_BODY = "FICTIONALsmokeOnly0123456789_abc-XYZ"


def anthropic_shaped(kind: str, body: str = FICTIONAL_BODY) -> str:
    """Build an Anthropic-shaped token at runtime, so secret scanners see none in the repo."""
    return "-".join(["sk", "ant", kind, body])


def _log_records(files: list[Path]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in files:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                records.append(json.loads(line))
    return records


def test_ac_m0_1_health_200_within_5_seconds(running_app: RunningApp) -> None:
    elapsed = running_app.ready_time - running_app.start_time
    assert elapsed < HEALTH_DEADLINE_SECONDS, f"/health took {elapsed:.2f} s to answer 200"

    with running_app.client() as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["database"] == "ok"
    assert body["runner"] == "running"


def test_smoke_root_page_200(running_app: RunningApp) -> None:
    with running_app.client() as client:
        response = client.get("/")

    assert response.status_code == 200, response.text[:200]
    assert response.headers["content-type"].startswith("text/html")
    assert TITLE in response.text
    assert response.headers.get("cache-control") == "no-store"
    for name, value in PROTECTIVE_HEADERS.items():
        assert response.headers.get_list(name) == [value], name
    token = page_token(response.text)
    assert TOKEN_SHAPE.fullmatch(token), "the launch token is 43 URL-safe characters"


def test_ac_m0_5_launch_token_not_in_log_files(running_app: RunningApp) -> None:
    fake_key = anthropic_shaped("oat01")
    with running_app.client() as client:
        page = client.get("/")
        assert page.status_code == 200, page.text[:200]
        token = page_token(page.text)
        with_token = {"X-JP-Token": token}

        fragment = client.get(
            "/fragments/smoke-probe", headers={**with_token, "HX-Request": "true"}
        )
        action = client.post(
            "/actions/smoke-probe",
            headers={**with_token, "origin": running_app.origin},
            content=b"fictional=1",
        )
        # Adversarial: the secrets in the URL path, where the access log would record them.
        token_in_path = client.get(f"/smoke-token/{token}", headers=with_token)
        key_in_path = client.get(f"/smoke-key/{fake_key}", headers=with_token)

    # Past the guards (not 400 or 403); there are no such routes, so 404 or 405.
    for response in (fragment, action, token_in_path, key_in_path):
        assert response.status_code in (404, 405), (response.request.url, response.status_code)

    result = running_app.stop()
    assert result.returncode == 0 and not result.killed, running_app.output_tail()

    files = running_app.log_files()
    assert files, f"no log files in {running_app.log_dir}"
    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        assert token not in text, f"launch token found in {path.name}"
        assert fake_key not in text, f"Anthropic-shaped string found in {path.name}"
    for stream, text in (
        ("stdout", running_app.stdout_text()),
        ("stderr", running_app.stderr_text()),
    ):
        assert token not in text, f"launch token found in {stream}"
        assert fake_key not in text, f"Anthropic-shaped string found in {stream}"

    access = [
        str(record["message"])
        for record in _log_records(files)
        if record.get("logger") == "uvicorn.access"
    ]
    expected = (
        '"GET /fragments/smoke-probe ',
        '"POST /actions/smoke-probe ',
        f'"GET /smoke-token/{REDACTED} ',
        f'"GET /smoke-key/{REDACTED} ',
    )
    for fragment_of_line in expected:
        assert any(fragment_of_line in line for line in access), (
            f"no access-log line containing {fragment_of_line!r}; got {access}"
        )
