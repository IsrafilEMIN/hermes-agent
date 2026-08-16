from __future__ import annotations

import base64
import json
import sys
import threading
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
            priority=row["priority"],
            runtime_api_key=row.get("access_token", ""),
            runtime_base_url=row.get("base_url", ""),
        )


class PoolAccountUsageTests(unittest.TestCase):
    def setUp(self):
        account_usage._clear_pool_account_usage_cache_for_tests()
        self.rows = [
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
                "label": "another@example.com",
                "access_token": "synthetic-token-b",
                "base_url": "https://example.invalid/backend-api/codex",
            },
        ]
        self.fake_pool_module = types.ModuleType("agent.credential_pool")
        self.fake_pool_module.PooledCredential = _FakePooledCredential

    def _run(self, fetcher, *, active_entry_id="entry-b", fresh=False, rows=None):
        with (
            patch.dict(sys.modules, {"agent.credential_pool": self.fake_pool_module}),
            patch.object(
                account_usage,
                "read_credential_pool",
                return_value=self.rows if rows is None else rows,
            ),
            patch.object(account_usage, "_fetch_codex_account_usage_with_credentials", side_effect=fetcher),
        ):
            return account_usage.fetch_pool_account_usage(
                "openai-codex",
                active_entry_id=active_entry_id,
                fresh=fresh,
            )

    def test_two_entries_are_bound_isolated_and_secret_safe(self):
        calls = []
        lock = threading.Lock()

        def fetcher(token, base_url, account_id=None):
            with lock:
                calls.append((token, base_url, account_id))
            used = 13.0 if token.endswith("a") else 58.0
            return account_usage.AccountUsageSnapshot(
                provider="openai-codex",
                source="usage_api",
                fetched_at=account_usage._utc_now(),
                windows=(account_usage.AccountUsageWindow("Session", used_percent=used),),
            )

        snapshots = self._run(fetcher, fresh=True)
        self.assertEqual(len(snapshots), 2)
        self.assertEqual({call[0] for call in calls}, {"synthetic-token-a", "synthetic-token-b"})
        self.assertTrue(all(call[2] is None for call in calls))
        self.assertEqual([item.account_label for item in snapshots], ["Codex 1", "Codex 2"])
        self.assertEqual([item.active for item in snapshots], [False, True])

        payload = [account_usage.account_usage_snapshot_to_dict(item) for item in snapshots]
        rendered = repr(payload)
        for secret in ("entry-a", "entry-b", "synthetic-token", "person@example.com", "another@example.com"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(payload[0]["windows"][0]["used_percent"], 13.0)
        self.assertEqual(payload[1]["windows"][0]["used_percent"], 58.0)

    def test_one_failure_does_not_hide_the_other(self):
        def fetcher(token, _base_url, _account_id=None):
            if token.endswith("a"):
                raise RuntimeError("synthetic-token-a must never leak")
            return account_usage.AccountUsageSnapshot(
                provider="openai-codex",
                source="usage_api",
                fetched_at=account_usage._utc_now(),
                details=("available",),
            )

        snapshots = self._run(fetcher, fresh=True)
        self.assertEqual(len(snapshots), 2)
        self.assertFalse(snapshots[0].available)
        self.assertTrue(snapshots[1].available)
        self.assertNotIn("synthetic-token", snapshots[0].unavailable_reason or "")

    def test_each_jwt_binds_its_own_chatgpt_account_id(self):
        def jwt(account_id):
            payload = base64.urlsafe_b64encode(
                json.dumps(
                    {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
                ).encode()
            ).decode().rstrip("=")
            return f"header.{payload}.signature"

        account_ids = ("acct-synthetic-a", "acct-synthetic-b")
        self.rows[0]["access_token"] = jwt(account_ids[0])
        self.rows[1]["access_token"] = jwt(account_ids[1])
        calls = []

        def fetcher(_token, _base_url, account_id=None):
            calls.append(account_id)
            return account_usage.AccountUsageSnapshot(
                provider="openai-codex",
                source="usage_api",
                fetched_at=account_usage._utc_now(),
                details=("available",),
            )

        snapshots = self._run(fetcher, fresh=True)

        self.assertCountEqual(calls, account_ids)
        rendered = repr(
            [account_usage.account_usage_snapshot_to_dict(snapshot) for snapshot in snapshots]
        )
        for account_id in account_ids:
            self.assertNotIn(account_id, rendered)

    def test_codex_window_labels_follow_known_durations(self):
        label = account_usage._codex_window_label
        self.assertEqual(label("primary_window", {"limit_window_seconds": 18_000}), "Session")
        self.assertEqual(label("primary_window", {"limit_window_seconds": 604_800}), "Weekly")
        self.assertEqual(label("secondary_window", {"limit_window_seconds": 18_000}), "Session")
        self.assertEqual(label("secondary_window", {"limit_window_seconds": 604_800}), "Weekly")
        self.assertEqual(label("primary_window", {"limit_seconds": 604_800}), "Weekly")
        self.assertEqual(label("primary_window", {"limit_window_seconds": 86_400}), "Session")
        self.assertEqual(label("secondary_window", {}), "Weekly")

    def test_cache_is_keyed_by_entry_id(self):
        calls = []

        def fetcher(token, _base_url, _account_id=None):
            calls.append(token)
            return account_usage.AccountUsageSnapshot(
                provider="openai-codex",
                source="usage_api",
                fetched_at=account_usage._utc_now(),
                details=(token[-1],),
            )

        first = self._run(fetcher, active_entry_id="entry-a", fresh=True)
        # Cache-only second pass: entry-a is served from its cache entry,
        # entry-b is fetched — the cache is keyed per entry id.
        second = self._run(fetcher, active_entry_id="entry-b", fresh=False)
        self.assertCountEqual(calls, ["synthetic-token-a", "synthetic-token-b"])
        self.assertEqual(first[0].details, ("a",))
        self.assertEqual(first[1].details, ("b",))
        self.assertEqual([item.active for item in second], [False, True])

    def test_non_codex_provider_is_not_enumerated(self):
        self.assertEqual(account_usage.fetch_pool_account_usage("anthropic"), ())
        self.assertEqual(
            account_usage.fetch_pool_account_usage("anthropic", fresh=True), ()
        )

    def test_fresh_bypasses_stale_cache_and_refreshes_it(self):
        calls = []

        def fetcher(token, _base_url, _account_id=None):
            calls.append(token)
            used = 13.0 if token.endswith("a") else 58.0
            return account_usage.AccountUsageSnapshot(
                provider="openai-codex",
                source="usage_api",
                fetched_at=account_usage._utc_now(),
                windows=(account_usage.AccountUsageWindow("Session", used_percent=used),),
            )

        # Warm the 60s per-entry cache with a normal (cached) enumeration.
        first = self._run(fetcher, fresh=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual([item.windows[0].used_percent for item in first], [13.0, 58.0])

        # A fresh enumeration must hit the backend again for EVERY entry,
        # ignoring the still-warm cache.
        def fresh_fetcher(token, _base_url, _account_id=None):
            calls.append("fresh:" + token)
            used = 21.0 if token.endswith("a") else 64.0
            return account_usage.AccountUsageSnapshot(
                provider="openai-codex",
                source="usage_api",
                fetched_at=account_usage._utc_now(),
                windows=(account_usage.AccountUsageWindow("Session", used_percent=used),),
            )

        refreshed = self._run(fresh_fetcher, fresh=True)
        self.assertEqual(
            [item.windows[0].used_percent for item in refreshed], [21.0, 64.0]
        )
        self.assertCountEqual(
            calls,
            [
                "synthetic-token-a",
                "synthetic-token-b",
                "fresh:synthetic-token-a",
                "fresh:synthetic-token-b",
            ],
        )

        # …and the fresh result replaced the stale cache entry: a later cached
        # enumeration makes NO new backend calls and reports the new numbers.
        after = self._run(fetcher)
        self.assertEqual(
            [item.windows[0].used_percent for item in after], [21.0, 64.0]
        )
        self.assertEqual(len(calls), 4)

    def test_fresh_preserves_failure_isolation(self):
        def fetcher(token, _base_url, _account_id=None):
            used = 13.0 if token.endswith("a") else 58.0
            return account_usage.AccountUsageSnapshot(
                provider="openai-codex",
                source="usage_api",
                fetched_at=account_usage._utc_now(),
                windows=(account_usage.AccountUsageWindow("Session", used_percent=used),),
            )

        self._run(fetcher, fresh=True)  # warm the cache

        def fresh_fetcher(token, _base_url, _account_id=None):
            if token.endswith("a"):
                raise RuntimeError("synthetic fresh failure on entry-a")
            return account_usage.AccountUsageSnapshot(
                provider="openai-codex",
                source="usage_api",
                fetched_at=account_usage._utc_now(),
                windows=(account_usage.AccountUsageWindow("Session", used_percent=77.0),),
            )

        snapshots = self._run(fresh_fetcher, fresh=True)
        # One entry's fresh failure must not hide the other entry's fresh data.
        self.assertFalse(snapshots[0].available)
        self.assertNotIn("synthetic", snapshots[0].unavailable_reason or "")
        self.assertEqual(snapshots[1].windows[0].used_percent, 77.0)

    # ── Cache-only default (fresh=False never fetches) ──────────────────────

    def _codex_snapshot(self, *, used=13.0):
        return account_usage.AccountUsageSnapshot(
            provider="openai-codex",
            source="usage_api",
            fetched_at=account_usage._utc_now(),
            windows=(account_usage.AccountUsageWindow("Session", used_percent=used),),
        )

    def test_default_cold_cache_fetches_nothing(self):
        def boom(*_args, **_kwargs):
            self.fail("cache-only default must never perform a fetch")

        self.assertEqual(self._run(boom), ())

    def test_default_warm_cache_served_without_fetch(self):
        calls = []

        def fetcher(token, _base_url, _account_id=None):
            calls.append(token)
            return self._codex_snapshot(used=float(len(token)))

        warmed = self._run(fetcher, fresh=True)
        self.assertEqual(len(calls), 2)
        calls.clear()

        served = self._run(lambda *_a, **_k: self.fail("must not fetch"))
        self.assertEqual([s.credential_id for s in served], ["entry-a", "entry-b"])
        self.assertEqual(
            [s.windows[0].used_percent for s in served],
            [s.windows[0].used_percent for s in warmed],
        )
        self.assertEqual(calls, [])

    def test_fresh_still_fetches_with_warm_cache(self):
        calls = []

        def fetcher(token, _base_url, _account_id=None):
            calls.append(token)
            return self._codex_snapshot(used=float(len(token)))

        self._run(fetcher, fresh=True)  # warm the cache
        calls.clear()

        served = self._run(fetcher, fresh=True)
        self.assertEqual(len(calls), 2, "fresh=True must bypass the cache and fetch")
        self.assertEqual([s.credential_id for s in served], ["entry-a", "entry-b"])

    def test_default_skips_missing_rows_keeps_ordinals(self):
        def fetcher(token, _base_url, _account_id=None):
            return self._codex_snapshot(used=float(len(token)))

        # Warm ONLY entry-a (its row alone), then enumerate the FULL pool
        # with the cache-only default: entry-b is a cache miss and must be
        # skipped, and the surviving ordinal keeps its position among ALL
        # visible rows.
        warmed = self._run(fetcher, fresh=True, rows=[self.rows[0]])
        self.assertEqual([s.credential_id for s in warmed], ["entry-a"])

        served = self._run(
            lambda *_a, **_k: self.fail("must not fetch on miss"),
        )
        self.assertEqual([s.credential_id for s in served], ["entry-a"])
        self.assertEqual([s.account_label for s in served], ["Codex 1"])


if __name__ == "__main__":
    unittest.main()
