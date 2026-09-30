"""
Ground Truth MCP connector — plain-language access to GroundTruth ad reporting.

Users ask in natural language about accounts, campaigns, ad groups, creatives,
locations, audiences, and performance. Data is fetched live from the
GroundTruth Reporting API (reporting.groundtruth.com) using
GROUND_TRUTH_USER_ID + GROUND_TRUTH_API_KEY.

Run:
  python ground_truth_mcp_server.py           # stdio (Claude Desktop / Cursor)
  python ground_truth_mcp_server.py --http    # http://127.0.0.1:8001/mcp
"""

from __future__ import annotations

import logging
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # optional
    def load_dotenv(*_a, **_k):  # type: ignore
        return False

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from mcp.server import MCPServer as FastMCP

from ground_truth_api import GroundTruthError, gt_client, metric

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_SCRIPT_DIR, ".env"))


def _metadata_get(path: str) -> str:
    """Read Cloud Run / GCE metadata (empty string when not on GCP)."""
    try:
        import httpx

        res = httpx.get(
            f"http://metadata.google.internal/computeMetadata/v1/{path.lstrip('/')}",
            headers={"Metadata-Flavor": "Google"},
            timeout=2.0,
        )
        if res.is_success:
            return (res.text or "").strip()
    except Exception:
        pass
    return ""


def _resolve_public_url() -> str:
    """
    Claude custom connectors need OAuth DCR. Enable when MCP_PUBLIC_URL is set
    (deploy script sets this). Git → Cloud Run often omits it — derive from
    K_SERVICE + metadata.
    """
    for key in ("MCP_PUBLIC_URL", "BASE_URL", "SERVICE_URL"):
        raw = os.environ.get(key, "").strip().rstrip("/")
        if raw:
            return raw

    service = os.environ.get("K_SERVICE", "").strip()
    if not service:
        return ""

    region = (
        os.environ.get("CLOUD_RUN_REGION", "").strip()
        or os.environ.get("GOOGLE_CLOUD_REGION", "").strip()
    )
    project_number = (
        os.environ.get("CLOUD_RUN_PROJECT_NUMBER", "").strip()
        or os.environ.get("GCP_PROJECT_NUMBER", "").strip()
        or os.environ.get("GOOGLE_CLOUD_PROJECT_NUMBER", "").strip()
    )

    if not project_number:
        project_number = _metadata_get("project/numeric-project-id")
    if not region:
        # projects/123456789/regions/us-central1
        meta_region = _metadata_get("instance/region")
        if "/" in meta_region:
            region = meta_region.rsplit("/", 1)[-1]

    if service and region and project_number:
        return f"https://{service}-{project_number}.{region}.run.app"
    return ""


MCP_PUBLIC_URL = _resolve_public_url()
MCP_OAUTH_PASSWORD = os.environ.get("MCP_OAUTH_PASSWORD", "").strip() or None
MCP_RESOURCE_URL = f"{MCP_PUBLIC_URL}/mcp" if MCP_PUBLIC_URL else ""


def _patch_claude_oauth_compat(scope: str = "ground-truth") -> None:
    """
    Claude.ai MCP OAuth requirements (see anthropics/claude-ai-mcp issues #5, #82, #214):

    1. Advertise token_endpoint_auth_methods_supported including \"none\".
    2. Treat DCR clients as public (PKCE) even when they request client_secret_post.
    3. Expose scopes_supported and CIMD support in authorization-server metadata.
    4. Strip unsupported jwt-bearer grant from Claude CIMD registration payloads.
    5. Preserve HTTPS client_id values during DCR (Claude CIMD).
    """
    import json

    import mcp.server.auth.handlers.register as register_mod
    import mcp.server.auth.routes as auth_routes

    if getattr(auth_routes, "_gt_claude_patch", False):
        return

    original_build = auth_routes.build_metadata

    def build_metadata(*args, **kwargs):
        metadata = original_build(*args, **kwargs)
        methods = list(metadata.token_endpoint_auth_methods_supported or [])
        if "none" not in methods:
            methods.insert(0, "none")
        metadata.token_endpoint_auth_methods_supported = methods
        if not metadata.scopes_supported:
            metadata.scopes_supported = [scope]
        metadata.client_id_metadata_document_supported = True
        # Claude canonical URLs omit trailing slashes on the issuer host.
        from pydantic import AnyHttpUrl

        metadata.issuer = AnyHttpUrl(str(metadata.issuer).rstrip("/"))
        return metadata

    auth_routes.build_metadata = build_metadata

    original_create_pr = auth_routes.create_protected_resource_routes

    def create_protected_resource_routes(
        resource_url, authorization_servers, scopes_supported=None, **kwargs
    ):
        from pydantic import AnyHttpUrl

        normalized = [
            AnyHttpUrl(str(url).rstrip("/")) for url in authorization_servers
        ]
        return original_create_pr(
            resource_url,
            normalized,
            scopes_supported=scopes_supported,
            **kwargs,
        )

    auth_routes.create_protected_resource_routes = create_protected_resource_routes

    register_mod._gt_cimd_client_id = None
    original_uuid4 = register_mod.uuid4

    class _CimdClientId:
        def __init__(self, value: str) -> None:
            self._value = value

        def __str__(self) -> str:
            return self._value

    def uuid4_with_cimd():
        cimd = register_mod._gt_cimd_client_id
        if cimd:
            register_mod._gt_cimd_client_id = None
            return _CimdClientId(cimd)
        return original_uuid4()

    register_mod.uuid4 = uuid4_with_cimd

    original_handle = register_mod.RegistrationHandler.handle

    async def handle(self, request):
        try:
            body = await request.body()
            data = json.loads(body)
            if data.get("token_endpoint_auth_method") in (
                None,
                "client_secret_post",
                "client_secret_basic",
            ):
                data["token_endpoint_auth_method"] = "none"
            grants = data.get("grant_types")
            if isinstance(grants, list):
                data["grant_types"] = [
                    g
                    for g in grants
                    if g != "urn:ietf:params:oauth:grant-type:jwt-bearer"
                ]
            client_id = data.get("client_id")
            if isinstance(client_id, str) and client_id.startswith("https://"):
                register_mod._gt_cimd_client_id = client_id
            from starlette.requests import Request as StarletteRequest

            patched = json.dumps(data).encode()

            async def receive():
                return {"type": "http.request", "body": patched, "more_body": False}

            request = StarletteRequest(request.scope, receive)
        except Exception:
            register_mod._gt_cimd_client_id = None
        return await original_handle(self, request)

    register_mod.RegistrationHandler.handle = handle
    auth_routes._gt_claude_patch = True


def _build_mcp():
    instructions = (
        "GroundTruth ad reporting. Call help_ground_truth first if unsure. "
        "Most reports need a campaign (numeric id or name) and a date range."
    )
    if not MCP_PUBLIC_URL:
        return FastMCP("ground-truth", instructions=instructions)

    _patch_claude_oauth_compat()

    from pydantic import AnyHttpUrl

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

    from ground_truth_oauth import ClaudeOAuthProvider

    provider = ClaudeOAuthProvider(
        base_url=MCP_PUBLIC_URL,
        password=MCP_OAUTH_PASSWORD,
    )
    # valid_scopes=None → accept whatever Claude sends during DCR (avoids ofid_ register failures).
    # resource_server_url MUST be the MCP path (/mcp) so RFC 9728 metadata is served at
    # /.well-known/oauth-protected-resource/mcp (Claude uses the connector URL as the resource).
    auth = AuthSettings(
        issuer_url=AnyHttpUrl(MCP_PUBLIC_URL),
        resource_server_url=AnyHttpUrl(MCP_RESOURCE_URL),
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=None,
            default_scopes=["ground-truth"],
        ),
        required_scopes=["ground-truth"],
    )
    return FastMCP(
        "ground-truth",
        instructions=instructions,
        auth_server_provider=provider,
        auth=auth,
    )


mcp = _build_mcp()


def _gt():
    return gt_client()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_date(value: str, *, default: Optional[date] = None) -> date:
    raw = (value or "").strip()
    if not raw:
        if default is None:
            raise ValueError("date required (YYYY-MM-DD)")
        return default
    lowered = raw.lower()
    today = date.today()
    if lowered == "today":
        return today
    if lowered == "yesterday":
        return today - timedelta(days=1)
    if lowered.startswith("last_") and lowered.endswith("_days"):
        return today - timedelta(days=int(lowered[5:-5]))
    return datetime.strptime(raw[:10], "%Y-%m-%d").date()


def _is_relative_preset(value: str) -> bool:
    lowered = (value or "").strip().lower()
    return lowered.startswith("last_") and lowered.endswith("_days")


def _date_range(start_date: str, end_date: str = "") -> tuple[date, date]:
    if _is_relative_preset(start_date) and not (end_date or "").strip():
        # last_N_days → the N full days ending yesterday.
        end = date.today() - timedelta(days=1)
        days = int(start_date.strip().lower()[5:-5])
        return end - timedelta(days=max(days, 1) - 1), end
    start = _parse_date(start_date)
    end = _parse_date(end_date, default=start)
    if end < start:
        start, end = end, start
    return start, end


def _default_range(start_date: str, end_date: str) -> tuple[date, date]:
    """Empty start_date → last 30 days ending yesterday."""
    if not (start_date or "").strip():
        end = date.today() - timedelta(days=1)
        return end - timedelta(days=29), end
    return _date_range(start_date, end_date)


def _fmt_int(n: Any) -> str:
    try:
        return f"{int(float(n)):,}"
    except (TypeError, ValueError):
        return "0"


def _fmt_float(n: Any, *, decimals: int = 2) -> str:
    try:
        return f"{float(n):,.{decimals}f}"
    except (TypeError, ValueError):
        return "0.00"


def _fmt_money(n: Any) -> str:
    return f"${_fmt_float(n)}"


def _fmt_pct(n: Any) -> str:
    return f"{_fmt_float(n)}%"


def _table_lines(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "No rows matched."
    lines = [" | ".join(headers)]
    lines.append(" | ".join("---" for _ in headers))
    for row in rows:
        lines.append(" | ".join(str(c) for c in row))
    return "\n".join(lines)


def _clamp(limit: int, hi: int) -> int:
    return max(1, min(int(limit or hi), hi))


def _span(start: date, end: date) -> str:
    return f"{start.isoformat()} to {end.isoformat()}"


def _safe(fn):
    """Turn API/config errors into readable tool output instead of MCP errors."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (GroundTruthError, RuntimeError, ValueError, KeyError) as e:
            return f"Error: {e}"

    return wrapper


def _campaign(campaign: str, account_id: str = "") -> str:
    return _gt().resolve_campaign_id(campaign, account_id=account_id)


_CORE = ("impressions", "clicks", "spend", "visits", "secondary_actions")


def _sum(rows: list[dict[str, Any]], names=_CORE) -> dict[str, float]:
    totals = {m: 0.0 for m in names}
    for r in rows:
        for m in names:
            totals[m] += metric(r, m)
    return totals


def _derived(t: dict[str, float]) -> list[str]:
    out = []
    imps, clicks, spend = t.get("impressions", 0), t.get("clicks", 0), t.get("spend", 0)
    if imps:
        out.append(f"CTR: {_fmt_pct(clicks / imps * 100)}")
        out.append(f"CPM: {_fmt_money(spend / imps * 1000)}")
    if clicks:
        out.append(f"CPC: {_fmt_money(spend / clicks)}")
    if t.get("visits"):
        out.append(f"Cost per visit: {_fmt_money(spend / t['visits'])}")
    return out


def _group_table(
    rows: list[dict[str, Any]],
    label_fn,
    *,
    header: str,
    limit: int,
    sort_by: str = "impressions",
    metrics=("impressions", "clicks", "ctr_calc", "spend", "visits"),
) -> str:
    buckets: dict[str, dict[str, float]] = defaultdict(lambda: {m: 0.0 for m in _CORE})
    for r in rows:
        label = str(label_fn(r) or "(unknown)")
        for m in _CORE:
            buckets[label][m] += metric(r, m)
    ranked = sorted(buckets.items(), key=lambda kv: kv[1].get(sort_by, 0), reverse=True)
    ranked = ranked[:limit]

    names = {
        "impressions": "Impressions",
        "clicks": "Clicks",
        "ctr_calc": "CTR",
        "spend": "Spend",
        "visits": "Visits",
        "secondary_actions": "Sec. actions",
    }
    body = []
    for label, v in ranked:
        row: list[Any] = [label[:60]]
        for m in metrics:
            if m == "ctr_calc":
                row.append(_fmt_pct(v["clicks"] / v["impressions"] * 100) if v["impressions"] else "—")
            elif m == "spend":
                row.append(_fmt_money(v["spend"]))
            else:
                row.append(_fmt_int(v[m]))
        body.append(row)
    return _table_lines([header] + [names[m] for m in metrics], body)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def help_ground_truth() -> str:
    """Explain what this GroundTruth connector can answer in simple language."""
    return """You can ask things like:

ACCOUNTS & CAMPAIGNS
- "List my GroundTruth accounts" / "Spend across all accounts last month"
- "List campaigns for account 12345" / "Find campaigns named Summer"
- "Is the GroundTruth integration configured?"

PERFORMANCE
- "Summary for the org last 30 days"
- "Account 12345 totals for August"
- "Campaign Summer Sale summary from 2026-08-01 to 2026-08-31"
- "Daily trend for campaign 98765 last 14 days"
- "Ad group performance for campaign Summer Sale"
- "Top creatives for campaign 98765"

BREAKDOWNS (per campaign)
- Locations: state / DMA / zipcode / county
- Demographics: age / gender / age × gender
- Device type, hour of day (time of day)
- Audiences: behavioral audience / category / brand affinity
- Inventory: publisher / network
- Store visits by POI / product (location-level visits)
- Audio: streaming genre / podcast topic / podcast series / audio publisher

CONVERSIONS
- "Conversion tracking for account 12345 last week"

Dates are YYYY-MM-DD, or today / yesterday / last_7_days / last_30_days.
Any range works: the API caps each request at 7 days, so longer ranges are
fetched week by week and combined automatically.
Campaign can be a numeric id or a name (names need GROUND_TRUTH_ORG_ID and
recent delivery; use the numeric id for older campaigns).
GroundTruth metrics: visits = attributed store visits, secondary actions =
click-to-call / directions / website / more-info / coupon."""


@mcp.tool()
def get_integration_status() -> str:
    """Check GroundTruth API credentials and connectivity (no secrets exposed)."""
    c = _gt()
    lines = [
        "GroundTruth Reporting API",
        f"Base URL: {c.base_url}",
        f"User ID: {'configured' if c.user_id else 'missing GROUND_TRUTH_USER_ID'}",
        f"API key: {'configured' if c.api_key else 'missing GROUND_TRUTH_API_KEY'}",
        f"Organization ID: {c.org_id or 'not set (GROUND_TRUTH_ORG_ID) — needed for account/campaign lists'}",
        f"Default account: {c.default_account or 'not set (GROUND_TRUTH_ACCOUNT_ID)'}",
    ]
    try:
        c.validate_config()
    except RuntimeError as e:
        lines.append(f"Status: {e}")
        return "\n".join(lines)
    if not c.org_id:
        lines.append("Status: credentials present; set GROUND_TRUTH_ORG_ID to run a live check.")
        return "\n".join(lines)
    try:
        end = date.today() - timedelta(days=1)
        rows = c.org_totals(end - timedelta(days=6), end)
        accounts = {str(r.get("account_id")) for r in rows if r.get("account_id")}
        lines.append(
            f"Status: OK — last 7 days returned {len(rows)} campaign row(s) "
            f"across {len(accounts)} account(s)."
        )
    except (GroundTruthError, ValueError) as e:
        lines.append(f"Status: error — {e}")
    return "\n".join(lines)


@mcp.tool()
@_safe
def list_accounts(
    start_date: str = "",
    end_date: str = "",
    organization_id: str = "",
    limit: int = 100,
) -> str:
    """List GroundTruth ad accounts in the organization with spend for a date range (default last 30 days)."""
    start, end = _default_range(start_date, end_date)
    rows = _gt().list_accounts(start, end, organization_id)[: _clamp(limit, 500)]
    if not rows:
        return f"No accounts with data for {_span(start, end)}."
    body = [
        [
            r["account_id"],
            r["account_name"][:40],
            r["account_status"] or "—",
            r["campaigns"],
            _fmt_money(r["spend"]),
            _fmt_int(r["impressions"]),
            _fmt_int(r["clicks"]),
            _fmt_int(r["visits"]),
            r["currency"] or "",
        ]
        for r in rows
    ]
    total = sum(r["spend"] for r in rows)
    return (
        f"{len(rows)} account(s) · {_span(start, end)} · total spend {_fmt_money(total)}\n"
        + _table_lines(
            ["Account ID", "Name", "Status", "Campaigns", "Spend", "Impressions", "Clicks", "Visits", "Currency"],
            body,
        )
    )


@mcp.tool()
@_safe
def list_campaigns(
    search: str = "",
    account_id: str = "",
    start_date: str = "",
    end_date: str = "",
    limit: int = 100,
) -> str:
    """List campaigns (optionally filtered by account or name) with totals for a date range (default last 30 days)."""
    start, end = _default_range(start_date, end_date)
    rows = _gt().list_campaigns(start, end, account_id=account_id, search=search)
    rows.sort(key=lambda r: metric(r, "spend"), reverse=True)
    rows = rows[: _clamp(limit, 500)]
    if not rows:
        return f"No campaigns matched for {_span(start, end)}."
    body = [
        [
            r["campaign_id"],
            str(r.get("campaign_name") or "")[:45],
            str(r.get("account_name") or "")[:30],
            _fmt_money(metric(r, "spend")),
            _fmt_int(metric(r, "impressions")),
            _fmt_int(metric(r, "clicks")),
            _fmt_int(metric(r, "visits")),
        ]
        for r in rows
    ]
    return (
        f"{len(rows)} campaign(s) · {_span(start, end)}\n"
        + _table_lines(
            ["Campaign ID", "Campaign", "Account", "Spend", "Impressions", "Clicks", "Visits"], body
        )
    )


@mcp.tool()
@_safe
def get_org_summary(start_date: str = "", end_date: str = "", organization_id: str = "") -> str:
    """Organization-wide KPI totals across all accounts and campaigns."""
    start, end = _default_range(start_date, end_date)
    rows = _gt().org_totals(start, end, organization_id)
    if not rows:
        return f"No org data for {_span(start, end)}."
    t = _sum(rows)
    accounts = {str(r.get("account_id")) for r in rows if r.get("account_id")}
    lines = [
        "GroundTruth organization summary",
        f"Dates: {_span(start, end)}",
        f"Accounts: {len(accounts)} · Campaigns: {len(rows)}",
        f"Spend: {_fmt_money(t['spend'])}",
        f"Impressions: {_fmt_int(t['impressions'])}",
        f"Clicks: {_fmt_int(t['clicks'])}",
        f"Visits: {_fmt_int(t['visits'])}",
        f"Secondary actions: {_fmt_int(t['secondary_actions'])}",
    ]
    return "\n".join(lines + _derived(t))


@mcp.tool()
@_safe
def get_account_summary(account_id: str = "", start_date: str = "", end_date: str = "", limit: int = 50) -> str:
    """Account totals plus a per-campaign table (GroundTruth account totals report)."""
    start, end = _default_range(start_date, end_date)
    aid = account_id or _gt().default_account
    rows = _gt().account_totals(aid, start, end)
    if not rows:
        return f"No data for account {aid} in {_span(start, end)}."
    t = _sum(rows)
    name = rows[0].get("account_name") or aid
    lines = [
        f"Account summary: {name} ({aid})",
        f"Dates: {_span(start, end)}",
        f"Spend: {_fmt_money(t['spend'])}",
        f"Impressions: {_fmt_int(t['impressions'])}",
        f"Clicks: {_fmt_int(t['clicks'])}",
        f"Visits: {_fmt_int(t['visits'])}",
        f"Secondary actions: {_fmt_int(t['secondary_actions'])}",
        *_derived(t),
    ]
    if len(rows) > 1 or rows[0].get("campaign_name"):
        lines.append("")
        lines.append(
            _group_table(
                rows,
                lambda r: f"{r.get('campaign_name') or ''} ({str(r.get('campaign_id') or '').split('.')[0]})",
                header="Campaign",
                limit=_clamp(limit, 200),
                sort_by="spend",
            )
        )
    return "\n".join(lines)


@mcp.tool()
@_safe
def get_campaign_summary(campaign: str, start_date: str = "", end_date: str = "", account_id: str = "") -> str:
    """KPI totals for one campaign (id or name): spend, impressions, clicks, visits, reach, video, conversions."""
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    rows = _gt().campaign_totals(cid, start, end)
    if not rows:
        return f"No data for campaign {cid} in {_span(start, end)}."
    r0 = rows[0]
    t = _sum(rows, _CORE + ("reach", "video_completes", "conversions", "projected_visits"))
    lines = [
        f"Campaign: {r0.get('campaign_name') or cid} ({cid})",
        f"Account: {r0.get('account_name') or '—'}",
        f"Dates: {_span(start, end)}",
        f"Spend: {_fmt_money(t['spend'])}",
        f"Impressions: {_fmt_int(t['impressions'])}",
        (
            f"Reach: at least {_fmt_int(t['reach'])} (largest single {_gt().max_range_days}-day window)"
            if (end - start).days + 1 > _gt().max_range_days
            else f"Reach: {_fmt_int(t['reach'])}"
        ),
        f"Clicks: {_fmt_int(t['clicks'])}",
        f"Visits: {_fmt_int(t['visits'])} (projected {_fmt_int(t['projected_visits'])})",
        f"Secondary actions: {_fmt_int(t['secondary_actions'])}",
    ]
    if t["video_completes"]:
        lines.append(f"Video completes: {_fmt_int(t['video_completes'])}")
    if t["conversions"]:
        lines.append(f"Conversions: {_fmt_int(t['conversions'])}")
    for key, label in (
        ("click_to_call", "Click-to-call"),
        ("directions", "Directions"),
        ("website", "Website"),
        ("moreinfo", "More info"),
        ("coupon", "Coupon"),
    ):
        v = sum(metric(r, key) for r in rows)
        if v:
            lines.append(f"{label}: {_fmt_int(v)}")
    return "\n".join(lines + _derived(t))


@mcp.tool()
@_safe
def get_daily_trend(campaign: str, start_date: str = "", end_date: str = "", account_id: str = "") -> str:
    """Day-by-day spend, impressions, clicks, and visits for a campaign."""
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    rows = _gt().campaign_daily(cid, start, end)
    daily: dict[str, dict[str, float]] = defaultdict(lambda: {m: 0.0 for m in _CORE})
    for r in rows:
        d = str(r.get("date") or "")[:10]
        if not d:
            continue
        for m in _CORE:
            daily[d][m] += metric(r, m)
    if not daily:
        return f"No daily data for campaign {cid} in {_span(start, end)}."
    body = [
        [d, _fmt_money(v["spend"]), _fmt_int(v["impressions"]), _fmt_int(v["clicks"]), _fmt_int(v["visits"])]
        for d, v in sorted(daily.items())
    ]
    return (
        f"Daily trend · campaign {cid} · {_span(start, end)}\n"
        + _table_lines(["Date", "Spend", "Impressions", "Clicks", "Visits"], body)
    )


@mcp.tool()
@_safe
def get_adgroup_performance(
    campaign: str, start_date: str = "", end_date: str = "", account_id: str = "", limit: int = 50
) -> str:
    """Totals for every ad group in a campaign, ranked by spend."""
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    rows = [r for r in _gt().campaign_totals(cid, start, end, by="adgroup") if r.get("adgroup_id")]
    if not rows:
        return f"No ad group data for campaign {cid} in {_span(start, end)}."
    return f"Ad groups · campaign {cid} · {_span(start, end)}\n" + _group_table(
        rows,
        lambda r: f"{r.get('adgroup_name') or ''} ({str(r.get('adgroup_id')).split('.')[0]})",
        header="Ad group",
        limit=_clamp(limit, 200),
        sort_by="spend",
    )


@mcp.tool()
@_safe
def get_creative_performance(
    campaign: str,
    start_date: str = "",
    end_date: str = "",
    account_id: str = "",
    sort_by: str = "impressions",
    limit: int = 25,
    group_by: str = "asset",
) -> str:
    """
    Creatives in a campaign with image URL, size, ad groups, and performance, ranked by
    impressions, clicks, spend, or visits. group_by = asset (one row per image, combining
    copies across ad groups) | creative (one row per creative id / ad group).
    Landing-page (click-through) URLs are not exposed by the GroundTruth Reporting API.
    """
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    key = sort_by if sort_by in ("impressions", "clicks", "spend", "visits") else "impressions"
    per_creative = (group_by or "").strip().lower() in ("creative", "creative_id", "id")

    rows = [r for r in _gt().creatives_daily(cid, start, end) if r.get("creative_id")]
    if not rows:
        rows = [r for r in _gt().campaign_totals(cid, start, end, by="creative") if r.get("creative_id")]
        if not rows:
            return f"No creative data for campaign {cid} in {_span(start, end)}."
        return f"Creatives by {key} · campaign {cid} · {_span(start, end)}\n" + _group_table(
            rows,
            lambda r: f"{r.get('creative_name') or ''} ({str(r.get('creative_id')).split('.')[0]})",
            header="Creative",
            limit=_clamp(limit, 100),
            sort_by=key,
        )

    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        crid = str(r.get("creative_id")).split(".")[0]
        url = str(r.get("creative_url") or "")
        bkey = crid if per_creative else (url or crid)
        b = buckets.setdefault(
            bkey,
            {
                "name": r.get("creative_name") or "",
                "size": r.get("creative_size") or "",
                "url": url,
                "ids": set(),
                "adgroups": set(),
                **{m: 0.0 for m in _CORE},
            },
        )
        b["ids"].add(crid)
        if r.get("adgroup_name"):
            b["adgroups"].add(str(r["adgroup_name"]))
        for m in _CORE:
            b[m] += metric(r, m)

    ranked = sorted(buckets.values(), key=lambda b: b.get(key, 0), reverse=True)[: _clamp(limit, 100)]
    body = []
    for b in ranked:
        ids = sorted(b["ids"])
        label = f"{b['name'][:50]} ({ids[0]})" if len(ids) == 1 else f"{b['name'][:50]} ({len(ids)} ids)"
        adgroups = sorted(b["adgroups"])
        ag = ", ".join(adgroups) if len(adgroups) <= 3 else f"{len(adgroups)} ad groups"
        body.append(
            [
                label,
                b["size"] or "—",
                ag or "—",
                _fmt_int(b["impressions"]),
                _fmt_int(b["clicks"]),
                _fmt_pct(b["clicks"] / b["impressions"] * 100) if b["impressions"] else "—",
                _fmt_money(b["spend"]),
                _fmt_int(b["visits"]),
                b["url"] or "—",
            ]
        )
    unit = "creative" if per_creative else "creative asset"
    return (
        f"{len(buckets)} {unit}(s) by {key} · campaign {cid} · {_span(start, end)}\n"
        + _table_lines(
            ["Creative", "Size", "Ad groups", "Impressions", "Clicks", "CTR", "Spend", "Visits", "Image URL"],
            body,
        )
        + "\nLanding-page (click-through) URLs are not available from the GroundTruth Reporting API."
    )


@mcp.tool()
@_safe
def get_location_breakdown(
    campaign: str,
    location_type: str = "state",
    start_date: str = "",
    end_date: str = "",
    account_id: str = "",
    sort_by: str = "impressions",
    limit: int = 50,
) -> str:
    """Campaign performance by geography: location_type = state | dma | zipcode | county."""
    lt = (location_type or "state").strip().lower()
    lt = {"zip": "zipcode", "zip_code": "zipcode", "region": "state", "states": "state"}.get(lt, lt)
    if lt not in ("state", "dma", "zipcode", "county"):
        return "Unknown location_type. Use state | dma | zipcode | county."
    sort_key = sort_by if sort_by in ("impressions", "clicks", "ctr", "secondary_actions", "visits") else "impressions"
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    rows = _gt().campaign_locations(cid, start, end, location_type=lt, sort_metric=sort_key)
    if not rows:
        return f"No {lt} data for campaign {cid} in {_span(start, end)}."
    field = {"state": "state", "dma": "dma", "zipcode": "zip", "county": "county"}[lt]

    def label(r):
        v = r.get(field) or r.get(lt)
        if lt == "zipcode" and r.get("city"):
            return f"{v} ({r.get('city')}, {r.get('state') or ''})"
        return v

    return f"Campaign {cid} by {lt} · {_span(start, end)}\n" + _group_table(
        rows,
        label,
        header=lt.title(),
        limit=_clamp(limit, 500),
        sort_by="impressions" if sort_key == "ctr" else sort_key,
        metrics=("impressions", "clicks", "ctr_calc", "spend", "visits", "secondary_actions"),
    )


@mcp.tool()
@_safe
def get_demographics_breakdown(
    campaign: str, by: str = "age_gender", start_date: str = "", end_date: str = "", account_id: str = ""
) -> str:
    """Campaign performance by age, gender, or age × gender."""
    key = (by or "age_gender").strip().lower().replace("-", "_").replace(" ", "_")
    key = {"demographics": "age_gender", "demo": "age_gender", "agegender": "age_gender"}.get(key, key)
    if key not in ("age", "gender", "age_gender"):
        return "Unknown demographic. Use by=age | gender | age_gender."
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    rows = _gt().campaign_demographic(cid, start, end, by=key)
    if not rows:
        return f"No demographic data for campaign {cid} in {_span(start, end)}."

    def label(r):
        if key == "age_gender":
            return f"{r.get('age') or '?'} / {r.get('gender') or '?'}"
        return r.get(key)

    return f"Campaign {cid} by {key.replace('_', ' × ')} · {_span(start, end)}\n" + _group_table(
        rows,
        label,
        header=key.replace("_", " × ").title(),
        limit=200,
        metrics=("impressions", "clicks", "ctr_calc", "spend", "secondary_actions"),
    )


@mcp.tool()
@_safe
def get_device_breakdown(campaign: str, start_date: str = "", end_date: str = "", account_id: str = "") -> str:
    """Campaign performance by device / publisher type (app, web, CTV …)."""
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    rows = _gt().campaign_device_type(cid, start, end)
    if not rows:
        return f"No device data for campaign {cid} in {_span(start, end)}."
    return f"Campaign {cid} by device type · {_span(start, end)}\n" + _group_table(
        rows,
        lambda r: r.get("pub_type"),
        header="Device type",
        limit=50,
        metrics=("impressions", "clicks", "ctr_calc", "spend"),
    )


@mcp.tool()
@_safe
def get_hourly_performance(
    campaign: str, start_date: str = "", end_date: str = "", account_id: str = "", level: str = "campaign"
) -> str:
    """Time-of-day (hour 0–23) performance for a campaign; level = campaign | adgroup | creative."""
    lvl = level if level in ("campaign", "adgroup", "creative") else "campaign"
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    rows = _gt().campaign_time_of_day(cid, start, end, level=lvl)
    if not rows:
        return f"No time-of-day data for campaign {cid} in {_span(start, end)}."
    hours: dict[int, dict[str, float]] = defaultdict(lambda: {m: 0.0 for m in _CORE})
    for r in rows:
        try:
            h = int(float(r.get("hr")))
        except (TypeError, ValueError):
            continue
        for m in _CORE:
            hours[h][m] += metric(r, m)
    body = [
        [
            f"{h:02d}:00",
            _fmt_int(v["impressions"]),
            _fmt_int(v["clicks"]),
            _fmt_pct(v["clicks"] / v["impressions"] * 100) if v["impressions"] else "—",
            _fmt_int(v["visits"]),
            _fmt_int(v["secondary_actions"]),
        ]
        for h, v in sorted(hours.items())
    ]
    return (
        f"Time of day · campaign {cid} · {_span(start, end)}\n"
        + _table_lines(["Hour", "Impressions", "Clicks", "CTR", "Visits", "Sec. actions"], body)
    )


_DIMENSIONS = {
    "behavioral_audience": ("behavioral_audience", lambda r: r.get("a_name") or r.get("a_id"), "Audience"),
    "audience": ("behavioral_audience", lambda r: r.get("a_name") or r.get("a_id"), "Audience"),
    "category": ("category", lambda r: r.get("c_name") or r.get("sic"), "Category"),
    "brand_affinity": ("brand_affinity", lambda r: r.get("b_name") or r.get("b_id"), "Brand"),
    "brand": ("brand_affinity", lambda r: r.get("b_name") or r.get("b_id"), "Brand"),
    "publisher": ("publisher", lambda r: r.get("publisher_name"), "Publisher"),
    "network": ("network", lambda r: r.get("network_name"), "Network"),
    "streaming_genre": ("audio/streaming_genre", lambda r: r.get("genre") or r.get("streaming_genre"), "Genre"),
    "podcast_topic": ("audio/podcast_topic", lambda r: r.get("topic"), "Podcast topic"),
    "podcast_series": ("audio/podcast_series", lambda r: r.get("series"), "Podcast series"),
    "audio_publisher": ("audio/publisher", lambda r: r.get("app_site_publisher_name"), "Audio publisher"),
}


@mcp.tool()
@_safe
def get_dimension_breakdown(
    campaign: str,
    dimension: str,
    start_date: str = "",
    end_date: str = "",
    account_id: str = "",
    level: str = "campaign",
    limit: int = 50,
) -> str:
    """
    Campaign breakdown by audience / inventory dimension.

    dimension = behavioral_audience | category | brand_affinity | publisher | network |
    streaming_genre | podcast_topic | podcast_series | audio_publisher.
    level = campaign | adgroup | creative (not every dimension supports every level).
    """
    spec = _DIMENSIONS.get((dimension or "").strip().lower())
    if not spec:
        return "Unknown dimension. Use: " + " | ".join(sorted(k for k in _DIMENSIONS if k not in ("audience", "brand")))
    path_dim, label_fn, header = spec
    lvl = level if level in ("campaign", "adgroup", "creative") else "campaign"
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    rows = _gt().campaign_dimension(path_dim, cid, start, end, level=lvl)
    if not rows:
        return f"No {header.lower()} data for campaign {cid} in {_span(start, end)}."
    if lvl != "campaign":
        name_key = "adgroup_name" if lvl == "adgroup" else "creative_name"
        base = label_fn
        label_fn = lambda r: f"{base(r)} · {r.get(name_key) or ''}"  # noqa: E731
    return f"Campaign {cid} by {header.lower()} · {_span(start, end)}\n" + _group_table(
        rows,
        label_fn,
        header=header,
        limit=_clamp(limit, 500),
        metrics=("impressions", "clicks", "ctr_calc", "visits", "secondary_actions"),
    )


@mcp.tool()
@_safe
def get_poi_performance(
    campaign: str,
    start_date: str = "",
    end_date: str = "",
    account_id: str = "",
    sort_by: str = "visits",
    limit: int = 50,
) -> str:
    """Store-visit performance by point of interest (POI / product / store location) for a campaign."""
    start, end = _default_range(start_date, end_date)
    cid = _campaign(campaign, account_id)
    rows = _gt().campaign_dimension("product", cid, start, end)
    if not rows:
        return f"No POI data for campaign {cid} in {_span(start, end)}."

    def label(r):
        poi = r.get("poi_name") or r.get("product") or "(unknown)"
        where = ", ".join(str(x) for x in (r.get("city"), r.get("state")) if x)
        return f"{poi} — {where}" if where else poi

    key = sort_by if sort_by in ("impressions", "clicks", "visits", "secondary_actions") else "visits"
    return f"POI / store visits · campaign {cid} · {_span(start, end)}\n" + _group_table(
        rows,
        label,
        header="POI",
        limit=_clamp(limit, 500),
        sort_by=key,
        metrics=("impressions", "clicks", "ctr_calc", "visits", "secondary_actions"),
    )


@mcp.tool()
@_safe
def get_conversion_tracking(account_id: str = "", start_date: str = "", end_date: str = "", limit: int = 100) -> str:
    """Conversion tracking results for every ad group under an account."""
    start, end = _default_range(start_date, end_date)
    aid = account_id or _gt().default_account
    rows = _gt().conversion_tracking(aid, start, end)
    if not rows:
        return f"No conversion tracking data for account {aid} in {_span(start, end)}."
    keys = [k for k in rows[0].keys()][:10]
    body = [[str(r.get(k, ""))[:40] for k in keys] for r in rows[: _clamp(limit, 500)]]
    return f"Conversion tracking · account {aid} · {_span(start, end)}\n" + _table_lines(keys, body)


@mcp.tool()
@_safe
def raw_report(path: str, start_date: str = "", end_date: str = "", extra_params: str = "") -> str:
    """
    Call any GroundTruth Reporting API GET endpoint under /demand/ and return JSON.

    Use for endpoints without a dedicated tool, e.g.
    path="/demand/v1/adgroup/123/daily" or "/demand/v2/campaign/sv_locations/456".
    extra_params: query string like "all_creatives=1&location_type=dma".
    """
    import json
    from urllib.parse import parse_qsl

    params: dict[str, Any] = dict(parse_qsl(extra_params or ""))
    if start_date or end_date:
        start, end = _date_range(start_date or end_date, end_date)
        params["start_date"] = _gt().fmt_date(start)
        params["end_date"] = _gt().fmt_date(end)
    data = _gt().get(path, params)
    text = json.dumps(data, indent=1, default=str)
    if len(text) > 40000:
        text = text[:40000] + "\n… (truncated)"
    return text


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
    }


@mcp.custom_route("/.well-known/openid-configuration", methods=["GET", "OPTIONS"])
async def openid_configuration(request):
    """Claude falls back to OIDC discovery when RFC 8414 metadata is unavailable."""
    from starlette.responses import JSONResponse, Response

    if request.method == "OPTIONS":
        return Response(status_code=204, headers=_cors_headers())
    if not MCP_PUBLIC_URL:
        return JSONResponse({"error": "oauth_disabled"}, status_code=404)

    from pydantic import AnyHttpUrl

    from mcp.server.auth.routes import build_metadata
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions

    metadata = build_metadata(
        AnyHttpUrl(MCP_PUBLIC_URL.rstrip("/") + "/"),
        None,
        ClientRegistrationOptions(
            enabled=True,
            valid_scopes=None,
            default_scopes=["ground-truth"],
        ),
        RevocationOptions(),
    )
    return JSONResponse(
        metadata.model_dump(mode="json", exclude_none=True),
        headers={"Cache-Control": "public, max-age=3600", **_cors_headers()},
    )


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    """Public health check (no auth) — used to verify Cloud Run / OAuth readiness."""
    from starlette.responses import JSONResponse

    oauth_ready = bool(MCP_PUBLIC_URL)
    return JSONResponse(
        {
            "status": "ok",
            "service": "ground-truth",
            "oauth_enabled": oauth_ready,
            "oauth_token_mode": "signed_stateless",
            "oauth_clients_mode": "signed_stateless",
            "mcp_transport": "streamable-http (stateless)",
            "oauth_sessions_survive_deploys": bool(
                os.environ.get("MCP_OAUTH_JWT_SECRET", "").strip()
            ),
            "public_url": MCP_PUBLIC_URL or None,
            "mcp_url": MCP_RESOURCE_URL or None,
            "oauth_discovery": (
                f"{MCP_PUBLIC_URL}/.well-known/oauth-authorization-server"
                if oauth_ready
                else None
            ),
            "protected_resource_metadata": (
                f"{MCP_PUBLIC_URL}/.well-known/oauth-protected-resource/mcp"
                if oauth_ready
                else None
            ),
        }
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    on_cloud = bool(os.environ.get("K_SERVICE"))
    default_http = (
        os.environ.get("MCP_TRANSPORT", "").lower() == "http" or on_cloud
    )
    default_host = os.environ.get("HOST") or (
        "0.0.0.0" if (default_http or on_cloud) else "127.0.0.1"
    )
    default_port = int(os.environ.get("PORT", "8080" if on_cloud else "8001"))

    parser = argparse.ArgumentParser(
        description="Ground Truth MCP connector (GroundTruth Reporting API)."
    )
    parser.add_argument("--http", action="store_true", default=default_http)
    parser.add_argument("--host", default=default_host)
    parser.add_argument("--port", type=int, default=default_port)
    args = parser.parse_args()

    print("Ground Truth MCP → GroundTruth Reporting API", file=sys.stderr, flush=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    if MCP_PUBLIC_URL:
        print(f"OAuth for Claude: enabled (issuer={MCP_PUBLIC_URL})", file=sys.stderr, flush=True)
        if not os.environ.get("MCP_OAUTH_JWT_SECRET", "").strip():
            print(
                "WARNING: MCP_OAUTH_JWT_SECRET not set — Claude will be asked to reconnect "
                "whenever GROUND_TRUTH_API_KEY changes.",
                file=sys.stderr,
                flush=True,
            )
    elif on_cloud:
        print(
            "WARNING: MCP_PUBLIC_URL not set — Claude OAuth DCR is disabled. "
            "Set MCP_PUBLIC_URL=https://YOUR-SERVICE.run.app on Cloud Run.",
            file=sys.stderr,
            flush=True,
        )
    try:
        _gt().validate_config()
        print("GroundTruth credentials loaded.", file=sys.stderr, flush=True)
    except RuntimeError as e:
        # Do not crash the process — OAuth /health must stay up for Claude register.
        print(f"WARNING: {e}", file=sys.stderr, flush=True)

    tools = [
        "help_ground_truth",
        "get_integration_status",
        "list_accounts",
        "list_campaigns",
        "get_org_summary",
        "get_account_summary",
        "get_campaign_summary",
        "get_daily_trend",
        "get_adgroup_performance",
        "get_creative_performance",
        "get_location_breakdown",
        "get_demographics_breakdown",
        "get_device_breakdown",
        "get_hourly_performance",
        "get_dimension_breakdown",
        "get_poi_performance",
        "get_conversion_tracking",
        "raw_report",
    ]
    print("Tools: " + ", ".join(tools), file=sys.stderr, flush=True)

    try:
        if args.http:
            public = f"{MCP_PUBLIC_URL}/mcp" if MCP_PUBLIC_URL else f"http://{args.host}:{args.port}/mcp"
            print(
                f"\nHTTP mode (stateless)\n  MCP endpoint: {public}\n  Stop: Ctrl+C\n",
                file=sys.stderr,
                flush=True,
            )
            from mcp.server.transport_security import TransportSecuritySettings

            # stateless_http: no server-side MCP session ids, so Cloud Run restarts,
            # redeploys, and instance swaps never invalidate Claude's connection.
            # json_response: plain JSON replies instead of long-lived SSE streams that
            # Cloud Run's request timeout would cut off.
            mcp.run(
                transport="streamable-http",
                host=args.host,
                port=args.port,
                stateless_http=True,
                json_response=True,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=False
                ),
            )
        else:
            mcp.run()
    except KeyboardInterrupt:
        print("Stopped.", file=sys.stderr)
