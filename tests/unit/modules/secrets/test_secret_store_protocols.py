"""Static conformance of the M1.3 secret stores to ``SecretStore`` (spec 2.4; ADR-036).

Checked with mypy --strict, the way test_keyring_store.py checks the keyring adapter, so a
store that drifts from the protocol (a missing method, a wrong signature) fails here even
if no runtime test happens to call that method.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]

CONFORMANCE = """\
from app.domain.redaction import Redactor
from app.modules.secrets import SecretLookup, SecretSource, SecretStore
from app.modules.secrets.providers.chained import ChainedSecretStore
from app.modules.secrets.providers.env import EnvSecretStore
from app.modules.secrets.providers.keyring_store import KeyringSecretStore
from app.modules.secrets.providers.memory import MemorySecretStore
from app.modules.secrets.providers.redacting import RedactingSecretStore

memory: SecretStore = MemorySecretStore()
unavailable: SecretStore = MemorySecretStore(available=False)
env: SecretStore = EnvSecretStore({})
chained_store = ChainedSecretStore(KeyringSecretStore(), EnvSecretStore({}))
chained: SecretStore = chained_store
redacting: SecretStore = RedactingSecretStore(chained, Redactor())
registering = ChainedSecretStore(KeyringSecretStore(), EnvSecretStore({}), redactor=Redactor())
registering_store: SecretStore = registering
looked_up: SecretLookup = registering.lookup("anthropic_api_key")
source: SecretSource | None = chained_store.source("anthropic_api_key")
"""


def test_secret_stores_satisfy_protocol(tmp_path: Path) -> None:
    sample = tmp_path / "conformance.py"
    sample.write_text(CONFORMANCE, encoding="utf-8")
    # Fixed argv to the venv's own interpreter; no shell, no untrusted input.
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            str(sample),
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "MYPYPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
