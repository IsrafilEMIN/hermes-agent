from __future__ import annotations

import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent import account_usage


class _FakePooledCredential:
    @classmethod
    def from_dict(cls, provider, row):
        return SimpleNamespace(
            provider=provider,
            id=row["id"],
            label=row.get("label", ""),
            priority=row["priority"],
            runtime_api_key=row.get("access_token", ""),
            runtime_base_url=row.get("base_url", ""),
        )


def _snapshot(provider="openai-codex", *, label=None, active=False, unavailable_reason=None, credential_id=None):
    return account_usage.AccountUsageSnapshot(
        provider=provider,
        source="usage_api",
        fetched_at=account_usage._utc_now(),
        account_label=label,
        active=active,
        credential_id=credential_id,
        windows=() if unavailable_reason else (account_usage.AccountUsageWindow("Session", 13.0),),
        unavailable_reason=unavailable_reason,
    )


class AggregateLabelPolicyTests(unittest.TestCase):
    """Unit tests for the safe-label sanitizer (is_safe_aggregate_account_label)."""

    def test_benign_human_labels_are_preserved(self):
        benign = [
            "work",
            "Codex 1",
            "My Work Account",
            "dev.2 (primary)",
            "home_office",
            "a",
            "A1",
            "123",
            "alice-laptop",
            "row 4 (2026)",
        ]
        for label in benign:
            self.assertTrue(
                account_usage.is_safe_aggregate_account_label(label),
                f"expected {label!r} to be safe",
            )

    def test_emails_and_at_signs_are_rejected(self):
        for label in ("person@example.com", "a@b", "@", "user @ host"):
            self.assertFalse(account_usage.is_safe_aggregate_account_label(label))

    def test_env_var_shaped_labels_are_rejected(self):
        for label in (
            "OPENCODE_GO_API_KEY",
            "OPENAI_API_KEY",
            "MY_TOKEN",
            "API_KEY",
            "SECRET_KEY",
            "ACCESS_TOKEN",
            "WORK_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
        ):
            self.assertFalse(
                account_usage.is_safe_aggregate_account_label(label),
                f"expected {label!r} to be rejected as env-var-shaped",
            )

    def test_token_and_jwt_shaped_labels_are_rejected(self):
        for label in (
            "sk-proj-abc123",
            "sk_abc123",
            "ghp_abcdefghijklmnopqrstuvwxyz1234567890",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.sig",
            "ya29.abcdefghijklmnopqrstuvwxyz",
            "-----BEGIN RSA PRIVATE KEY-----",
            "bearer",
            "token",
            "secret",
            "api_key",
            "access_token",
        ):
            self.assertFalse(
                account_usage.is_safe_aggregate_account_label(label),
                f"expected {label!r} to be rejected as token/JWT-shaped",
            )

    def test_uuid_and_hash_shaped_labels_are_rejected(self):
        self.assertFalse(
            account_usage.is_safe_aggregate_account_label("123e4567-e89b-12d3-a456-426614174000")
        )
        self.assertFalse(account_usage.is_safe_aggregate_account_label("a" * 64))
        self.assertFalse(account_usage.is_safe_aggregate_account_label("0f" * 32))

    def test_shape_and_encoding_edges_are_rejected(self):
        for label in (
            "",
            None,
            123,
            "x" * 49,
            " work",
            "work!",
            "work\n",
            "Über",
            "café",
            "work\x00",
            "work\taccount",
            ".",
            "-",
        ):
            self.assertFalse(
                account_usage.is_safe_aggregate_account_label(label),
                f"expected {label!r} to be rejected",
            )

    def test_internal_ids_are_rejected_by_display_label_helper(self):
        label = account_usage._aggregate_display_label(
            "abc123", provider="openai-codex", index=1, entry_id="abc123"
        )
        self.assertEqual(label, "OpenAI-Codex-1")


class AggregateRowMappingTests(unittest.TestCase):
    """Raw Codex row -> safe display name mapping (order survives, count-safe)."""

    def setUp(self):
        account_usage._clear_pool_account_usage_cache_for_tests()
        self.fake_pool_module = types.ModuleType("agent.credential_pool")
        self.fake_pool_module.PooledCredential = _FakePooledCredential

    def _rows(self):
        return [
            {
                "id": "entry-a",
                "priority": 0,
                "label": "person@example.com",
                "access_token": "synthetic-token-a",
                "base_url": "https://example.invalid/backend-api/codex",
            },
            {
                "id": "entry-b",
                "priority": 1,
                "label": "work",
                "access_token": "synthetic-token-b",
                "base_url": "https://example.invalid/backend-api/codex",
            },
            {
                "id": "entry-c",
                "priority": 2,
                "label": "OPENCODE_GO_API_KEY",
                "access_token": "synthetic-token-c",
                "base_url": "https://example.invalid/backend-api/codex",
            },
        ]

    def _labels(self, rows):
        with (
            patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            patch.object(account_usage, "read_credential_pool", return_value=rows),
        ):
            return account_usage._aggregate_codex_row_labels()

    def test_raw_rows_map_to_safe_labels_in_priority_order(self):
        mapped = self._labels(self._rows())
        self.assertEqual(
            [label for _entry_id, label in mapped],
            ["OpenAI-Codex-1", "work", "OpenAI-Codex-3"],
        )
        self.assertEqual([entry_id for entry_id, _label in mapped], ["entry-a", "entry-b", "entry-c"])

    def test_pool_read_failure_yields_empty_mapping(self):
        with patch.object(account_usage, "read_credential_pool", side_effect=RuntimeError("boom")):
            self.assertEqual(account_usage._aggregate_codex_row_labels(), ())

    def test_empty_pool_yields_empty_mapping(self):
        self.assertEqual(self._labels([]), ())

    def test_row_mapping_survives_snapshot_count_mismatch(self):
        rows = self._rows()[:2]  # two rows, but the pool fetch returns three snapshots
        with (
            patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            patch.object(account_usage, "read_credential_pool", return_value=rows),
            patch.object(
                account_usage,
                "fetch_pool_account_usage",
                return_value=(
                    _snapshot(label="Codex 1", credential_id="entry-a"),
                    _snapshot(label="Codex 2", credential_id="entry-b"),
                    _snapshot(label="Codex 3", credential_id="entry-extra"),
                ),
            ),
        ):
            snapshots = account_usage.fetch_aggregate_account_usage(providers=("openai-codex",))
        self.assertEqual(
            [snapshot.account_label for snapshot in snapshots],
            ["OpenAI-Codex-1", "work", "OpenAI-Codex-3"],
        )

    def test_snapshots_without_ids_fall_back_by_position(self):
        rows = self._rows()[:2]
        with (
            patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            patch.object(account_usage, "read_credential_pool", return_value=rows),
            patch.object(
                account_usage,
                "fetch_pool_account_usage",
                return_value=(
                    _snapshot(label=None, credential_id=None),
                    _snapshot(label=None, credential_id=None),
                ),
            ),
        ):
            snapshots = account_usage.fetch_aggregate_account_usage(providers=("openai-codex",))
        self.assertEqual(
            [snapshot.account_label for snapshot in snapshots],
            ["OpenAI-Codex-1", "work"],
        )


class AggregateFetchTests(unittest.TestCase):
    def setUp(self):
        account_usage._clear_pool_account_usage_cache_for_tests()

    def _run(self, fake_fetch, *, providers=None, fresh=None):
        """Run the aggregate with a stubbed pool read (no real auth store)."""
        kwargs = {}
        if providers is not None:
            kwargs["providers"] = providers
        if fresh is not None:
            kwargs["fresh"] = fresh
        with (
            patch.object(account_usage, "read_credential_pool", return_value=[]),
            patch.object(account_usage, "fetch_pool_account_usage", side_effect=fake_fetch),
        ):
            return account_usage.fetch_aggregate_account_usage(**kwargs)

    def test_defaults_to_all_providers_and_fresh(self):
        calls = []

        def fake_fetch(provider, *, fresh=False):
            calls.append((provider, fresh))
            if provider == "openai-codex":
                return (
                    _snapshot(label="Codex 1", active=True, credential_id="entry-a"),
                    _snapshot(label="Codex 2", credential_id="entry-b"),
                )
            if provider == "opencode-go":
                return (_snapshot(provider="opencode-go", active=True),)
            return ()

        snapshots = self._run(fake_fetch)

        self.assertEqual(
            calls, [("openai-codex", True), ("opencode-go", True)]
        )
        self.assertEqual(
            [snapshot.account_label for snapshot in snapshots],
            ["OpenAI-Codex-1", "OpenAI-Codex-2", "OpenCode-Go-1"],
        )
        self.assertEqual([snapshot.active for snapshot in snapshots], [True, False, True])

    def test_providers_are_deduped_and_filtered(self):
        calls = []

        def fake_fetch(provider, *, fresh=False):
            calls.append(provider)
            if provider == "openai-codex":
                return (_snapshot(credential_id="entry-a"),)
            return ()

        snapshots = self._run(
            fake_fetch,
            providers=("openai-codex", "openai-codex", "OPENAI-CODEX", "openai-codex"),
            fresh=False,
        )
        self.assertEqual(calls, ["openai-codex"])
        self.assertEqual(len(snapshots), 1)

    def test_empty_providers_returns_empty(self):
        self.assertEqual(account_usage.fetch_aggregate_account_usage(providers=()), ())
        self.assertEqual(account_usage.fetch_aggregate_account_usage(providers=("", " ")), ())

    def test_per_provider_exception_yields_unavailable_snapshot(self):
        def fake_fetch(provider, *, fresh=False):
            if provider == "opencode-go":
                raise RuntimeError("synthetic go failure")
            return (_snapshot(credential_id="entry-a", active=True),)

        snapshots = self._run(fake_fetch)

        self.assertEqual(len(snapshots), 2)
        self.assertTrue(snapshots[0].available)
        self.assertFalse(snapshots[1].available)
        self.assertEqual(snapshots[1].provider, "opencode-go")
        self.assertEqual(snapshots[1].account_label, "OpenCode-Go-1")
        self.assertNotIn("synthetic", snapshots[1].unavailable_reason or "")

    def test_opencode_go_label_is_never_the_env_var_name(self):
        snapshots = self._run(
            lambda provider, *, fresh=False: (
                _snapshot(provider="opencode-go", active=True),
            ),
            providers=("opencode-go",),
        )
        self.assertEqual(snapshots[0].account_label, "OpenCode-Go-1")
        self.assertNotEqual(snapshots[0].account_label, "OPENCODE_GO_API_KEY")

    def test_serialized_payload_is_secret_safe(self):
        rows = [
            {
                "id": "entry-a",
                "priority": 0,
                "label": "person@example.com",
                "access_token": "synthetic-token-a",
                "base_url": "https://example.invalid/backend-api/codex",
            }
        ]
        fake_pool_module = types.ModuleType("agent.credential_pool")
        fake_pool_module.PooledCredential = _FakePooledCredential
        with (
            patch.dict(sys.modules, {"agent.credential_pool": fake_pool_module}),
            patch.object(account_usage, "read_credential_pool", return_value=rows),
            patch.object(
                account_usage,
                "fetch_pool_account_usage",
                return_value=(
                    _snapshot(label="Codex 1", active=True, credential_id="entry-a"),
                    _snapshot(provider="opencode-go", active=True),
                ),
            ),
        ):
            snapshots = account_usage.fetch_aggregate_account_usage()

        rendered = repr(
            [account_usage.account_usage_snapshot_to_dict(snapshot) for snapshot in snapshots]
        )
        for secret in (
            "entry-a",
            "credential_id",
            "synthetic-token",
            "person@example.com",
            "OPENCODE_GO_API_KEY",
            "@",
        ):
            self.assertNotIn(secret, rendered)
        self.assertEqual(snapshots[0].account_label, "OpenAI-Codex-1")

    def test_fresh_flag_is_passed_through(self):
        calls = []

        def fake_fetch(provider, *, fresh=False):
            calls.append(fresh)
            return ()

        self._run(fake_fetch, fresh=False)
        self._run(fake_fetch, fresh=True)
        self.assertEqual(calls, [False, False, True, True])


if __name__ == "__main__":
    unittest.main()
