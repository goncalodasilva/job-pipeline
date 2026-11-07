"""Subprocess audit probe; paths come exclusively from the test's temporary home."""

import json
import os
import sys
from pathlib import Path

from platformdirs import user_data_dir


def main() -> None:
    real = Path(user_data_dir("jobpipeline"))
    config = Path(os.environ["JOBPIPELINE_CONFIG_DIR"])
    accesses: list[str] = []

    def audit(event: str, args: tuple[object, ...]) -> None:
        if event not in ("open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rename"):
            return
        for value in args[:2]:
            if isinstance(value, (str, bytes)):
                path = Path(os.fsdecode(value)).absolute()
                if path in (real, config) or real in path.parents or config in path.parents:
                    accesses.append(event)
                    raise AssertionError("demo accessed a real workspace path")

    sys.addaudithook(audit)
    from fastapi.testclient import TestClient

    from app.config import CliOptions, resolve_config
    from app.main import create_app

    resolved = resolve_config(CliOptions(demo=True, no_browser=True), os.environ)
    with TestClient(create_app(resolved), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/")
        assert response.status_code == 200 and "Today" in response.text
        count = client.app.state.workspace.db.connection.execute(
            "SELECT count(*) FROM jobs"
        ).fetchone()[0]
        assert count == 15
    print(json.dumps({"accesses": accesses, "jobs": count}))


if __name__ == "__main__":
    main()
