"""Read-only environment secret store (M1.3; AC-M1.5 part; spec 2.4, 2.9.9, 7.3; ADR-044).

``EnvSecretStore`` is the fallback when no keychain is usable: it maps the known secret keys
to ``ANTHROPIC_API_KEY`` and ``CLAUDE_CODE_OAUTH_TOKEN`` in an injected mapping (never the
real process environment), reads it at call time, and refuses every write.

All values are fictional and built at runtime so secret scanners stay quiet.
"""

import traceback

import pytest

from app.domain.errors import SecretStoreUnavailable
from app.modules.secrets import ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, KNOWN_SECRET_KEYS
from app.modules.secrets.providers.env import ENVIRONMENT_VARIABLES, EnvSecretStore

FAKE_SECRET = "fake-" + "secret-" + "x" * 8
OTHER_SECRET = "fake-" + "secret-" + "y" * 8


def _error_texts(error: SecretStoreUnavailable) -> dict[str, str]:
    return {
        "str": str(error),
        "repr": repr(error),
        "args": repr(error.args),
        "user_message": error.user_message,
        "traceback": "".join(traceback.format_exception(error)),
    }


def test_env_store_is_read_only_set_raises() -> None:
    env = {"ANTHROPIC_API_KEY": OTHER_SECRET}
    store = EnvSecretStore(env)

    with pytest.raises(SecretStoreUnavailable) as raised_set:
        store.set(ANTHROPIC_API_KEY, FAKE_SECRET)
    with pytest.raises(SecretStoreUnavailable) as raised_delete:
        store.delete(ANTHROPIC_API_KEY)

    for error in (raised_set.value, raised_delete.value):
        for name, text in _error_texts(error).items():
            assert FAKE_SECRET not in text, name
            assert OTHER_SECRET not in text, name
        assert error.__cause__ is None
    assert env == {"ANTHROPIC_API_KEY": OTHER_SECRET}  # the environment is never written
    assert store.get(ANTHROPIC_API_KEY) == OTHER_SECRET


def test_env_store_maps_keys_to_variables() -> None:
    assert dict(ENVIRONMENT_VARIABLES) == {
        ANTHROPIC_API_KEY: "ANTHROPIC_API_KEY",
        CLAUDE_CODE_OAUTH_TOKEN: "CLAUDE_CODE_OAUTH_TOKEN",
    }
    # Secret key names (not values): the keychain item names stay stable across releases.
    assert KNOWN_SECRET_KEYS == ("anthropic_api_key", "claude_code_oauth_token")
    assert KNOWN_SECRET_KEYS == (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN)

    store = EnvSecretStore(
        {
            "ANTHROPIC_API_KEY": FAKE_SECRET,
            "CLAUDE_CODE_OAUTH_TOKEN": OTHER_SECRET,
            "UNRELATED_VARIABLE": "fake-unrelated",
        }
    )

    assert store.get(ANTHROPIC_API_KEY) == FAKE_SECRET
    assert store.get(CLAUDE_CODE_OAUTH_TOKEN) == OTHER_SECRET
    assert store.get("unknown_key") is None
    assert store.get("UNRELATED_VARIABLE") is None  # only mapped keys, never raw names
    assert store.get("ANTHROPIC_API_KEY") is None  # keys are the lower-case secret keys


def test_env_store_ignores_empty_and_strips_whitespace() -> None:
    store = EnvSecretStore({"ANTHROPIC_API_KEY": "", "CLAUDE_CODE_OAUTH_TOKEN": "  \t\n "})
    assert store.get(ANTHROPIC_API_KEY) is None
    assert store.get(CLAUDE_CODE_OAUTH_TOKEN) is None

    padded = EnvSecretStore(
        {"ANTHROPIC_API_KEY": f"  {FAKE_SECRET}\n", "CLAUDE_CODE_OAUTH_TOKEN": f"\t{OTHER_SECRET} "}
    )
    assert padded.get(ANTHROPIC_API_KEY) == FAKE_SECRET
    assert padded.get(CLAUDE_CODE_OAUTH_TOKEN) == OTHER_SECRET

    assert EnvSecretStore({}).get(ANTHROPIC_API_KEY) is None


def test_env_store_reads_mapping_at_call_time() -> None:
    env: dict[str, str] = {}
    store = EnvSecretStore(env)
    assert store.get(ANTHROPIC_API_KEY) is None

    env["ANTHROPIC_API_KEY"] = FAKE_SECRET
    assert store.get(ANTHROPIC_API_KEY) == FAKE_SECRET

    env["ANTHROPIC_API_KEY"] = OTHER_SECRET
    assert store.get(ANTHROPIC_API_KEY) == OTHER_SECRET

    del env["ANTHROPIC_API_KEY"]
    assert store.get(ANTHROPIC_API_KEY) is None


def test_env_store_is_not_available() -> None:
    store = EnvSecretStore({"ANTHROPIC_API_KEY": FAKE_SECRET})

    assert store.available is False
    assert store.get(ANTHROPIC_API_KEY) == FAKE_SECRET  # still readable
