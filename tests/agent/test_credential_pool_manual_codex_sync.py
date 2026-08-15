from types import SimpleNamespace
from unittest.mock import patch

from agent.credential_pool import CredentialPool


def test_manual_codex_entry_never_adopts_singleton_auth_tokens():
    pool = SimpleNamespace(provider="openai-codex")
    entry = SimpleNamespace(source="manual:device_code")

    with patch(
        "agent.credential_pool._load_auth_store",
        side_effect=AssertionError("manual entry must not read singleton auth state"),
    ):
        result = CredentialPool._sync_codex_entry_from_auth_store(pool, entry)

    assert result is entry