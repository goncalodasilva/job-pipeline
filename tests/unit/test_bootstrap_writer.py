"""Bootstrap config writer and config-folder ownership check (M1.4).

Spec 2.8; ADR-024, 038, 043, 044.

``write_bootstrap`` writes ``config.toml`` by hand (no ``tomli-w``, ADR-044 item 1), atomically
(temporary file in the same folder, then ``os.replace``), with the file 0600 and a created
folder 0700 on POSIX (ADR-038). It refuses a config folder owned by another user, the rule
ADR-043 set for the data folder. Every path here is fictional and lives under a temporary
folder; no test touches the developer's real config folder.
"""

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.config import (
    BOOTSTRAP_FILE,
    load_bootstrap,
    owned_by_another_user,
    write_bootstrap,
)
from app.domain.errors import ConfigError

FICTIONAL_VERSION = "0.0.1-test"
OTHER_VERSION = "0.0.2-test"

# Windows has no owner-only mode bits (ACLs protect the folder); the mode assertions below
# return early there, the same pattern as the ADR-038 database permission test.
POSIX = os.name == "posix"


# --- helpers -------------------------------------------------------------------------------


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def names_in(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def assert_detail_hides(error: ConfigError, *paths: Path) -> None:
    """ConfigError detail names the setting, never its value (app/domain/errors.py)."""
    detail = str(error)
    for path in paths:
        assert str(path) not in detail
        assert path.as_posix() not in detail


def existing_bootstrap(config_dir: Path, data_dir: Path) -> bytes:
    """A valid config.toml written by the writer itself; returns its bytes."""
    write_bootstrap(config_dir, data_dir=data_dir, version=FICTIONAL_VERSION)
    return (config_dir / BOOTSTRAP_FILE).read_bytes()


# --- round trip ----------------------------------------------------------------------------

# Anything a str can hold except lone surrogates (tested separately: TOML can't hold them).
_text_chars = st.characters(exclude_categories=["Cs"])
_tricky = st.sampled_from(
    [
        '"',
        "\\",
        '\\"',
        '""" quoted """',
        "'single'",
        "\t",
        "\n",
        "\r\n",
        "\x00",
        "\x1f",
        "\x7f",
        "\x08\x0c",
        "\\u0041",
        "\\n",
        "C:\\Users\\x",
        "D:\\Jobs\\Search\\",
        "\\\\server\\share",
        "~",
        "${HOME}",
        "é ü ñ",
        "日本語",
        "\U0001f600",
        "\U00010348",
        "\u2028\u2029",
        "\ufeff",
        "# not a comment",
        "key = value",
        "]]",
    ]
)
# No ":" in random segments: on Windows "a:b" joins as a drive-relative (not absolute) path.
_segment = st.one_of(
    _tricky,
    st.text(alphabet=_text_chars, min_size=1, max_size=20).filter(lambda text: ":" not in text),
)


@settings(max_examples=200, deadline=None)
@given(
    segments=st.lists(_segment, min_size=1, max_size=4),
    version=st.one_of(_tricky, st.text(alphabet=_text_chars, max_size=40)),
)
def test_write_bootstrap_round_trips_through_load_bootstrap(
    tmp_path_factory: pytest.TempPathFactory, segments: list[str], version: str
) -> None:
    base = tmp_path_factory.mktemp("roundtrip")
    config_dir = base / "config" / "jobpipeline"
    # The tmp base is absolute, so load_bootstrap's leading "~" expansion never applies.
    data_dir = (base / "Fictional Data").joinpath(*segments)
    assert data_dir.is_absolute()

    written = write_bootstrap(config_dir, data_dir=data_dir, version=version)

    assert written == config_dir / BOOTSTRAP_FILE
    loaded = load_bootstrap(config_dir)
    assert loaded is not None
    assert loaded.data_dir == data_dir
    assert loaded.version == version
    # Control characters are escaped, never written raw; only the line breaks between keys.
    text = written.read_text(encoding="utf-8")
    assert not [c for c in text if (ord(c) < 0x20 and c != "\n") or ord(c) == 0x7F]
    assert names_in(config_dir) == [BOOTSTRAP_FILE]


@pytest.mark.parametrize(
    ("path_suffix", "version"),
    [
        ("lone-\udcff-surrogate", FICTIONAL_VERSION),  # undecodable file name byte
        ("high-\ud800-surrogate", FICTIONAL_VERSION),
        ("plain", "0.0.1-\udc80"),
    ],
    ids=["path_low_surrogate", "path_high_surrogate", "version_surrogate"],
)
def test_write_bootstrap_rejects_unrepresentable_values(
    tmp_path: Path, path_suffix: str, version: str
) -> None:
    config_dir = tmp_path / "config" / "jobpipeline"
    data_dir = tmp_path / "data" / path_suffix

    with pytest.raises(ConfigError) as caught:
        write_bootstrap(config_dir, data_dir=data_dir, version=version)

    assert names_in(config_dir) == [], "no config.toml and no temporary file left behind"
    assert_detail_hides(caught.value, config_dir, data_dir)


def test_write_bootstrap_rejects_unrepresentable_value_keeps_existing_file(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / "config"
    before = existing_bootstrap(config_dir, tmp_path / "data")

    with pytest.raises(ConfigError):
        write_bootstrap(config_dir, data_dir=tmp_path / "bad-\udcff", version=FICTIONAL_VERSION)

    assert (config_dir / BOOTSTRAP_FILE).read_bytes() == before
    assert names_in(config_dir) == [BOOTSTRAP_FILE]


@pytest.mark.parametrize("relative", ["data", "relative/data", "./data", "../data"])
def test_write_bootstrap_rejects_relative_data_dir(tmp_path: Path, relative: str) -> None:
    config_dir = tmp_path / "config"

    with pytest.raises(ConfigError) as caught:
        write_bootstrap(config_dir, data_dir=Path(relative), version=FICTIONAL_VERSION)

    assert names_in(config_dir) == []
    assert "data_dir" in str(caught.value)
    assert_detail_hides(caught.value, config_dir)


# --- atomic write and permissions ----------------------------------------------------------


def test_write_bootstrap_is_atomic_and_0600(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "Fictional Data"
    target = config_dir / BOOTSTRAP_FILE
    real_replace: Callable[..., None] = os.replace
    replaced: list[tuple[Path, Path, int]] = []

    def recording_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        replaced.append((Path(src), Path(dst), mode_of(Path(src))))
        real_replace(src, dst)

    # Path.replace calls os.replace too, so either spelling in the writer is observed.
    monkeypatch.setattr(os, "replace", recording_replace)
    write_bootstrap(config_dir, data_dir=data_dir, version=FICTIONAL_VERSION)
    monkeypatch.setattr(os, "replace", real_replace)

    assert len(replaced) == 1, "written to a temporary file, then moved into place once"
    src, dst, src_mode = replaced[0]
    assert dst == target
    assert src.parent == config_dir, "temporary file in the same folder (same file system)"
    assert src.name != BOOTSTRAP_FILE
    assert names_in(config_dir) == [BOOTSTRAP_FILE], "no temporary files left on success"
    if POSIX:
        assert src_mode == 0o600, "the temporary file is private from the start"
        assert mode_of(target) == 0o600

    # A failure at the final step leaves the existing file as it was and cleans up.
    before = target.read_bytes()

    def failing_replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        raise OSError("fictional disk failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    with pytest.raises(ConfigError) as caught:
        write_bootstrap(config_dir, data_dir=tmp_path / "Other Data", version=OTHER_VERSION)
    monkeypatch.setattr(os, "replace", real_replace)

    assert target.read_bytes() == before
    assert names_in(config_dir) == [BOOTSTRAP_FILE], "temporary file removed after a failure"
    assert_detail_hides(caught.value, data_dir, tmp_path / "Other Data", config_dir)
    loaded = load_bootstrap(config_dir)
    assert loaded is not None
    assert (loaded.data_dir, loaded.version) == (data_dir, FICTIONAL_VERSION)


def test_write_bootstrap_tightens_existing_file_to_0600(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    target = config_dir / BOOTSTRAP_FILE
    target.write_text('data_dir = "/fictional/old"\nversion = "0.0.0"\n', encoding="utf-8")
    target.chmod(0o644)

    write_bootstrap(config_dir, data_dir=tmp_path / "data", version=FICTIONAL_VERSION)

    loaded = load_bootstrap(config_dir)
    assert loaded is not None
    assert loaded.data_dir == tmp_path / "data"
    if not POSIX:  # Windows has no owner-only mode bits
        return
    assert mode_of(target) == 0o600


def test_config_dir_created_0700(tmp_path: Path) -> None:
    nested = tmp_path / "missing" / "parents" / "jobpipeline"

    write_bootstrap(nested, data_dir=tmp_path / "data", version=FICTIONAL_VERSION)

    assert (nested / BOOTSTRAP_FILE).is_file()
    existing = tmp_path / "existing"
    existing.mkdir()
    existing.chmod(0o755)
    write_bootstrap(existing, data_dir=tmp_path / "data", version=FICTIONAL_VERSION)
    assert (existing / BOOTSTRAP_FILE).is_file()
    if not POSIX:  # Windows has no owner-only mode bits
        return
    assert mode_of(nested) == 0o700
    assert mode_of(existing) == 0o755, "an existing folder keeps its permissions (ADR-038)"


def test_write_bootstrap_overwrites_existing(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    write_bootstrap(config_dir, data_dir=tmp_path / "first", version=FICTIONAL_VERSION)

    written = write_bootstrap(config_dir, data_dir=tmp_path / "second", version=OTHER_VERSION)

    loaded = load_bootstrap(config_dir)
    assert loaded is not None
    assert (loaded.data_dir, loaded.version) == (tmp_path / "second", OTHER_VERSION)
    assert written == config_dir / BOOTSTRAP_FILE
    assert names_in(config_dir) == [BOOTSTRAP_FILE]


# --- ownership (ADR-043; the config folder gets the data folder's rule) --------------------


def test_write_bootstrap_refuses_config_dir_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"
    data_dir = tmp_path / "data"
    before = existing_bootstrap(config_dir, data_dir)
    other_user = config_dir.stat().st_uid + 1
    monkeypatch.setattr(os, "geteuid", lambda: other_user, raising=False)

    with pytest.raises(ConfigError) as caught:
        write_bootstrap(config_dir, data_dir=tmp_path / "elsewhere", version=OTHER_VERSION)

    assert (config_dir / BOOTSTRAP_FILE).read_bytes() == before
    assert names_in(config_dir) == [BOOTSTRAP_FILE], "nothing written into a foreign folder"
    assert "config folder" in str(caught.value).lower()
    assert_detail_hides(caught.value, config_dir, data_dir, tmp_path / "elsewhere")


def test_write_bootstrap_checks_ownership_after_creating_the_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A folder another account creates just before mkdir (a race) is refused too."""
    config_dir = tmp_path / "config"
    assert not config_dir.exists()
    monkeypatch.setattr(os, "geteuid", lambda: -1, raising=False)  # nobody owns it

    with pytest.raises(ConfigError) as caught:
        write_bootstrap(config_dir, data_dir=tmp_path / "data", version=FICTIONAL_VERSION)

    assert names_in(config_dir) == []
    assert "config folder" in str(caught.value).lower()


def test_owned_by_another_user_moved_to_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "folder"
    folder.mkdir()
    owner = folder.stat().st_uid

    monkeypatch.setattr(os, "geteuid", lambda: owner, raising=False)
    assert owned_by_another_user(folder) is False

    monkeypatch.setattr(os, "geteuid", lambda: owner + 1, raising=False)
    assert owned_by_another_user(folder) is True
    assert owned_by_another_user(tmp_path / "missing") is False, "unusable folder: not refused"

    monkeypatch.delattr(os, "geteuid", raising=False)  # Windows: no POSIX owner ids
    assert owned_by_another_user(folder) is False


def test_owned_by_another_user_follows_symlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The folder a link points to is what counts (ADR-043)."""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:  # Windows without symlink rights: nothing to follow
        return
    owner = target.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner, raising=False)
    assert owned_by_another_user(link) is False
    monkeypatch.setattr(os, "geteuid", lambda: owner + 1, raising=False)
    assert owned_by_another_user(link) is True


def test_write_bootstrap_maps_temp_file_failure_to_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"

    def refuse(*args: object, **kwargs: object) -> tuple[int, str]:
        raise PermissionError("fictional: read-only folder")

    monkeypatch.setattr("app.config.tempfile.mkstemp", refuse)

    with pytest.raises(ConfigError) as raised:
        write_bootstrap(config_dir, data_dir=tmp_path / "data", version="0.1.0")

    assert str(config_dir) not in str(raised.value)
    assert list(config_dir.iterdir()) == []


def test_write_bootstrap_interrupted_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_dir = tmp_path / "config"

    def interrupt(*args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("app.config.os.replace", interrupt)

    with pytest.raises(KeyboardInterrupt):
        write_bootstrap(config_dir, data_dir=tmp_path / "data", version="0.1.0")

    assert list(config_dir.iterdir()) == []


def _link_owned_by(monkeypatch: pytest.MonkeyPatch, link: Path, owner: int) -> None:
    """Make ``os.lstat`` report ``link`` as owned by ``owner`` (only root can chown a link)."""
    real_lstat = os.lstat

    def fake_lstat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        result = real_lstat(path, *args, **kwargs)  # type: ignore[arg-type]
        if Path(os.fspath(path)) == link:  # type: ignore[arg-type]
            fields = list(result)
            fields[stat.ST_UID] = owner
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(os, "lstat", fake_lstat)


def test_owned_by_another_user_refuses_a_link_owned_by_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link planted by another account is refused even when it points to the user's own
    folder (ADR-043 amendment)."""
    target = tmp_path / "own-folder"
    target.mkdir()
    link = tmp_path / "planted-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:  # Windows without symlink rights
        return
    owner = target.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner, raising=False)
    _link_owned_by(monkeypatch, link, owner + 1)

    assert owned_by_another_user(link) is True
    assert owned_by_another_user(target) is False


def test_write_bootstrap_refuses_config_dir_link_owned_by_another_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "own-app-settings"
    target.mkdir()
    (target / BOOTSTRAP_FILE).write_text("untouched", encoding="utf-8")
    link = tmp_path / "shared" / "jp"
    link.parent.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        return
    owner = target.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: owner, raising=False)
    _link_owned_by(monkeypatch, link, owner + 1)

    with pytest.raises(ConfigError):
        write_bootstrap(link, data_dir=tmp_path / "data", version="0.1.0")

    assert (target / BOOTSTRAP_FILE).read_text(encoding="utf-8") == "untouched"
