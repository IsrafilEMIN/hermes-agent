from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

import httpx

from agent.anthropic_adapter import _is_oauth_token, resolve_anthropic_token
from hermes_cli.auth import (
    AuthError,
    _decode_jwt_claims,
    _read_codex_tokens,
    is_source_suppressed,
    read_credential_pool,
    resolve_codex_runtime_credentials,
)
from hermes_cli.runtime_provider import resolve_runtime_provider

if TYPE_CHECKING:
    from typing import TypeGuard

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AccountUsageWindow:
    label: str
    used_percent: Optional[float] = None
    reset_at: Optional[datetime] = None
    detail: Optional[str] = None


@dataclass(frozen=True)
class AccountUsageSnapshot:
    provider: str
    source: str
    fetched_at: datetime
    title: str = "Account limits"
    plan: Optional[str] = None
    windows: tuple[AccountUsageWindow, ...] = ()
    details: tuple[str, ...] = ()
    unavailable_reason: Optional[str] = None
    # Pool-aware metadata (optional, safe display/internal fields only). Never
    # populated by the single-account fetch paths, so those snapshots are
    # byte-identical to before. ``credential_id`` is an INTERNAL correlation
    # id — it must never be emitted by secret-safe serializers.
    credential_id: Optional[str] = None
    account_label: Optional[str] = None
    active: bool = False

    @property
    def available(self) -> bool:
        return bool(self.windows or self.details) and not self.unavailable_reason


def _title_case_slug(value: Optional[str]) -> Optional[str]:
    cleaned = str(value or "").strip()
    if not cleaned:
        return None
    return cleaned.replace("_", " ").replace("-", " ").title()


def _parse_dt(value: Any) -> Optional[datetime]:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _format_reset(dt: Optional[datetime]) -> str:
    if not dt:
        return "unknown"
    local_dt = dt.astimezone()
    delta = dt - _utc_now()
    total_seconds = int(delta.total_seconds())
    if total_seconds <= 0:
        return f"now ({local_dt.strftime('%Y-%m-%d %H:%M %Z')})"
    hours, rem = divmod(total_seconds, 3600)
    minutes = rem // 60
    if hours >= 24:
        days, hours = divmod(hours, 24)
        rel = f"in {days}d {hours}h"
    elif hours > 0:
        rel = f"in {hours}h {minutes}m"
    else:
        rel = f"in {minutes}m"
    return f"{rel} ({local_dt.strftime('%Y-%m-%d %H:%M %Z')})"


def render_account_usage_lines(snapshot: Optional[AccountUsageSnapshot], *, markdown: bool = False) -> list[str]:
    if not snapshot:
        return []
    header = f"📈 {'**' if markdown else ''}{snapshot.title}{'**' if markdown else ''}"
    lines = [header]
    if snapshot.plan:
        lines.append(f"Provider: {snapshot.provider} ({snapshot.plan})")
    else:
        lines.append(f"Provider: {snapshot.provider}")
    for window in snapshot.windows:
        if window.used_percent is None:
            base = f"{window.label}: unavailable"
        else:
            remaining = max(0, round(100 - float(window.used_percent)))
            used = max(0, round(float(window.used_percent)))
            base = f"{window.label}: {remaining}% remaining ({used}% used)"
        if window.reset_at:
            base += f" • resets {_format_reset(window.reset_at)}"
        elif window.detail:
            base += f" • {window.detail}"
        lines.append(base)
    for detail in snapshot.details:
        lines.append(detail)
    if snapshot.unavailable_reason:
        lines.append(f"Unavailable: {snapshot.unavailable_reason}")
    return lines


def _fmt_usd(d: float) -> str:
    return f"${d:,.2f}"


def _is_finite_num(v: Any) -> TypeGuard[float]:
    """True iff v is a real numeric value (int or float, not bool, not NaN/Inf).

    Typed as a ``TypeGuard[float]`` so the type checker narrows ``v`` to a real
    number in the positive branch — callers can then do arithmetic / pass it to
    ``_fmt_usd`` without a None-operand warning.
    """
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def build_nous_credits_snapshot(account_info) -> Optional[AccountUsageSnapshot]:
    """Map a NousPortalAccountInfo into an AccountUsageSnapshot for /usage.

    Shows dollar magnitudes (subscription / top-up / total) + renewal date + a
    portal CTA. When the portal supplies a subscription denominator
    (``monthly_credits``), also emits a subscription-usage window so the renderer
    shows a real ``% used`` gauge; when it's absent (older portals) the view
    gracefully degrades to magnitudes-only. Returns None when there's no usable
    account info to show (fail-open: caller just shows nothing).
    """
    try:
        from hermes_cli.nous_account import nous_portal_topup_url

        if account_info is None or not getattr(account_info, "logged_in", False):
            return None

        access = getattr(account_info, "paid_service_access_info", None)
        sub = getattr(account_info, "subscription", None)

        windows: list[AccountUsageWindow] = []
        details: list[str] = []

        # Subscription usage gauge — only when the portal supplies a positive
        # monthly_credits denominator AND a finite remaining balance that does
        # not exceed the cap. Money math is on float dollars (allowed: numeric
        # account fields, NOT a server-provided *_usd string). used = cap -
        # remaining; clamp [0,100] so a debt balance (remaining < 0) reads 100%.
        # Excluded on purpose:
        #   - non-finite values (NaN/Infinity slip past isinstance and json.loads
        #     parses bare NaN/Infinity by default) → would render "$nan"/"$inf"
        #     and a falsely-confident gauge;
        #   - remaining > cap (rollover balance spanning the period) → monthly_credits
        #     is no longer a meaningful denominator, and "$X of $Y left" with X>Y
        #     reads as a contradiction. Both fall back to the magnitudes lines.
        if sub is not None:
            monthly_credits = getattr(sub, "monthly_credits", None)
            sub_remaining = getattr(sub, "credits_remaining", None)
            if (
                _is_finite_num(monthly_credits)
                and monthly_credits > 0
                and _is_finite_num(sub_remaining)
                and sub_remaining <= monthly_credits
            ):
                used = monthly_credits - sub_remaining
                used_pct = max(0.0, min(100.0, used / monthly_credits * 100.0))
                windows.append(
                    AccountUsageWindow(
                        label="Subscription",
                        used_percent=used_pct,
                        detail=f"{_fmt_usd(sub_remaining)} of {_fmt_usd(monthly_credits)} left",
                    )
                )

        if access is not None:
            sub_credits = getattr(access, "subscription_credits_remaining", None)
            if _is_finite_num(sub_credits):
                details.append(f"Subscription credits: {_fmt_usd(sub_credits)}")
            purchased = getattr(access, "purchased_credits_remaining", None)
            if _is_finite_num(purchased):
                details.append(f"Top-up credits: {_fmt_usd(purchased)}")
            total_usable = getattr(access, "total_usable_credits", None)
            if _is_finite_num(total_usable):
                details.append(f"Total usable: {_fmt_usd(total_usable)}")

        if sub is not None:
            rollover = getattr(sub, "rollover_credits", None)
            if _is_finite_num(rollover) and rollover > 0:
                details.append(f"Rollover: {_fmt_usd(rollover)}")
            period_end = getattr(sub, "current_period_end", None)
            if period_end:
                details.append(f"Renews: {period_end}")

        paid = getattr(account_info, "paid_service_access", None)
        if paid is False:
            details.append("Status: access depleted — top up to restore")

        if not windows and not details:
            return None

        details.append(f"Top up: {nous_portal_topup_url(account_info)}")
        details.append("(or run /topup)")

        plan = getattr(sub, "plan", None) if sub is not None else None
        return AccountUsageSnapshot(
            provider="nous",
            source="portal-account",
            fetched_at=_utc_now(),
            title="Nous credits",
            plan=plan,
            windows=tuple(windows),
            details=tuple(details),
        )
    except (AttributeError, TypeError):
        return None


def nous_credits_lines(*, markdown: bool = False, timeout: float = 10.0) -> list[str]:
    """Return rendered Nous-credits /usage lines, or [] when there's nothing to show.

    Account-independent of any live agent: gated on "a Nous account is logged in"
    (a cheap local auth-state check), then a wall-clock-bounded portal fetch. Shared
    by the CLI ``_show_usage`` and the TUI ``session.usage`` RPC so both surfaces show
    the same block regardless of session API-call count or resume state. Fail-open:
    any auth/portal hiccup or timeout returns [] (the caller shows nothing).

    Dev override: when HERMES_DEV_CREDITS_FIXTURE selects a fixture state, /usage
    renders from that fixture instead of the real portal (so the block + gauge are
    testable without a live account). Throwaway scaffolding.
    """
    # Dev fixture short-circuit — render /usage from the injected state, no portal.
    try:
        from agent.credits_tracker import dev_fixture_credits_state

        fixture = dev_fixture_credits_state()
    except Exception:
        fixture = None
    if fixture is not None:
        snapshot = _snapshot_from_credits_state(fixture)
        return render_account_usage_lines(snapshot, markdown=markdown)

    try:
        from hermes_cli.auth import get_provider_auth_state

        tok = (get_provider_auth_state("nous") or {}).get("access_token")
        if not (isinstance(tok, str) and tok.strip()):
            return []
    except Exception:
        return []
    try:
        import concurrent.futures

        from hermes_cli.nous_account import get_nous_portal_account_info

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            account = pool.submit(
                get_nous_portal_account_info, force_fresh=True
            ).result(timeout=timeout)
        snapshot = build_nous_credits_snapshot(account)
        return render_account_usage_lines(snapshot, markdown=markdown)
    except Exception:
        # Fail-open (caller shows nothing), but leave a breadcrumb so a dead
        # /usage credits block is diagnosable in agent.log without a dev flag.
        logger.debug("credits ▸ /usage portal fetch/render failed (fail-open)", exc_info=True)
        return []


def _snapshot_from_credits_state(state) -> Optional[AccountUsageSnapshot]:
    """Map a header-shaped CreditsState (e.g. a dev fixture) to the /usage snapshot.

    Renders the same magnitudes + monthly-grant % window the portal path produces,
    so HERMES_DEV_CREDITS_FIXTURE can exercise /usage without a live account. The
    *_usd strings are mock display values here (not server balance to compute on);
    the % comes from CreditsState.used_fraction (micros math). Fail-open → None.
    """
    try:
        if state is None:
            return None

        windows: list[AccountUsageWindow] = []
        details: list[str] = []

        uf = getattr(state, "used_fraction", None)
        if isinstance(uf, (int, float)) and math.isfinite(uf):
            cap_usd = getattr(state, "subscription_limit_usd", None)
            sub_usd = getattr(state, "subscription_usd", None)
            detail = None
            if sub_usd and cap_usd:
                detail = f"${sub_usd} of ${cap_usd} left"
            windows.append(
                AccountUsageWindow(
                    label="Subscription",
                    used_percent=max(0.0, min(100.0, uf * 100.0)),
                    detail=detail,
                )
            )

        sub_usd = getattr(state, "subscription_usd", None)
        if sub_usd:
            details.append(f"Subscription credits: ${sub_usd}")
        purchased_usd = getattr(state, "purchased_usd", None)
        if purchased_usd:
            details.append(f"Top-up credits: ${purchased_usd}")
        remaining_usd = getattr(state, "remaining_usd", None)
        if remaining_usd:
            details.append(f"Total usable: ${remaining_usd}")
        if getattr(state, "paid_access", True) is False:
            details.append("Status: access depleted — top up to restore")

        if not windows and not details:
            return None

        details.append("(dev fixture — HERMES_DEV_CREDITS_FIXTURE)")
        return AccountUsageSnapshot(
            provider="nous",
            source="dev-fixture",
            fetched_at=_utc_now(),
            title="Nous credits",
            windows=tuple(windows),
            details=tuple(details),
        )
    except (AttributeError, TypeError):
        return None


@dataclass(frozen=True)
class CreditsView:
    """Surface-agnostic data for the ``/topup`` balance view.

    One portal fetch, one parse — consumed identically by the CLI panel, the
    gateway button, and any other money surface. Fail-open: when not logged in
    or the portal is unreachable, ``logged_in`` is False / ``topup_url`` is None
    and callers degrade gracefully.
    """

    logged_in: bool
    balance_lines: tuple[str, ...] = ()
    identity_line: Optional[str] = None
    topup_url: Optional[str] = None
    depleted: bool = False


def build_credits_view(*, markdown: bool = False, timeout: float = 10.0) -> CreditsView:
    """Build the /topup balance view: balance block + identity line + top-up URL.

    Reuses the same account fetch + snapshot + URL builder as the /usage credits
    block, so the numbers always match. The balance block is the rendered
    snapshot MINUS its trailing top-up/command-hint lines (the /topup surface
    supplies its own affordance). Fail-open → ``CreditsView(logged_in=False)``.
    """
    not_logged_in = CreditsView(logged_in=False)
    try:
        from hermes_cli.auth import get_provider_auth_state

        tok = (get_provider_auth_state("nous") or {}).get("access_token")
        if not (isinstance(tok, str) and tok.strip()):
            return not_logged_in
    except Exception:
        return not_logged_in

    try:
        import concurrent.futures

        from hermes_cli.nous_account import (
            get_nous_portal_account_info,
            nous_portal_topup_url,
        )

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            account = pool.submit(get_nous_portal_account_info, force_fresh=True).result(
                timeout=timeout
            )
    except Exception:
        logger.debug("credits ▸ /topup portal fetch failed (fail-open)", exc_info=True)
        return not_logged_in

    if account is None or not getattr(account, "logged_in", False):
        return not_logged_in

    snapshot = build_nous_credits_snapshot(account)
    # Balance lines = the snapshot block minus the two trailing affordance lines
    # ("Top up: <url>" + "(or run /topup)") that build_nous_credits_snapshot
    # appends for the /usage surface. /topup renders its own button/panel.
    balance_lines: list[str] = []
    if snapshot is not None:
        rendered = render_account_usage_lines(snapshot, markdown=markdown)
        balance_lines = [
            line
            for line in rendered
            if not line.lstrip().startswith("Top up:")
            and not line.lstrip().startswith("(or run")
        ]

    # Identity line — shown before any open (roadmap §4.4).
    email = getattr(account, "email", None)
    org_name = getattr(account, "org_name", None)
    who: list[str] = []
    if email:
        who.append(str(email))
    if org_name:
        who.append(f"org {org_name}")
    identity_line = ("Topping up as " + " / ".join(who)) if who else None

    return CreditsView(
        logged_in=True,
        balance_lines=tuple(balance_lines),
        identity_line=identity_line,
        topup_url=nous_portal_topup_url(account),
        depleted=getattr(account, "paid_service_access", None) is False,
    )


def _codex_backend_urls(base_url: str) -> tuple[str, str, str]:
    """Resolve the Codex backend endpoints (usage, reset-credits list, consume).

    Mirrors the Codex CLI's PathStyle split (codex-rs backend-client): base URLs
    containing ``/backend-api`` use the ChatGPT ``/wham/...`` paths; everything
    else uses ``/api/codex/...``.
    """
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        normalized = "https://chatgpt.com/backend-api/codex"
    if normalized.endswith("/codex"):
        normalized = normalized[: -len("/codex")]
    prefix = normalized + ("/wham" if "/backend-api" in normalized else "/api/codex")
    return (
        prefix + "/usage",
        prefix + "/rate-limit-reset-credits",
        prefix + "/rate-limit-reset-credits/consume",
    )


def _resolve_codex_usage_url(base_url: str) -> str:
    return _codex_backend_urls(base_url)[0]


def _resolve_codex_usage_credentials(
    base_url: Optional[str],
    api_key: Optional[str],
) -> tuple[str, str, Optional[str]]:
    """Resolve Codex quota credentials from the native runtime path.

    Prefer explicit live-agent credentials, then the legacy singleton OAuth
    state, then the credential pool.  Hermes's native OAuth setup now stores
    device-code logins in the pool, so quota diagnostics must not depend only
    on the older singleton store.
    """
    explicit_key = str(api_key or "").strip()
    if explicit_key:
        return explicit_key, str(base_url or "").strip(), None

    # Tier 2: the native runtime resolver. It ALREADY falls back to the
    # credential pool when the singleton is empty (see
    # ``resolve_codex_runtime_credentials`` — issue #32992), so in a pool-only
    # setup this returns a usable ``source="credential_pool"`` token.
    #
    # Only ``AuthError`` ("no creds" / rate-limited) is caught so tier 3 can
    # run: a broad ``except Exception`` would (a) mask a transient refresh /
    # network failure and silently hand back a DIFFERENT pool account's usage,
    # and (b) hide genuine programming errors. A refresh/network error must
    # propagate — the outer ``fetch_account_usage`` guard fails open (shows
    # nothing this turn) rather than reporting the wrong account.
    #
    # The ``account_id`` (for the ``ChatGPT-Account-Id`` header) is read
    # best-effort: a partial/missing singleton token store must not sink an
    # otherwise-usable resolver credential and force a header-less pool fallback.
    try:
        creds = resolve_codex_runtime_credentials(refresh_if_expiring=True)
        account_id: Optional[str] = None
        try:
            token_data = _read_codex_tokens()
            tokens = token_data.get("tokens") or {}
            account_id = str(tokens.get("account_id", "") or "").strip() or None
        except AuthError:
            # Pool-only creds carry no singleton account_id; header is optional.
            logger.debug("codex ▸ /usage account_id read failed (best-effort)", exc_info=True)
        return creds["api_key"], str(creds.get("base_url", "") or "").strip(), account_id
    except AuthError:
        logger.debug("codex ▸ /usage runtime resolver returned no creds; trying pool", exc_info=True)

    # Tier 3: direct pool select. Reached only when the resolver itself raises
    # AuthError (e.g. singleton missing AND its own pool read found nothing at
    # resolve time, but a pool entry is usable now). Pool credentials have no
    # account_id concept, so the ChatGPT-Account-Id header is intentionally
    # omitted here.
    from agent.credential_pool import load_pool

    pool = load_pool("openai-codex")
    entry = pool.select()
    if entry is None:
        raise RuntimeError("No available openai-codex credential in credential pool")
    return entry.runtime_api_key, str(entry.runtime_base_url or base_url or "").strip(), None


_CODEX_SESSION_WINDOW_SECONDS = 5 * 3600
_CODEX_WEEKLY_WINDOW_SECONDS = 7 * 24 * 3600
_CODEX_WINDOW_DURATION_TOLERANCE = 0.1


def _codex_window_label(key: str, window: dict[str, Any]) -> str:
    """Classify known Codex windows by duration, with key-based fallback."""
    duration = window.get("limit_window_seconds")
    if not _is_finite_num(duration):
        duration = window.get("limit_seconds")
    if _is_finite_num(duration):
        for seconds, label in (
            (_CODEX_WEEKLY_WINDOW_SECONDS, "Weekly"),
            (_CODEX_SESSION_WINDOW_SECONDS, "Session"),
        ):
            if abs(duration / seconds - 1.0) <= _CODEX_WINDOW_DURATION_TOLERANCE:
                return label
    return "Session" if key == "primary_window" else "Weekly"


def _fetch_codex_account_usage_with_credentials(
    token: str,
    base_url: Optional[str],
    account_id: Optional[str] = None,
) -> AccountUsageSnapshot:
    """Fetch + parse Codex quota usage with EXPLICIT credentials.

    The factored transport/parser half of the Codex usage fetch: it takes a
    concrete token/base_url (never resolves anything itself) and performs the
    GET against the usage endpoint, then maps the payload into an
    ``AccountUsageSnapshot``. Shared by the single-account path
    (``_fetch_codex_account_usage``) and the pool-aware path
    (``fetch_pool_account_usage``), so both surfaces render identical
    windows/details. Raises on transport/HTTP errors — callers decide how to
    fail open.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    with httpx.Client(timeout=15.0) as client:
        response = client.get(_resolve_codex_usage_url(base_url), headers=headers)
        response.raise_for_status()
    payload = response.json() or {}
    rate_limit = payload.get("rate_limit") or {}
    windows: list[AccountUsageWindow] = []
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key) or {}
        used = window.get("used_percent")
        if used is None:
            continue
        windows.append(
            AccountUsageWindow(
                label=_codex_window_label(key, window),
                used_percent=float(used),
                reset_at=_parse_dt(window.get("reset_at")),
            )
        )
    details: list[str] = []
    reset_credits = payload.get("rate_limit_reset_credits") or {}
    banked = reset_credits.get("available_count")
    if isinstance(banked, (int, float)) and int(banked) > 0:
        count = int(banked)
        plural = "s" if count != 1 else ""
        details.append(
            f"You have {count} reset{plural} banked - use /usage reset to activate"
        )
    credits = payload.get("credits") or {}
    if credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float)):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")
    return AccountUsageSnapshot(
        provider="openai-codex",
        source="usage_api",
        fetched_at=_utc_now(),
        plan=_title_case_slug(payload.get("plan_type")),
        windows=tuple(windows),
        details=tuple(details),
    )


def _chatgpt_account_id_from_token(token: str) -> Optional[str]:
    """Best-effort account binding for a concrete Codex OAuth JWT."""
    claims = _decode_jwt_claims(token)
    auth_claims = claims.get("https://api.openai.com/auth")
    if not isinstance(auth_claims, dict):
        return None
    account_id = auth_claims.get("chatgpt_account_id")
    if not isinstance(account_id, str) or not account_id.strip():
        return None
    return account_id.strip()


def _fetch_codex_account_usage(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    token, resolved_base_url, account_id = _resolve_codex_usage_credentials(base_url, api_key)
    return _fetch_codex_account_usage_with_credentials(token, resolved_base_url, account_id)


@dataclass(frozen=True)
class CodexResetRedeemResult:
    """Outcome of a `/usage reset` attempt against the Codex backend."""

    status: str  # reset | nothing_to_reset | no_credit | already_redeemed |
    #              not_exhausted | no_credits_banked | unavailable
    message: str
    available_count: int = 0
    windows_reset: int = 0

    @property
    def redeemed(self) -> bool:
        return self.status == "reset"


# Client-side guard threshold: a rate-limit window only counts as exhausted
# when it is fully used. Below this, redeeming a banked reset wastes most of
# its value, so we block and point at --force instead.
_CODEX_WINDOW_EXHAUSTED_PERCENT = 100.0


def redeem_codex_reset_credit(
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    force: bool = False,
) -> CodexResetRedeemResult:
    """Redeem one banked Codex rate-limit reset credit (`/usage reset`).

    Flow (mirrors the Codex CLI's reset-credits picker, codex-rs
    ``backend-client``):

    1. ``GET .../usage`` — read the current windows + banked credit count.
    2. Guard: zero banked credits → refuse. No window fully used and not
       ``force`` → refuse with a warning (a banked reset restores the WHOLE
       5h + weekly allowance; burning it early wastes it). The backend has
       the same protection (``nothing_to_reset`` doesn't consume the
       credit), but failing fast client-side gives a clearer message.
    3. ``POST .../rate-limit-reset-credits/consume`` with a fresh UUID
       idempotency key (``redeem_request_id``). No ``credit_id`` — the
       backend picks the next available credit, exactly like the CLI's
       default "Full reset" option.

    Never raises: every failure mode returns a ``CodexResetRedeemResult``
    with a user-renderable message.
    """
    import uuid

    try:
        token, resolved_base_url, account_id = _resolve_codex_usage_credentials(base_url, api_key)
    except Exception:
        return CodexResetRedeemResult(
            status="unavailable",
            message="No Codex credentials available. Run `hermes auth` to sign in with your ChatGPT account.",
        )
    usage_url, _credits_url, consume_url = _codex_backend_urls(resolved_base_url)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    try:
        with httpx.Client(timeout=15.0) as client:
            usage_resp = client.get(usage_url, headers=headers)
            usage_resp.raise_for_status()
            payload = usage_resp.json() or {}

            reset_credits = payload.get("rate_limit_reset_credits") or {}
            raw_count = reset_credits.get("available_count")
            available = int(raw_count) if isinstance(raw_count, (int, float)) else 0
            if available <= 0:
                return CodexResetRedeemResult(
                    status="no_credits_banked",
                    message="No banked reset credits on this account — nothing to redeem.",
                )

            rate_limit = payload.get("rate_limit") or {}
            worst_used: Optional[float] = None
            for key in ("primary_window", "secondary_window"):
                used = (rate_limit.get(key) or {}).get("used_percent")
                if isinstance(used, (int, float)):
                    worst_used = max(worst_used or 0.0, float(used))
            exhausted = worst_used is not None and worst_used >= _CODEX_WINDOW_EXHAUSTED_PERCENT
            if not exhausted and not force:
                usage_note = (
                    f"your busiest window is only {worst_used:.0f}% used"
                    if worst_used is not None
                    else "your current usage could not be confirmed as exhausted"
                )
                plural = "s" if available != 1 else ""
                return CodexResetRedeemResult(
                    status="not_exhausted",
                    message=(
                        f"⚠️ Not redeeming: {usage_note}. A banked reset restores your FULL "
                        f"5h + weekly limits, so spending it now would waste most of it. "
                        f"You have {available} reset{plural} banked. "
                        f"Use `/usage reset --force` to redeem anyway."
                    ),
                    available_count=available,
                )

            consume_resp = client.post(
                consume_url,
                headers={**headers, "Content-Type": "application/json"},
                json={"redeem_request_id": str(uuid.uuid4())},
            )
            consume_resp.raise_for_status()
            body = consume_resp.json() or {}
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return CodexResetRedeemResult(
                status="unavailable",
                message=(
                    "Codex backend rejected the request (HTTP "
                    f"{code}). Reset credits require ChatGPT-account (OAuth) auth — "
                    "run `hermes auth` and sign in with your ChatGPT account."
                ),
            )
        return CodexResetRedeemResult(
            status="unavailable",
            message=f"Codex backend error (HTTP {code}) — try again shortly.",
        )
    except Exception as exc:
        return CodexResetRedeemResult(
            status="unavailable",
            message=f"Could not reach the Codex backend: {exc}",
        )

    code = str(body.get("code", "") or "").strip().lower()
    windows_reset = body.get("windows_reset")
    windows_reset = int(windows_reset) if isinstance(windows_reset, (int, float)) else 0
    remaining = max(0, available - 1)
    plural = "s" if remaining != 1 else ""
    if code == "reset":
        # The redeemed reset restores the account's quota upstream — lift any
        # persisted pool cooldowns so Hermes doesn't keep the credential
        # frozen behind the now-stale ``last_error_reset_at`` (issue #43747).
        try:
            from hermes_cli.auth import clear_codex_pool_quota_cooldowns

            clear_codex_pool_quota_cooldowns()
        except Exception:
            logger.debug(
                "Failed to clear Codex pool cooldowns after reset redemption",
                exc_info=True,
            )
        return CodexResetRedeemResult(
            status="reset",
            message=(
                f"✅ Reset redeemed — your usage limits have been reset. "
                f"{remaining} banked reset{plural} remaining."
            ),
            available_count=remaining,
            windows_reset=windows_reset,
        )
    if code == "nothing_to_reset":
        return CodexResetRedeemResult(
            status="nothing_to_reset",
            message=(
                "Backend reports nothing to reset — your limits aren't exhausted. "
                "The credit was NOT spent."
            ),
            available_count=available,
        )
    if code == "no_credit":
        return CodexResetRedeemResult(
            status="no_credit",
            message="Backend reports no available reset credit on this account.",
        )
    if code == "already_redeemed":
        return CodexResetRedeemResult(
            status="already_redeemed",
            message="This redemption was already processed — no additional credit was spent.",
            available_count=remaining,
        )
    return CodexResetRedeemResult(
        status="unavailable",
        message=f"Unexpected response from the Codex backend: {body!r}",
    )


def _fetch_anthropic_account_usage() -> Optional[AccountUsageSnapshot]:
    token = (resolve_anthropic_token() or "").strip()
    if not token:
        return None
    if not _is_oauth_token(token):
        return AccountUsageSnapshot(
            provider="anthropic",
            source="oauth_usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="Anthropic account limits are only available for OAuth-backed Claude accounts.",
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "claude-code/2.1.0",
    }
    with httpx.Client(timeout=15.0) as client:
        response = client.get("https://api.anthropic.com/api/oauth/usage", headers=headers)
        response.raise_for_status()
    payload = response.json() or {}
    windows: list[AccountUsageWindow] = []
    mapping = (
        ("five_hour", "Current session"),
        ("seven_day", "Current week"),
        ("seven_day_opus", "Opus week"),
        ("seven_day_sonnet", "Sonnet week"),
    )
    for key, label in mapping:
        window = payload.get(key) or {}
        util = window.get("utilization")
        if util is None:
            continue
        used = float(util) * 100 if float(util) <= 1 else float(util)
        windows.append(
            AccountUsageWindow(
                label=label,
                used_percent=used,
                reset_at=_parse_dt(window.get("resets_at")),
            )
        )
    details: list[str] = []
    extra = payload.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used_credits = extra.get("used_credits")
        monthly_limit = extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if isinstance(used_credits, (int, float)) and isinstance(monthly_limit, (int, float)):
            details.append(
                f"Extra usage: {used_credits:.2f} / {monthly_limit:.2f} {currency}"
            )
    return AccountUsageSnapshot(
        provider="anthropic",
        source="oauth_usage_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
        details=tuple(details),
    )


def _fetch_openrouter_account_usage(base_url: Optional[str], api_key: Optional[str]) -> Optional[AccountUsageSnapshot]:
    runtime = resolve_runtime_provider(
        requested="openrouter",
        explicit_base_url=base_url,
        explicit_api_key=api_key,
    )
    token = str(runtime.get("api_key", "") or "").strip()
    if not token:
        return None
    normalized = str(runtime.get("base_url", "") or "").rstrip("/")
    credits_url = f"{normalized}/credits"
    key_url = f"{normalized}/key"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=10.0) as client:
        credits_resp = client.get(credits_url, headers=headers)
        credits_resp.raise_for_status()
        credits = (credits_resp.json() or {}).get("data") or {}
        try:
            key_resp = client.get(key_url, headers=headers)
            key_resp.raise_for_status()
            key_data = (key_resp.json() or {}).get("data") or {}
        except Exception:
            key_data = {}
    total_credits = float(credits.get("total_credits") or 0.0)
    total_usage = float(credits.get("total_usage") or 0.0)
    details = [f"Credits balance: ${max(0.0, total_credits - total_usage):.2f}"]
    windows: list[AccountUsageWindow] = []
    limit = key_data.get("limit")
    limit_remaining = key_data.get("limit_remaining")
    limit_reset = str(key_data.get("limit_reset") or "").strip()
    usage = key_data.get("usage")
    if (
        isinstance(limit, (int, float))
        and float(limit) > 0
        and isinstance(limit_remaining, (int, float))
        and 0 <= float(limit_remaining) <= float(limit)
    ):
        limit_value = float(limit)
        remaining_value = float(limit_remaining)
        used_percent = ((limit_value - remaining_value) / limit_value) * 100
        detail_parts = [f"${remaining_value:.2f} of ${limit_value:.2f} remaining"]
        if limit_reset:
            detail_parts.append(f"resets {limit_reset}")
        windows.append(
            AccountUsageWindow(
                label="API key quota",
                used_percent=used_percent,
                detail=" • ".join(detail_parts),
            )
        )
    if isinstance(usage, (int, float)):
        usage_parts = [f"API key usage: ${float(usage):.2f} total"]
        for value, label in (
            (key_data.get("usage_daily"), "today"),
            (key_data.get("usage_weekly"), "this week"),
            (key_data.get("usage_monthly"), "this month"),
        ):
            if isinstance(value, (int, float)) and float(value) > 0:
                usage_parts.append(f"${float(value):.2f} {label}")
        details.append(" • ".join(usage_parts))
    return AccountUsageSnapshot(
        provider="openrouter",
        source="credits_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
        details=tuple(details),
    )


# ── OpenCode Go (pool-aware, read-only) ─────────────────────────────────────

_OPENCODE_GO_DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
_OPENCODE_GO_WINDOW_SPECS = (
    ("rolling", "Rolling 5h"),
    ("weekly", "Weekly"),
    ("monthly", "Monthly"),
)


def _canonical_opencode_go_base_url(base_url: Optional[str]) -> str:
    """Canonicalize the OpenCode Go usage base URL.

    The usage endpoint lives at ``<base>/usage`` under the SAME ``/v1`` root
    as the OpenAI-compatible inference endpoints, so the canonical official
    base is ``https://opencode.ai/zen/go/v1``.  Known official variants —
    ``https://opencode.ai/zen/go`` and any trailing-slash form, with or
    without the ``/v1`` suffix — normalize to that canonical URL, and an
    empty value falls back to it.  Anything else (custom proxy overrides via
    ``OPENCODE_GO_BASE_URL`` or an explicit base) is returned untouched: the
    operator owns that layout, and a usage GET must not silently relocate it.
    """
    url = str(base_url or "").strip().rstrip("/")
    if not url:
        return _OPENCODE_GO_DEFAULT_BASE_URL
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return url
    if (
        parsed.netloc.lower() == "opencode.ai"
        and parsed.path.rstrip("/") in {"/zen/go", "/zen/go/v1"}
        and not parsed.query
        and not parsed.fragment
    ):
        return _OPENCODE_GO_DEFAULT_BASE_URL
    return url


def _resolve_opencode_go_usage_credentials(
    base_url: Optional[str],
    api_key: Optional[str],
) -> tuple[str, str]:
    """Resolve OpenCode Go usage credentials with strict read-only reads.

    Prefer explicit live-agent credentials; otherwise read the env-backed
    ``OPENCODE_GO_API_KEY`` / ``OPENCODE_GO_BASE_URL`` variables directly and
    canonicalize the official base form.  Deliberately does NOT call
    ``resolve_runtime_provider``: that chain can seed/select the credential
    pool and re-derives ``api_mode`` from the effective model, which strips
    the ``/v1`` suffix from the official base for anthropic-routed Go models
    (e.g. minimax/qwen) — the usage GET would then hit
    ``https://opencode.ai/zen/go/usage`` instead of the canonical
    ``https://opencode.ai/zen/go/v1/usage``.  Never loads, selects, peeks,
    refreshes, or persists any credential.  The token is never logged.
    """
    explicit_key = str(api_key or "").strip()
    token = explicit_key or str(os.getenv("OPENCODE_GO_API_KEY") or "").strip()
    if not token:
        raise RuntimeError("No OpenCode Go API key configured (set OPENCODE_GO_API_KEY)")
    explicit_base = str(base_url or "").strip()
    base = explicit_base or str(os.getenv("OPENCODE_GO_BASE_URL") or "").strip()
    return token, _canonical_opencode_go_base_url(base)


def _fetch_opencode_go_account_usage_with_credentials(
    token: str,
    base_url: Optional[str],
) -> AccountUsageSnapshot:
    """Fetch + parse OpenCode Go usage with EXPLICIT credentials.

    The factored transport/parser half of the OpenCode Go usage fetch: takes a
    concrete token/base_url (never resolves anything itself) and performs the
    GET against the ``/usage`` endpoint, then maps the payload into an
    ``AccountUsageSnapshot``. Shared by the single-account path
    (``_fetch_opencode_go_account_usage``) and the env pool path
    (``fetch_pool_account_usage``), so both surfaces render identical windows.
    Raises on transport/HTTP errors — callers decide how to fail open.

    Payload contract (verified live): ``{"usage": {monthly|rolling|weekly:
    {"percent": <used %>, "resetsAt": <ISO-8601>, "status": <arbitrary
    string>}}}``. ``percent`` is the used percent, clamped to [0, 100];
    ``status`` is informational and intentionally ignored.  The payload must
    be a dict: a malformed (non-dict) body yields a safe unavailable
    snapshot instead of crashing, and a dict with no parseable windows yields
    an empty-windows snapshot the status formatter renders as ``?``.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "hermes",
    }
    normalized = _canonical_opencode_go_base_url(base_url)
    with httpx.Client(timeout=15.0) as client:
        response = client.get(f"{normalized}/usage", headers=headers)
        response.raise_for_status()
    try:
        raw = response.json()
    except Exception:
        raw = None
    if not isinstance(raw, dict):
        return AccountUsageSnapshot(
            provider="opencode-go",
            source="usage_api",
            fetched_at=_utc_now(),
            unavailable_reason="The OpenCode Go usage service returned an unexpected response.",
        )
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    windows: list[AccountUsageWindow] = []
    for key, label in _OPENCODE_GO_WINDOW_SPECS:
        window = usage.get(key)
        if not isinstance(window, dict):
            continue
        percent = window.get("percent")
        if not _is_finite_num(percent):
            continue
        windows.append(
            AccountUsageWindow(
                label=label,
                # percent is the USED percent; clamp so out-of-range backend
                # values never render as a falsely-confident negative gauge.
                used_percent=max(0.0, min(100.0, float(percent))),
                reset_at=_parse_dt(window.get("resetsAt")),
            )
        )
    return AccountUsageSnapshot(
        provider="opencode-go",
        source="usage_api",
        fetched_at=_utc_now(),
        windows=tuple(windows),
    )


def _fetch_opencode_go_account_usage(
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    token, resolved_base_url = _resolve_opencode_go_usage_credentials(base_url, api_key)
    return _fetch_opencode_go_account_usage_with_credentials(token, resolved_base_url)


def fetch_account_usage(
    provider: Optional[str],
    *,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Optional[AccountUsageSnapshot]:
    normalized = str(provider or "").strip().lower()
    if normalized in {"", "auto", "custom"}:
        return None
    try:
        if normalized == "openai-codex":
            return _fetch_codex_account_usage(base_url=base_url, api_key=api_key)
        if normalized == "anthropic":
            return _fetch_anthropic_account_usage()
        if normalized == "openrouter":
            return _fetch_openrouter_account_usage(base_url, api_key)
        if normalized == "opencode-go":
            return _fetch_opencode_go_account_usage(base_url=base_url, api_key=api_key)
    except Exception:
        return None
    return None


_POOL_USAGE_CACHE_TTL_SECONDS = 60.0
_POOL_USAGE_CACHE: dict[tuple[str, str], tuple[float, AccountUsageSnapshot]] = {}
_POOL_USAGE_CACHE_LOCK = threading.Lock()


def _clear_pool_account_usage_cache_for_tests() -> None:
    """Clear the process-local quota cache used by synthetic tests."""
    with _POOL_USAGE_CACHE_LOCK:
        _POOL_USAGE_CACHE.clear()


def _pool_usage_cache_get(provider: str, credential_id: str) -> Optional[AccountUsageSnapshot]:
    now = time.monotonic()
    key = (provider, credential_id)
    with _POOL_USAGE_CACHE_LOCK:
        cached = _POOL_USAGE_CACHE.get(key)
        if cached is None:
            return None
        deadline, snapshot = cached
        if deadline <= now:
            _POOL_USAGE_CACHE.pop(key, None)
            return None
        return snapshot


def _pool_usage_cache_put(provider: str, credential_id: str, snapshot: AccountUsageSnapshot) -> None:
    now = time.monotonic()
    with _POOL_USAGE_CACHE_LOCK:
        # Bound stale-key growth when credentials are removed/re-added.
        for key, (deadline, _snapshot) in list(_POOL_USAGE_CACHE.items()):
            if deadline <= now:
                _POOL_USAGE_CACHE.pop(key, None)
        _POOL_USAGE_CACHE[(provider, credential_id)] = (
            now + _POOL_USAGE_CACHE_TTL_SECONDS,
            snapshot,
        )


def _unavailable_pool_snapshot(reason: str, *, provider: str = "openai-codex") -> AccountUsageSnapshot:
    return AccountUsageSnapshot(
        provider=provider,
        source="usage_api",
        fetched_at=_utc_now(),
        unavailable_reason=reason,
    )


# Stable internal cache key for the env-fallback OpenCode Go account. Only
# used when NO pool rows are visible (classic env-only setups); pool rows
# cache under their own entry ids. Provider-specific by construction (the
# cache tuple is (provider, key)) and never serialized: the pool path never
# copies it onto a snapshot.
_OPENCODE_GO_ENV_CACHE_KEY = "env"


def _fetch_opencode_go_env_usage_snapshot(*, fresh: bool) -> tuple[AccountUsageSnapshot, ...]:
    """Fetch the ONE env-backed OpenCode Go account into a singleton snapshot.

    This is the compatibility fallback used by ``fetch_pool_account_usage``
    when NO ``opencode-go`` pool rows are visible (classic env-only setups):
    classic setups keep working exactly as before, byte-identical payloads
    included (no ``credential_id``/``account_label`` beyond the active flag).

    Read-only by design: credential resolution is a strict env read
    (``OPENCODE_GO_API_KEY`` / ``OPENCODE_GO_BASE_URL``) with no pool
    load/select/peek, no refresh/persistence, and no runtime-provider
    api-mode normalization (which would strip ``/v1`` from the official base
    for anthropic-routed Go models and point the usage GET at the wrong
    path).  No credential is configured → ``()`` (nothing to show, like an
    empty pool).  A configured credential whose fetch fails yields an
    unavailable snapshot with the same failure isolation as the Codex pool
    entries.
    """
    if not fresh:
        cached = _pool_usage_cache_get("opencode-go", _OPENCODE_GO_ENV_CACHE_KEY)
        if cached is not None:
            return (replace(cached, active=True),)
    try:
        token, base_url = _resolve_opencode_go_usage_credentials(None, None)
    except Exception:
        # No OpenCode Go credential configured (e.g. OPENCODE_GO_API_KEY
        # unset): nothing to show — exactly like an empty pool.
        return ()
    try:
        snapshot = _fetch_opencode_go_account_usage_with_credentials(token, base_url)
    except httpx.HTTPStatusError as exc:
        snapshot = _unavailable_pool_snapshot(
            "The stored OpenCode Go API key was rejected."
            if exc.response.status_code in {401, 403}
            else "The OpenCode Go usage service is temporarily unavailable.",
            provider="opencode-go",
        )
    except Exception:
        snapshot = _unavailable_pool_snapshot(
            "The OpenCode Go usage service is temporarily unavailable.",
            provider="opencode-go",
        )
    _pool_usage_cache_put("opencode-go", _OPENCODE_GO_ENV_CACHE_KEY, snapshot)
    return (replace(snapshot, active=True),)


def _hydrate_opencode_go_env_rows(entries) -> None:
    """Re-resolve borrowed ``env:`` rows from the live environment.

    ``env:VAR`` rows (seeded from .env by the pool seeder) persist
    metadata-only: the disk-boundary sanitizer strips the token and the raw
    read here never hydrates it, so an otherwise healthy env-backed profile
    would report "no usable key".  Re-resolve the var with the seeder's own
    .env-preferred helper (``get_env_prefer_dotenv`` - pure read, memoized
    on .env mtime, never writes), honoring user suppression so a deliberately
    removed env source is not resurrected by the usage path.
    """
    try:
        from agent.credential_pool import get_env_prefer_dotenv
    except Exception:
        return
    for entry in entries:
        source = str(getattr(entry, "source", "") or "").strip()
        if not source.startswith("env:"):
            continue
        env_var = source.split(":", 1)[1].strip()
        if not env_var:
            continue
        # Mirror _seed_from_env's suppression gate: an env source the user
        # removed (hermes auth remove opencode-go <N>) must not come back
        # via the usage path just because the var still exists somewhere.
        try:
            if is_source_suppressed("opencode-go", source):
                continue
        except Exception:
            pass
        try:
            resolved = str(get_env_prefer_dotenv(env_var) or "").strip()
        except Exception:
            resolved = ""
        if resolved:
            # runtime_api_key is a property over access_token on the real
            # class; writing the field hydrates the row in place.
            entry.access_token = resolved


def _fetch_opencode_go_pool_usage_snapshots(
    *,
    active_entry_id: Optional[str] = None,
    fresh: bool = False,
) -> tuple[AccountUsageSnapshot, ...]:
    """Fetch OpenCode Go usage for every visible pool row, read-only.

    Enumerates the persisted ``opencode-go`` pool rows with the same raw
    ``read_credential_pool`` read + ``PooledCredential.from_dict`` parse as
    the Codex pool path — never ``load_pool``/``select``/``peek``, no token
    refresh, no persistence, no routing mutation — then fetches each row's
    usage through the existing explicit Go transport
    (``_fetch_opencode_go_account_usage_with_credentials``) with that row's
    own ``runtime_api_key``/``runtime_base_url``.

    Per-row behavior mirrors the Codex entries exactly: the 60s cache is
    keyed ``("opencode-go", entry.id)``, failures are isolated per row
    (401/403 → "key rejected", anything else → temporary-unavailable, and a
    row with no stored key → its own unavailable snapshot), and the
    ``active`` flag follows ``active_entry_id`` when it names a visible row,
    else the first row with a usable key (priority order), else the first
    row.  Labels are safe ordinals (``OpenCode Go N``) — raw row labels,
    ids, and tokens are never placed on display fields.

    Compatibility fallback: when NO pool rows are visible (classic env-only
    setup, or the pool read itself fails), delegates to
    ``_fetch_opencode_go_env_usage_snapshot`` so env-only setups keep the
    exact pre-pool behavior.
    """
    try:
        from agent.credential_pool import PooledCredential

        raw_rows = read_credential_pool("opencode-go")
        entries = [
            PooledCredential.from_dict("opencode-go", row)
            for row in raw_rows
            if isinstance(row, dict)
        ]
    except Exception:
        entries = []

    if not entries:
        # Env-only compatibility fallback (classic setups, or a failed pool
        # read): never let a pool-read hiccup hide a configured env account.
        return _fetch_opencode_go_env_usage_snapshot(fresh=fresh)

    entries.sort(key=lambda entry: (int(entry.priority or 0), str(entry.id)))

    # Borrowed env rows persist tokenless on disk and the raw read never
    # hydrates them; re-resolve from the live environment before the fetch
    # loop so env-backed profiles render real numbers instead of
    # "no usable key".  Placement is before effective_active_id selection:
    # active marking must see the hydrated token.
    _hydrate_opencode_go_env_rows(entries)

    known_ids = {entry.id for entry in entries}
    effective_active_id = active_entry_id if active_entry_id in known_ids else None
    if effective_active_id is None:
        effective_active_id = next(
            (entry.id for entry in entries if str(entry.runtime_api_key or "").strip()),
            entries[0].id,
        )

    def fetch_entry(entry) -> AccountUsageSnapshot:
        if not fresh:
            cached = _pool_usage_cache_get("opencode-go", entry.id)
            if cached is not None:
                return cached
        token = str(entry.runtime_api_key or "").strip()
        if not token:
            snapshot = _unavailable_pool_snapshot(
                "No usable OpenCode Go API key is stored for this account.",
                provider="opencode-go",
            )
        else:
            try:
                snapshot = _fetch_opencode_go_account_usage_with_credentials(
                    token,
                    str(entry.runtime_base_url or "").strip(),
                )
            except httpx.HTTPStatusError as exc:
                snapshot = _unavailable_pool_snapshot(
                    "The stored OpenCode Go API key was rejected."
                    if exc.response.status_code in {401, 403}
                    else "The OpenCode Go usage service is temporarily unavailable.",
                    provider="opencode-go",
                )
            except Exception:
                snapshot = _unavailable_pool_snapshot(
                    "The OpenCode Go usage service is temporarily unavailable.",
                    provider="opencode-go",
                )
        _pool_usage_cache_put("opencode-go", entry.id, snapshot)
        return snapshot

    # Independent requests are concurrent so two unavailable accounts cost at
    # most one transport timeout rather than blocking the gateway serially.
    try:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(entries))) as executor:
            base_snapshots = list(executor.map(fetch_entry, entries))
    except Exception:
        base_snapshots = [fetch_entry(entry) for entry in entries]

    return tuple(
        replace(
            snapshot,
            credential_id=entry.id,
            account_label=f"OpenCode Go {index}",
            active=entry.id == effective_active_id,
        )
        for index, (entry, snapshot) in enumerate(zip(entries, base_snapshots), start=1)
    )


def fetch_pool_account_usage(
    provider: Optional[str],
    *,
    active_entry_id: Optional[str] = None,
    fresh: bool = False,
) -> tuple[AccountUsageSnapshot, ...]:
    """Fetch Codex limits for every persisted pool row without mutating auth.

    This inspection path deliberately reads raw persisted rows instead of
    ``load_pool()``: loading can seed/normalize and write the credential store,
    while selection can rotate routing. It also never refreshes OAuth tokens.
    Expired/rejected standby credentials therefore appear as unavailable until
    normal inference routing refreshes them.

    ``fresh=True`` bypasses the per-entry 60s cache: every entry is fetched
    from the backend, and the fresh result is written back through the cache so
    later cached emissions immediately see the new numbers (the stale entry is
    invalidated, not just skipped). This is the end-of-turn surface — the
    caller decides when a completed turn's quota change is worth a fetch — and
    deliberately adds no polling or auth refresh of its own. Failure isolation
    is unchanged: a failing entry still yields an unavailable snapshot while
    the others report live numbers.

    ``opencode-go`` enumerates its persisted pool rows the same read-only way
    as the Codex entries (raw ``read_credential_pool`` + ``PooledCredential
    .from_dict`` — never load/select/peek/refresh/persist), fetching each row
    with its own ``runtime_api_key``/``runtime_base_url`` through the explicit
    Go transport and labeling it with a safe ordinal (``OpenCode Go N``). When
    NO pool rows are visible it falls back to the classic single env-backed
    account (``OPENCODE_GO_API_KEY``), so env-only setups keep working
    unchanged. ``fresh`` and the 60s cache behave identically to the Codex
    entries.
    """
    normalized = str(provider or "").strip().lower()
    if normalized == "opencode-go":
        return _fetch_opencode_go_pool_usage_snapshots(
            active_entry_id=active_entry_id,
            fresh=fresh,
        )
    if normalized != "openai-codex":
        return ()

    try:
        from agent.credential_pool import PooledCredential

        raw_rows = read_credential_pool(normalized)
        entries = [
            PooledCredential.from_dict(normalized, row)
            for row in raw_rows
            if isinstance(row, dict)
        ]
    except Exception:
        return ()

    entries.sort(key=lambda entry: (int(entry.priority or 0), str(entry.id)))
    if not entries:
        return ()

    known_ids = {entry.id for entry in entries}
    effective_active_id = active_entry_id if active_entry_id in known_ids else None
    if effective_active_id is None:
        effective_active_id = next(
            (entry.id for entry in entries if str(entry.runtime_api_key or "").strip()),
            entries[0].id,
        )

    def fetch_entry(entry) -> AccountUsageSnapshot:
        if not fresh:
            cached = _pool_usage_cache_get(normalized, entry.id)
            if cached is not None:
                return cached
        token = str(entry.runtime_api_key or "").strip()
        if not token:
            snapshot = _unavailable_pool_snapshot("No usable OAuth access token is stored for this account.")
        else:
            try:
                snapshot = _fetch_codex_account_usage_with_credentials(
                    token,
                    str(entry.runtime_base_url or "").strip(),
                    _chatgpt_account_id_from_token(token),
                )
            except httpx.HTTPStatusError as exc:
                snapshot = _unavailable_pool_snapshot(
                    "The stored OAuth credential was rejected."
                    if exc.response.status_code in {401, 403}
                    else "The Codex usage service is temporarily unavailable."
                )
            except Exception:
                snapshot = _unavailable_pool_snapshot(
                    "The Codex usage service is temporarily unavailable."
                )
        _pool_usage_cache_put(normalized, entry.id, snapshot)
        return snapshot

    # Independent requests are concurrent so two unavailable accounts cost at
    # most one transport timeout rather than blocking the gateway serially.
    try:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(entries))) as executor:
            base_snapshots = list(executor.map(fetch_entry, entries))
    except Exception:
        base_snapshots = [fetch_entry(entry) for entry in entries]

    return tuple(
        replace(
            snapshot,
            credential_id=entry.id,
            account_label=f"Codex {index}",
            active=entry.id == effective_active_id,
        )
        for index, (entry, snapshot) in enumerate(zip(entries, base_snapshots), start=1)
    )


# ── Aggregate (all-provider, explicit-command surface) ──────────────────────
#
# ``fetch_aggregate_account_usage`` is the read-only, all-provider surface for
# explicit user commands (plugin /chatgpt-limits, /gptusage, CLI). It composes
# the existing per-provider pool fetches and re-labels every snapshot with
# SAFE display names: a persisted row's raw ``label`` is preserved ONLY when it
# is a benign human-configured name (see ``is_safe_aggregate_account_label``);
# anything email-shaped, env-var-shaped, token/JWT/UUID/hash-like, an internal
# id, or otherwise non-benign falls back to a provider-prefixed ordinal
# (``OpenAI-Codex-N`` / ``OpenCode-Go-N``). Account ids and tokens are never
# placed on the snapshots' display fields and the serializer already refuses
# to emit ``credential_id``.

_AGGREGATE_PROVIDERS = ("openai-codex", "opencode-go")

# Conservative display-name pattern: printable ASCII word chars only, 1..48
# chars, first char alphanumeric. No ``@``, no control characters, no
# non-ASCII, no leading whitespace, no punctuation outside `` ._()-``.
_SAFE_AGGREGATE_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,47}$")
# Env-var-shaped names (e.g. ``OPENCODE_GO_API_KEY``, ``MY_SECRET_TOKEN``).
_ENV_VAR_LIKE_LABEL_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")
# Digest/UUID-shaped: >= 32 chars of hex/dash only.
_HEXISH_LABEL_PATTERN = re.compile(r"^[0-9a-fA-F-]{32,}$")
# Common credential/token prefixes (lowercased comparisons). Defense-in-depth:
# a raw label that starts with one of these is never a benign display name.
_TOKEN_LIKE_LABEL_PREFIXES = (
    "sk-",
    "sk_",
    "sk.",
    "ghp_",
    "gho_",
    "ghs_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "xoxa-",
    "ya29.",
    "eyj",
    "ak-",
    "rk-",
    "akia",
    "-----begin",
)
# Suffixes that make a name look like a credential variable, not a human label.
_ENV_VAR_LIKE_LABEL_SUFFIXES = (
    "_api_key",
    "_token",
    "_secret",
    "_password",
    "_key",
    "_access_key",
)
# Whole-word credential names that are never benign display labels.
_CREDENTIAL_WORD_LABELS = {
    "bearer",
    "token",
    "secret",
    "password",
    "apikey",
    "api_key",
    "access_token",
    "refresh_token",
}


def is_safe_aggregate_account_label(raw: Any) -> bool:
    """True iff ``raw`` is a benign, display-safe account label.

    The safe-label policy for the aggregate surface: preserve a raw persisted
    label only when it is a plausible human-configured name — 1..48 chars,
    printable ASCII word chars only (``[A-Za-z0-9][A-Za-z0-9 ._()-]*``), no
    ``@`` (no emails), no control/non-ASCII characters, not env-var-shaped
    (``*_API_KEY``, ``*_TOKEN``, …), not token/JWT/UUID/hash-shaped, and not a
    bare credential word. Anything else must fall back to a provider ordinal.
    Internal ids are rejected by the caller (``_aggregate_codex_row_labels``)
    rather than here, because the id comparison needs the row's own id.
    """
    if not isinstance(raw, str):
        return False
    if not raw or len(raw) > 48:
        return False
    if not _SAFE_AGGREGATE_LABEL_PATTERN.fullmatch(raw):
        return False
    lowered = raw.lower()
    if "@" in raw:  # belt-and-braces; the pattern already excludes '@'
        return False
    if lowered.startswith(_TOKEN_LIKE_LABEL_PREFIXES):
        return False
    if _ENV_VAR_LIKE_LABEL_PATTERN.fullmatch(raw):
        return False
    if lowered.endswith(_ENV_VAR_LIKE_LABEL_SUFFIXES):
        return False
    if lowered in _CREDENTIAL_WORD_LABELS:
        return False
    if _HEXISH_LABEL_PATTERN.fullmatch(raw):
        return False
    return True


def _aggregate_display_label(raw: Any, *, provider: str, index: int, entry_id: Optional[str] = None) -> str:
    """Return the safe display label for one aggregate account.

    Preserves ``raw`` only when benign (and not the row's own internal id);
    otherwise falls back to a provider-prefixed ordinal: ``OpenAI-Codex-N`` for
    ``openai-codex``, ``OpenCode-Go-N`` for ``opencode-go``, else a title-cased
    provider prefix. ``index`` is the 1-based position among that provider's
    accounts in priority order.
    """
    if entry_id is not None and isinstance(raw, str) and raw == str(entry_id):
        raw = None  # never emit an internal credential id as a display name
    if is_safe_aggregate_account_label(raw):
        return raw
    prefix = {
        "openai-codex": "OpenAI-Codex",
        "opencode-go": "OpenCode-Go",
    }.get(provider, str(provider or "account").title())
    return f"{prefix}-{index}"


def _aggregate_codex_row_labels() -> tuple[tuple[str, str], ...]:
    """Map persisted Codex pool rows to SAFE display labels, in priority order.

    Read-only: uses the same raw ``read_credential_pool`` read as
    ``fetch_pool_account_usage`` — never ``load_pool``/``select``/``peek``, no
    refresh, no persistence, no routing mutation. The returned pairs are
    ``(entry_id, safe_label)``; ``entry_id`` is used only for internal
    correlation and is never placed on a display field. Any read/parse failure
    yields ``()`` and the aggregate falls back to pure ordinals.
    """
    try:
        from agent.credential_pool import PooledCredential

        raw_rows = read_credential_pool("openai-codex")
        entries = [
            PooledCredential.from_dict("openai-codex", row)
            for row in raw_rows
            if isinstance(row, dict)
        ]
    except Exception:
        return ()
    entries.sort(key=lambda entry: (int(entry.priority or 0), str(entry.id)))
    return tuple(
        (
            str(entry.id),
            _aggregate_display_label(
                entry.label,
                provider="openai-codex",
                index=index,
                entry_id=str(entry.id),
            ),
        )
        for index, entry in enumerate(entries, start=1)
    )


def fetch_aggregate_account_usage(
    *,
    providers: tuple[str, ...] = _AGGREGATE_PROVIDERS,
    fresh: bool = True,
) -> tuple[AccountUsageSnapshot, ...]:
    """Fetch usage for every configured account across the supported providers.

    The explicit all-provider surface for user commands: composes
    ``fetch_pool_account_usage`` for each provider in ``providers`` (deduped,
    order-preserving) with per-provider failure isolation, and returns one
    snapshot per account labeled for THIS surface only (safe display names —
    see ``is_safe_aggregate_account_label``; benign configured labels are
    preserved, everything else becomes ``OpenAI-Codex-N`` / ``OpenCode-Go-N``).

    Read-only by construction: it never loads, selects, peeks, refreshes, or
    persists any credential (the underlying pool fetch is itself side-effect
    free), and account ids / tokens are never placed on display fields.

    ``fresh=True`` (default) bypasses the per-entry 60s cache READS — every
    account is fetched from the backend — while the fresh results are still
    written back through the existing per-entry caches, so the gateway/status
    path and later cached calls immediately see the new numbers. This is the
    explicit-command contract: a user asking for limits gets live numbers, and
    the shared cache stays warm.

    Failure isolation mirrors the pool path: a provider that raises yields one
    unavailable snapshot (with its provider's ordinal label) instead of
    aborting the aggregate, and a failing entry inside the pool fetch already
    yields its own unavailable snapshot. The Codex row→label mapping is
    order/id based and degrades safely on unavailable rows or count mismatch
    (pure ordinals). ``opencode-go`` snapshots are always labeled
    ``OpenCode-Go-N`` — pool rows carry no display-safe label here (a raw
    label could be email/env-var/token-shaped) and the env var name
    (``OPENCODE_GO_API_KEY``) must never surface.
    """
    normalized: list[str] = []
    for raw in providers or ():
        candidate = str(raw or "").strip().lower()
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    if not normalized:
        return ()

    codex_labels = _aggregate_codex_row_labels() if "openai-codex" in normalized else ()
    labels_by_id = dict(codex_labels)

    results: list[AccountUsageSnapshot] = []
    for provider in normalized:
        try:
            snapshots = fetch_pool_account_usage(provider, fresh=fresh)
        except Exception:
            # Fixed safe message: never echo the exception text into the
            # payload (it could embed internals). The failure is logged so a
            # dead provider is diagnosable in agent.log.
            logger.debug("aggregate ▸ %s pool fetch failed (fail-open)", provider, exc_info=True)
            snapshots = (
                _unavailable_pool_snapshot(
                    "The account usage service is temporarily unavailable.", provider=provider
                ),
            )
        if provider == "openai-codex":
            for position, snapshot in enumerate(snapshots, start=1):
                label = None
                if snapshot.credential_id:
                    label = labels_by_id.get(snapshot.credential_id)
                if label is None and position - 1 < len(codex_labels):
                    # Order-based fallback (e.g. pool changed between the two
                    # raw reads, or the snapshot lacks an id).
                    label = codex_labels[position - 1][1]
                if label is None:
                    label = _aggregate_display_label(None, provider="openai-codex", index=position)
                results.append(replace(snapshot, account_label=label))
        elif provider == "opencode-go":
            for position, snapshot in enumerate(snapshots, start=1):
                results.append(
                    replace(
                        snapshot,
                        account_label=_aggregate_display_label(
                            None, provider="opencode-go", index=position
                        ),
                    )
                )
        else:
            results.extend(snapshots)
    return tuple(results)


def account_usage_snapshot_to_dict(snapshot: AccountUsageSnapshot) -> dict[str, Any]:
    """Serialize an account snapshot without credential identifiers or secrets."""
    return {
        "success": True,
        "available": snapshot.available,
        "provider": snapshot.provider,
        "source": snapshot.source,
        "title": snapshot.title,
        "plan": snapshot.plan,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "label": snapshot.account_label,
        "active": snapshot.active,
        "windows": [
            {
                "label": window.label,
                "used_percent": window.used_percent,
                "reset_at": window.reset_at.isoformat() if window.reset_at else None,
                "reset_human": _format_reset(window.reset_at) if window.reset_at else None,
                "detail": window.detail,
            }
            for window in snapshot.windows
        ],
        "details": list(snapshot.details),
        "unavailable_reason": snapshot.unavailable_reason,
    }
