"""Uploads through the real app: the launch token, the origin and the size cap (M3.1; AC-M3.7;
spec 2.9 rules 2, 3, 5 and 11).

``POST /facts/import`` arrives in M3.4, so the fixture adds a test-only route around
``read_upload`` to the app that ``create_app`` builds, as the security middleware tests do.
A rejected request must never reach the route. The new names are imported inside the tests.
"""

import importlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.security import TOKEN_HEADER
from app.config import RuntimeConfig
from app.main import create_app
from tests.fakes.clock import FrozenClock
from tests.setup_harness import complete_setup
from tests.upload_bodies import CONTENT_TYPE, FieldPart, FilePart, multipart_body

pytestmark = pytest.mark.integration

START = datetime(2026, 10, 11, 9, 0, 0, tzinfo=UTC)
PORT = 8000
HOST = f"127.0.0.1:{PORT}"
ORIGIN = f"http://{HOST}"
UPLOAD_PATH = "/probe/upload"
MIB = 1024 * 1024
PDF_BYTES = b"%PDF-1.4\nfictional bytes that only look like a PDF\n"
SECURITY_HEADERS = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


@dataclass
class Harness:
    client: TestClient
    token: str
    data_dir: Path
    uploads: list[Any] = field(default_factory=list)

    def post(self, body: bytes, **headers: str) -> Any:
        merged = {"content-type": CONTENT_TYPE, **headers}
        return self.client.post(UPLOAD_PATH, content=body, headers=merged)

    def trusted(self) -> dict[str, str]:
        return {TOKEN_HEADER: self.token, "origin": ORIGIN}


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "cfg",
        port=PORT,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
    )


def _add_upload_route(app: FastAPI, uploads: list[Any]) -> None:
    async def receive_upload(request: Request) -> JSONResponse:
        # Looked up per request, so a missing module fails the test and not the fixture.
        read_upload = importlib.import_module("app.api.uploads").read_upload
        upload = await read_upload(request)
        uploads.append(upload)
        return JSONResponse({"source": upload.source, "size": len(upload.data)})

    app.add_api_route(UPLOAD_PATH, receive_upload, methods=["POST"])


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    complete_setup(tmp_path / "data", tmp_path / "cfg", clock=FrozenClock(START))
    app = create_app(_config(tmp_path), clock=FrozenClock(START))
    uploads: list[Any] = []
    _add_upload_route(app, uploads)
    with TestClient(app, base_url=f"http://{HOST}") as client:
        yield Harness(
            client=client, token=app.state.launch_token, data_dir=tmp_path / "data", uploads=uploads
        )


def _message(name: str) -> str:
    errors = importlib.import_module("app.domain.errors")
    assert hasattr(errors, name), f"app.domain.errors.{name} doesn't exist yet"
    text: str = getattr(errors, name)().user_message
    return text


def _stored_files(data_dir: Path) -> set[str]:
    return {str(path.relative_to(data_dir)) for path in data_dir.rglob("*") if path.is_file()}


def test_upload_needs_token_and_origin(harness: Harness) -> None:
    body = multipart_body(FilePart("cv.pdf", PDF_BYTES))
    refused = {
        "no token, no origin": {},
        "origin only": {"origin": ORIGIN},
        "token only": {TOKEN_HEADER: harness.token},
        "wrong token": {TOKEN_HEADER: "x" * len(harness.token), "origin": ORIGIN},
        "another site's origin": {TOKEN_HEADER: harness.token, "origin": "http://evil.example"},
        "origin null": {TOKEN_HEADER: harness.token, "origin": "null"},
    }

    for label, headers in refused.items():
        response = harness.post(body, **headers)
        assert response.status_code == 403, label
        assert harness.token not in response.text, label
    assert harness.uploads == [], "a rejected upload reached the route"

    accepted = harness.post(body, **harness.trusted())

    assert accepted.status_code == 200, accepted.text
    assert accepted.json() == {"source": "file", "size": len(PDF_BYTES)}
    assert harness.uploads[0].data == PDF_BYTES


def test_pasted_text_through_the_app(harness: Harness) -> None:
    text = "Alex Example\nOperations Analyst at Example Widgets Ltd\n"

    response = harness.post(multipart_body(FieldPart(text)), **harness.trusted())

    assert response.status_code == 200, response.text
    assert harness.uploads[0].source == "text"
    assert harness.uploads[0].data == text.encode("utf-8")


def test_ac_m3_7_oversized_upload_gets_413_and_friendly_text(harness: Harness) -> None:
    before = _stored_files(harness.data_dir)
    body = multipart_body(FilePart("cv.pdf", b"x" * (11 * MIB)))

    response = harness.post(body, **harness.trusted())

    assert response.status_code == 413
    assert response.headers["content-type"].startswith("text/plain")
    assert response.text == _message("UploadTooLarge")
    for name, value in SECURITY_HEADERS.items():
        assert response.headers[name] == value
    assert harness.uploads == []
    # Reading an upload stores nothing: no partial record is left behind.
    assert _stored_files(harness.data_dir) == before


def test_ac_m3_7_two_files_get_400_and_friendly_text(harness: Harness) -> None:
    before = _stored_files(harness.data_dir)
    body = multipart_body(FilePart("cv.pdf", PDF_BYTES), FilePart("other.pdf", PDF_BYTES))

    response = harness.post(body, **harness.trusted())

    assert response.status_code == 400
    assert response.text == _message("UploadNotAccepted")
    assert harness.uploads == []
    assert _stored_files(harness.data_dir) == before
