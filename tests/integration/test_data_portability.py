"""M9.1 part B: private JSON export with files, staged restore and tamper refusals (ADR-131)."""

import base64
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import stat
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.modules.storage import MIGRATIONS_DIR, Database, apply_migrations, load_migrations
from app.modules.storage.portability import PortabilityError, export_tables
from app.services import data_portability as module
from app.services.data_folder import SYSTEM_FILES
from app.services.data_portability import DataPortabilityService
from tests.fakes.clock import FrozenClock
from tests.integration.test_portability import fresh, seed, seed_every_table
from tests.integration.test_schema import schema_snapshot
from tests.integration.test_schema_0002 import insert_usage
from tests.integration.test_schema_0003 import task_row
from tests.integration.test_schema_0004 import START, insert

pytestmark = pytest.mark.integration
FILES = {
    "documents/app-example/export-0001.pdf": b"Fictional PDF bytes\x00\xff",
    "assets/photo-fictional.png": b"Fictional photo bytes\x01",
    "imports/fictional.txt": b"Fictional CV text",
    "prompts/overrides/refine_fact/prompt.md": b"Fictional override bytes",
}


def service(db, *, migrations_dir=MIGRATIONS_DIR):
    return DataPortabilityService(
        lambda: db, lambda: db.path.parent, FrozenClock(START), migrations_dir=migrations_dir
    )


def make_files(root):
    for name, data in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for folder in ("logs", "backups"):
        (root / folder).mkdir(exist_ok=True)
        (root / folder / "not-exported.txt").write_text("fictional private diagnostic")


def tree(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


def test_ac_m9_3_export_import_round_trip_exact_with_files(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed_every_table(source)
        make_files(source.path.parent)
        archive = service(source).export(include_usage_snapshots=True)
        payload = json.loads(archive.read_bytes())
        assert payload["format"] == 1 and payload["app_version"]
        assert payload["schema_version"] == max(m.version for m in load_migrations(MIGRATIONS_DIR))
        assert {f["name"] for f in payload["files"]} == set(FILES)
        assert not [f for f in payload["files"] if f["name"].startswith(("logs/", "backups/"))]
        service(target).restore(archive)
        assert export_tables(target, include_usage_snapshots=True) == export_tables(
            source, include_usage_snapshots=True
        )
        assert schema_snapshot(target.connection) == schema_snapshot(source.connection)
        for name, data in FILES.items():
            assert (target.path.parent / name).read_bytes() == data
        assert not (target.path.parent / "logs").exists()
        assert not (target.path.parent / "backups").exists()
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700


def ai_provider(db):
    row = db.connection.execute("SELECT value FROM settings WHERE key='ai_provider'").fetchone()
    return json.loads(row[0])


def test_ac_m9_3_export_contains_no_keys(tmp_path, monkeypatch):
    from app.modules.secrets import ANTHROPIC_API_KEY
    from app.modules.secrets.providers.memory import MemorySecretStore

    marker = "fictional" + "-credential-marker"
    store = MemorySecretStore()
    store.set(ANTHROPIC_API_KEY, marker)
    monkeypatch.setenv("ANTHROPIC_API_KEY", marker)
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed(source)  # its ai_provider setting records which kinds have a saved sign-in
        assert ai_provider(source)["saved_sign_ins"] == ["claude_api_key"]
        archive = service(source).export()
        artifact = archive.read_bytes()
        assert marker.encode() not in artifact
        saved = json.loads(artifact)["tables"]["settings"]
        assert marker not in json.dumps(saved)  # only kind names, never a credential value
        service(target).restore(archive)
        assert ai_provider(target)["saved_sign_ins"] == []
        assert marker.encode() not in target.path.read_bytes()
        assert store.get(ANTHROPIC_API_KEY) == marker


def test_import_clears_saved_sign_ins(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed(source)
        assert ai_provider(source)["saved_sign_ins"] == ["claude_api_key"]
        service(target).restore(service(source).export())
        restored = ai_provider(target)
        assert restored["saved_sign_ins"] == [] and restored["kind"] == "claude_api_key"
        assert "sign_in_saved_for" not in restored


def test_export_excludes_usage_snapshots_unless_opted_in(tmp_path):
    with fresh(tmp_path) as db:
        seed_every_table(db)
        clock = FrozenClock(START)
        exports = DataPortabilityService(lambda: db, lambda: db.path.parent, clock)
        first = json.loads(exports.export().read_bytes())
        assert "provider_usage_snapshots" not in first["tables"]
        assert "provider_usage_snapshots" in first["excluded"]
        clock.advance(timedelta(seconds=1))
        opted = json.loads(exports.export(include_usage_snapshots=True).read_bytes())
        assert opted["tables"]["provider_usage_snapshots"]["rows"]


UNSAFE_NAMES = [
    pytest.param("../outside.txt", id="parent_traversal"),
    pytest.param("app.db", id="database"),
    pytest.param("config.toml", id="config"),
    pytest.param("logs/a.txt", id="logs"),
    pytest.param("backups/a", id="backups"),
    pytest.param("exports/a.json", id="exports"),
    pytest.param("documents/a.pdf", id="documents_too_shallow"),
    pytest.param("documents/a/b/c.pdf", id="documents_too_deep"),
    pytest.param("/etc/x", id="absolute"),
    pytest.param("imports\\a.txt", id="backslash"),
    pytest.param("imports/a.txt/", id="trailing_slash"),
]


def tampered(tmp_path, change):
    """A valid archive from a seeded source, changed one way; returns (path, target db args)."""
    with fresh(tmp_path / "source") as source:
        seed(source)
        make_files(source.path.parent)
        archive = service(source).export()
    payload = json.loads(archive.read_bytes())
    if change == "unknown_table":
        payload["tables"]["unexpected"] = {"columns": [], "rows": []}
    elif change == "unknown_column":
        payload["tables"]["facts"]["columns"][0] = "unexpected"
    elif change == "sha_mismatch":
        payload["files"][0]["sha256"] = "0" * 64
    elif change == "unsafe_file_name":
        payload["files"][0]["name"] = "../outside.txt"
    elif change == "duplicate_file":
        payload["files"].append(copy.deepcopy(payload["files"][0]))
    elif change == "invalid_base64":
        payload["files"][0]["data"] = "!invalid"
    elif change.startswith("name:"):
        payload["files"][0]["name"] = change[5:]
    data = json.dumps(payload).encode()
    if change == "bad_json":
        data = b"{" + data
    sent = tmp_path / "sent.json"
    sent.write_bytes(data)
    return sent


def assert_refused_and_untouched(tmp_path, change, monkeypatch=None):
    sent = tampered(tmp_path, change)
    if monkeypatch is not None:
        monkeypatch.setattr(module, "MAX_EXPORT_BYTES", sent.stat().st_size - 1)
    with fresh(tmp_path / "target") as target:
        before, paths = export_tables(target), tree(target.path.parent)
        with pytest.raises(PortabilityError):
            service(target).restore(sent)
        assert export_tables(target) == before
        assert tree(target.path.parent) == paths
        assert not (tmp_path / "outside.txt").exists()


@pytest.mark.parametrize(
    "change",
    [
        "bad_json",
        "unknown_table",
        "unknown_column",
        "sha_mismatch",
        "unsafe_file_name",
        "duplicate_file",
        "oversize",
        "invalid_base64",
    ],
)
def test_import_refuses_tampered_file(tmp_path, monkeypatch, change):
    if change == "oversize":
        assert_refused_and_untouched(tmp_path, change, monkeypatch)
    else:
        assert_refused_and_untouched(tmp_path, change)


def test_import_refuses_oversize_payload_with_its_own_message(monkeypatch):
    monkeypatch.setattr(module, "MAX_EXPORT_BYTES", 10)
    with pytest.raises(PortabilityError, match="larger than 200 MB"):
        module._payload(b"x" * 11)


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_import_refuses_unsafe_file_names(tmp_path, name):
    assert_refused_and_untouched(tmp_path, "name:" + name)


def test_import_refuses_case_variant_duplicate_file_names(tmp_path):
    def entry(name, data):
        return {
            "name": name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "data": base64.b64encode(data).decode("ascii"),
        }

    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        payload = json.loads(service(source).export().read_bytes())
        payload["files"] = [entry("imports/A.txt", b"one"), entry("imports/a.txt", b"two")]
        sent = tmp_path / "sent.json"
        sent.write_bytes(json.dumps(payload).encode())
        before, paths = export_tables(target), tree(target.path.parent)
        with pytest.raises(PortabilityError):
            service(target).restore(sent)
        assert export_tables(target) == before
        assert tree(target.path.parent) == paths


def file_entry(name, data):
    return {
        "name": name,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "data": base64.b64encode(data).decode("ascii"),
    }


def test_decoded_files_accept_exactly_the_cap_and_refuse_one_byte_more(monkeypatch):
    monkeypatch.setattr(module, "MAX_EXPORT_BYTES", 10)
    exact = [file_entry("imports/a.txt", b"12345"), file_entry("imports/b.txt", b"67890")]
    assert [len(d) for _, d in module._decoded_files(exact)] == [5, 5]
    over = [*exact, file_entry("imports/c.txt", b"1")]
    with pytest.raises(PortabilityError):
        module._decoded_files(over)  # each file is within the cap; the decoded total is not
    with pytest.raises(PortabilityError):
        module._decoded_files([file_entry("imports/a.txt", b"x" * 11)])


def test_import_newer_schema_refused(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        payload = json.loads(service(source).export().read_bytes())
        payload["schema_version"] = 10**6
        sent = tmp_path / "sent.json"
        sent.write_bytes(json.dumps(payload).encode())
        before, paths = export_tables(target), tree(target.path.parent)
        with pytest.raises(PortabilityError):
            service(target).restore(sent)
        assert export_tables(target) == before
        assert tree(target.path.parent) == paths


def test_import_into_non_empty_workspace_refused(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        seed(target)
        make_files(target.path.parent)
        before, paths = export_tables(target), tree(target.path.parent)
        with pytest.raises(PortabilityError):
            service(target).restore(service(source).export())
        assert export_tables(target) == before
        assert tree(target.path.parent) == paths


@pytest.mark.parametrize("kind", ["exports_link", "folder_link", "file_link", "hardlink"])
def test_export_file_private_and_links_refused(tmp_path, kind):
    with fresh(tmp_path / "source") as db:
        root = db.path.parent
        outside = tmp_path / "outside"
        outside.mkdir()
        secret = outside / "fictional.txt"
        secret.write_bytes(b"fictional outside data")
        if kind == "exports_link":
            (root / "exports").symlink_to(outside, target_is_directory=True)
        elif kind == "folder_link":
            (root / "imports").symlink_to(outside, target_is_directory=True)
        else:
            (root / "imports").mkdir()
            if kind == "file_link":
                (root / "imports/fictional.txt").symlink_to(secret)
            else:
                os.link(secret, root / "imports/fictional.txt")
        with pytest.raises(PortabilityError):
            service(db).export()
        assert list(outside.iterdir()) == [secret]
        assert secret.read_bytes() == b"fictional outside data"


def test_import_older_schema_version_migrates_up(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for path in MIGRATIONS_DIR.glob("*.sql"):
        shutil.copy(path, migrations / path.name)
    next_version = max(m.version for m in load_migrations(migrations)) + 1
    (migrations / f"{next_version:04d}_fictional_upgrade.sql").write_text(
        "CREATE TABLE fictional_upgrade (id TEXT PRIMARY KEY);"
    )
    historical = tmp_path / "historical"
    historical.mkdir()
    for migration in load_migrations(MIGRATIONS_DIR):
        if migration.version <= 9:
            shutil.copyfile(migration.path, historical / migration.path.name)
    with (
        Database.open(tmp_path / "source/app.db") as source,
        Database.open(tmp_path / "target/app.db") as target,
    ):
        apply_migrations(source, historical, tmp_path / "source-backups", FrozenClock(START))
        seed(source)
        apply_migrations(target, migrations, tmp_path / "backups", FrozenClock(START))
        archive = service(source).export()
        assert json.loads(archive.read_bytes())["schema_version"] == 9
        service(target, migrations_dir=migrations).restore(archive)
        assert export_tables(target)["schema_version"] == next_version
        assert (
            target.connection.execute("SELECT id FROM facts ORDER BY id").fetchall()
            == source.connection.execute("SELECT id FROM facts ORDER BY id").fetchall()
        )
        with Database.open(tmp_path / "expected/app.db") as expected:
            apply_migrations(
                expected, migrations, tmp_path / "expected-backups", FrozenClock(START)
            )
            assert schema_snapshot(target.connection) == schema_snapshot(expected.connection)
        restored = export_tables(target)["tables"]
        original = export_tables(source)["tables"]
        # ADR-131 explicitly clears imported saved sign-in flags: keys aren't exported.
        settings = original["settings"]
        for row in settings["rows"]:
            if row[settings["columns"].index("key")] == "ai_provider":
                index = settings["columns"].index("value")
                value = json.loads(row[index])
                value.pop("sign_in_saved_for", None)
                value["saved_sign_ins"] = []
                row[index] = json.dumps(value, separators=(",", ":"))
        for name, table in original.items():
            assert restored[name] == table


def test_import_failure_leaves_workspace_empty(tmp_path, monkeypatch):
    from app.modules.storage import portability

    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        make_files(source.path.parent)
        archive = service(source).export()
        original = portability.import_tables

        def fail_target(db, snapshot, **kwargs):
            if db is target:
                raise PortabilityError("Fictional failure before database commit.")
            return original(db, snapshot, **kwargs)

        monkeypatch.setattr(portability, "import_tables", fail_target)
        before, paths = export_tables(target), tree(target.path.parent)
        with pytest.raises(PortabilityError):
            service(target).restore(archive)
        assert export_tables(target) == before
        assert tree(target.path.parent) == paths


def test_export_refuses_photo_replaced_between_table_and_file_reads(tmp_path, monkeypatch):
    from app.services.personal_details import PersonalDetailsService
    from tests.fakes.ids import SequentialIdFactory
    from tests.image_support import png_image

    with fresh(tmp_path) as db:
        details = PersonalDetailsService(
            lambda: db, FrozenClock(START), SequentialIdFactory(), lambda: db.path.parent
        )
        details.save_photo(png_image(100, 120), version=0)
        original = module.portability.export_tables
        calls = 0

        def replace_photo(database, **kwargs):
            nonlocal calls
            snapshot = original(database, **kwargs)
            calls += 1
            if calls == 1:
                details.save_photo(png_image(110, 120), version=1)
            return snapshot

        monkeypatch.setattr(module.portability, "export_tables", replace_photo)
        with pytest.raises(PortabilityError):
            service(db).export()
        assert not (db.path.parent / "exports").exists()
        assert details.view().photo is not None


def test_import_rechecks_files_after_staging_and_preserves_competing_file(tmp_path, monkeypatch):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        make_files(source.path.parent)
        archive = service(source).export()
        original = module.portability.database_from_snapshot
        competing = target.path.parent / "imports/competing.txt"

        def stage(*args, **kwargs):
            result = original(*args, **kwargs)
            competing.parent.mkdir(exist_ok=True)
            competing.write_bytes(b"Fictional competing import")
            return result

        monkeypatch.setattr(module.portability, "database_from_snapshot", stage)
        before = export_tables(target)
        with pytest.raises(PortabilityError):
            service(target).restore(archive)
        assert export_tables(target) == before
        assert competing.read_bytes() == b"Fictional competing import"
        assert not (target.path.parent / "documents").exists()


def test_failed_restore_preserves_existing_directory_modes(tmp_path, monkeypatch):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        make_files(source.path.parent)
        archive = service(source).export()
        assets = target.path.parent / "assets"
        assets.mkdir(mode=0o755)
        assets.chmod(0o755)
        original = module.portability.import_tables

        def fail(database, *args, **kwargs):
            if database is target:
                raise PortabilityError("Fictional database failure.")
            return original(database, *args, **kwargs)

        monkeypatch.setattr(module.portability, "import_tables", fail)
        with pytest.raises(PortabilityError):
            service(target).restore(archive)
        assert stat.S_IMODE(assets.stat().st_mode) == 0o755
        assert list(assets.iterdir()) == []


def test_export_download_uses_generated_name_and_writes_nothing(tmp_path):
    with fresh(tmp_path) as db:
        archive = service(db).export()
        before = tree(tmp_path)
        assert service(db).download(archive.name) == archive.read_bytes()
        assert tree(tmp_path) == before


def test_export_tightens_existing_export_folder_permissions(tmp_path):
    with fresh(tmp_path) as db:
        folder = tmp_path / "exports"
        folder.mkdir(mode=0o755)
        folder.chmod(0o755)
        archive = service(db).export()
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600


def scatter_system_files(root):
    for folder in ("imports", "documents/app-example"):
        (root / folder).mkdir(parents=True, exist_ok=True)
        for name in SYSTEM_FILES:
            (root / folder / name).write_bytes(b"fictional finder noise")


def test_system_files_are_neither_exported_nor_block_restore(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        make_files(source.path.parent)
        scatter_system_files(source.path.parent)
        archive = service(source).export()
        names = {f["name"] for f in json.loads(archive.read_bytes())["files"]}
        assert names == set(FILES)
        scatter_system_files(target.path.parent)  # a workspace holding only Finder noise
        assert service(target).can_restore()
        service(target).restore(archive)
        for name, data in FILES.items():
            assert (target.path.parent / name).read_bytes() == data
        for name in SYSTEM_FILES:  # left alone
            assert (target.path.parent / "imports" / name).read_bytes() == b"fictional finder noise"


@pytest.mark.parametrize("name", ["odd name.txt", ".hidden", "caf\u00e9.txt", "a:b"])
def test_other_odd_file_names_are_still_refused(tmp_path, name):
    with fresh(tmp_path) as db:
        (tmp_path / "imports").mkdir()
        (tmp_path / "imports" / name).write_bytes(b"fictional")
        assert not service(db).can_restore()
        with pytest.raises(PortabilityError):
            service(db).export()


def test_rollback_is_best_effort_and_raises_the_original_error(tmp_path, monkeypatch):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        make_files(source.path.parent)
        archive = service(source).export()
        original = module.portability.import_tables
        root = target.path.parent
        competing = root / "documents/app-example/competing.txt"

        def fail(database, *args, **kwargs):
            if database is not target:
                return original(database, *args, **kwargs)
            competing.write_bytes(b"Fictional competing file")  # makes that rmdir fail
            raise PortabilityError("Fictional failure before commit.")

        monkeypatch.setattr(module.portability, "import_tables", fail)
        before = export_tables(target)
        with pytest.raises(PortabilityError, match="Fictional failure before commit"):
            service(target).restore(archive)
        assert export_tables(target) == before
        assert competing.read_bytes() == b"Fictional competing file"
        assert not (root / "documents/app-example/export-0001.pdf").exists()
        for gone in ("assets", "imports", "prompts"):  # later entries were still cleaned up
            assert not (root / gone).exists()


def test_sweep_restore_staging_removes_only_real_staging_directories(tmp_path):
    root = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_bytes(b"fictional outside data")
    leftover = root / ".restore-ab12cd"
    (leftover / "nested").mkdir(parents=True)
    (leftover / "app.db").write_bytes(b"fictional staged database")
    (leftover / "nested" / "link").symlink_to(outside, target_is_directory=True)
    (root / ".restore-link").symlink_to(outside, target_is_directory=True)
    (root / ".restore-file").write_bytes(b"fictional")
    (root / ".other").mkdir()
    (root / "imports").mkdir()
    (root / "imports" / "fictional.txt").write_bytes(b"fictional")
    module.sweep_restore_staging(root)
    assert not leftover.exists()
    assert (outside / "keep.txt").read_bytes() == b"fictional outside data"
    assert (root / ".restore-link").is_symlink()
    assert (root / ".restore-file").is_file()
    assert (root / ".other").is_dir() and (root / "imports/fictional.txt").is_file()
    module.sweep_restore_staging(tmp_path / "missing")  # best effort: nothing to do
    module.sweep_restore_staging(root / ".restore-file")


def test_nothing_but_the_sweep_removes_a_staging_folder(tmp_path):
    with fresh(tmp_path / "source") as source, fresh(tmp_path / "target") as target:
        make_files(source.path.parent)
        leftover = target.path.parent / ".restore-ab12cd"
        leftover.mkdir()
        (leftover / "app.db").write_bytes(b"fictional")
        assert service(target).can_restore()  # dot-directories at the top are not content
        archive = service(source).export()
        service(target).restore(archive)
        service(target).export()
        assert (leftover / "app.db").exists()
        module.sweep_restore_staging(target.path.parent)
        assert not leftover.exists()


def test_workspace_open_sweeps_leftover_restore_staging(tmp_path):
    from fastapi.testclient import TestClient

    import app.main as app_main
    from app.config import RuntimeConfig

    folder = tmp_path / "data"
    with fresh(folder):
        pass
    leftover = folder / ".restore-ab12cd"
    leftover.mkdir()
    (leftover / "app.db").write_bytes(b"fictional")
    config = RuntimeConfig(
        data_dir=folder,
        config_dir=tmp_path / "cfg",
        port=8000,
        log_level="INFO",
        demo=False,
        setup=False,
        no_browser=False,
        data_dir_source="cli",
    )
    app = app_main.create_app(config, clock=FrozenClock(START), migrations_dir=MIGRATIONS_DIR)
    with TestClient(app, base_url="http://127.0.0.1:8000"):
        assert app.state.workspace is not None
        assert not leftover.exists()


def test_export_ignores_volatile_runner_tables_between_reads(tmp_path, monkeypatch):
    with fresh(tmp_path) as db:
        original = module.portability.export_tables
        calls = 0

        def heartbeat(database, **kwargs):
            nonlocal calls
            snapshot = original(database, **kwargs)
            calls += 1
            if calls == 1:
                with database.transaction() as conn:
                    insert(conn, "tasks", task_row("task-0001"))
                    insert_usage(conn, "usage-0001", service="fake")
            return snapshot

        monkeypatch.setattr(module.portability, "export_tables", heartbeat)
        assert service(db).export().is_file()
        assert calls == 2


def test_export_refuses_a_settings_row_changed_between_reads(tmp_path, monkeypatch):
    with fresh(tmp_path) as db:
        original = module.portability.export_tables
        calls = 0

        def change_setting(database, **kwargs):
            nonlocal calls
            snapshot = original(database, **kwargs)
            calls += 1
            if calls == 1:
                with database.transaction() as conn:
                    insert(
                        conn,
                        "settings",
                        dict(key="fictional", value="{}", created_at="x", updated_at="x"),
                    )
            return snapshot

        monkeypatch.setattr(module.portability, "export_tables", change_setting)
        with pytest.raises(PortabilityError):
            service(db).export()
        assert not (tmp_path / "exports").exists()


def test_export_refuses_early_when_the_encoded_files_would_pass_the_cap(tmp_path, monkeypatch):
    with fresh(tmp_path) as db:
        (tmp_path / "imports").mkdir()
        (tmp_path / "imports" / "big.bin").write_bytes(b"\x07" * 3000)
        tables = len(json.dumps(export_tables(db), separators=(",", ":")))
        monkeypatch.setattr(module, "MAX_EXPORT_BYTES", tables + 3999)  # 4000 once encoded
        monkeypatch.setattr(
            module,
            "base64",
            SimpleNamespace(
                b64encode=lambda _data: pytest.fail("encoded before checking the size"),
                b64decode=base64.b64decode,
            ),
        )
        with pytest.raises(PortabilityError, match="larger than 200 MB"):
            service(db).export()
        assert not (tmp_path / "exports").exists()


def test_export_wraps_database_and_file_errors_without_paths(tmp_path, monkeypatch):
    with fresh(tmp_path) as db:

        def database_error(*_args, **_kwargs):
            raise sqlite3.OperationalError("fictional /secret/place failure")

        monkeypatch.setattr(module.portability, "export_tables", database_error)
        with pytest.raises(PortabilityError) as caught:
            service(db).export()
        assert "secret" not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__
        monkeypatch.undo()

        def file_error(_fd):
            raise OSError(5, "fictional", "/secret/place")

        monkeypatch.setattr(module, "_files", file_error)
        with pytest.raises(PortabilityError) as caught:
            service(db).export()
        assert "secret" not in str(caught.value)
        assert caught.value.__cause__ is None and caught.value.__suppress_context__


def test_workspace_folder_must_match_the_database_folder(tmp_path):
    with fresh(tmp_path / "source") as db:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        mismatched = DataPortabilityService(
            lambda: db, lambda: elsewhere, FrozenClock(START), migrations_dir=MIGRATIONS_DIR
        )
        archive = service(db).export()
        assert not mismatched.can_restore()
        with pytest.raises(PortabilityError):
            mismatched.export()
        with pytest.raises(PortabilityError):
            mismatched.download(archive.name)
        with pytest.raises(PortabilityError):
            mismatched.restore(archive)
        assert list(elsewhere.iterdir()) == []
        roundabout = elsewhere / ".." / "source"  # a different spelling of the same folder
        same = DataPortabilityService(
            lambda: db, lambda: roundabout, FrozenClock(START), migrations_dir=MIGRATIONS_DIR
        )
        assert same.download(archive.name) == archive.read_bytes()
