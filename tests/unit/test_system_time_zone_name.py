"""IANA name detection uses synthetic links only, never the host's configuration."""

from pathlib import Path

import pytest

from app import wiring


@pytest.mark.parametrize(
    "mode", ["link", "relative_link", "copied_file", "unknown_name", "missing"]
)
def test_system_time_zone_name(mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    link = tmp_path / "localtime"
    if mode in {"link", "relative_link", "unknown_name"}:
        name = "Europe/Lisbon" if mode != "unknown_name" else "Unknown/Fictional"
        target = tmp_path / "zoneinfo" / name
        target.parent.mkdir(parents=True)
        target.write_bytes(b"fixture only; never opened")
        link.symlink_to(target if mode != "relative_link" else Path("zoneinfo") / name)
    elif mode == "copied_file":
        link.write_bytes(b"fixture only")
    original = Path.readlink
    monkeypatch.setattr(
        Path,
        "readlink",
        lambda self: original(link) if self == Path("/etc/localtime") else original(self),
    )
    assert wiring.system_time_zone_name() == (
        "Europe/Lisbon" if mode in {"link", "relative_link"} else None
    )
