"""
Facebook Graph API client for the Ground Truth MCP connector.

Auth prefers GROUND_TRUTH_ACCESS_TOKEN (user token with ads_read). Falls back to
app token via client_credentials when no user token is set.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

FB_API_VERSION = os.environ.get("GROUND_TRUTH_API_VERSION", "v21.0").strip() or "v21.0"
FB_BASE = f"https://graph.facebook.com/{FB_API_VERSION}"

logger = logging.getLogger("ground-truth-api")

# Core insight fields. Name fields are required when level is set — without them
# Graph returns blank campaign/adset/ad names (defect D-2 / D-3).
INSIGHT_FIELDS = [
    "campaign_id",
    "campaign_name",
    "adset_id",
    "adset_name",
    "ad_id",
    "ad_name",
    "impressions",
    "clicks",
    "inline_link_clicks",
    "spend",
    "cpc",
    "cpm",
    "ctr",
    "reach",
    "frequency",
    "actions",
    "action_values",
    "cost_per_action_type",
    "purchase_roas",
    "date_start",
    "date_stop",
]

# Prefer omni_purchase (consolidated). Do not sum with purchase — that double-counts.
PURCHASE_ACTION = "omni_purchase"
PURCHASE_FALLBACK = "purchase"


class MetaGraphError(Exception):
    """Facebook Graph API error."""


def _get_act(actions: Optional[list[dict]], action_type: str) -> float:
    if not actions:
        return 0.0
    for a in actions:
        if a.get("action_type") == action_type:
            try:
                return float(a.get("value") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _get_act_val(values: Optional[list[dict]], action_type: str) -> float:
    if not values:
        return 0.0
    for a in values:
        if a.get("action_type") == action_type:
            try:
                return float(a.get("value") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def extract_purchases(row: dict[str, Any]) -> tuple[int, float]:
    """Unpack purchases from nested actions / action_values (prefer omni_purchase)."""
    purchases = int(_get_act(row.get("actions"), PURCHASE_ACTION))
    value = _get_act_val(row.get("action_values"), PURCHASE_ACTION)
    if purchases == 0 and value == 0:
        purchases = int(_get_act(row.get("actions"), PURCHASE_FALLBACK))
        value = _get_act_val(row.get("action_values"), PURCHASE_FALLBACK)
    return purchases, value


def normalize_insight_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map Graph API insight row to MCP-friendly metric names."""
    purchases, purchase_value = extract_purchases(row)
    clicks = int(float(row.get("clicks") or 0))
    link_clicks = int(float(row.get("inline_link_clicks") or 0))
    return {
        "campaign_id": row.get("campaign_id") or "",
        "campaign_name": row.get("campaign_name") or "",
        "adset_id": row.get("adset_id") or "",
        "adset_name": row.get("adset_name") or "",
        "ad_id": row.get("ad_id") or "",
        "ad_name": row.get("ad_name") or "",
        "day": row.get("date_start") or row.get("date_stop") or "",
        "date_start": row.get("date_start") or "",
        "date_stop": row.get("date_stop") or "",
        "publisher_platform": row.get("publisher_platform") or "",
        "platform_position": row.get("platform_position") or "",
        "placement": row.get("platform_position") or row.get("publisher_platform") or "",
        "platform": row.get("publisher_platform") or "",
        # Geographic breakdowns (Meta Insights API: region ≈ state/province)
        "region": row.get("region") or "",
        "country": row.get("country") or "",
        "dma": row.get("dma") or "",
        # Demographic / hourly breakdowns
        "age": row.get("age") or "",
        "gender": row.get("gender") or "",
        "hourly_stats_aggregated_by_advertiser_time_zone": row.get(
            "hourly_stats_aggregated_by_advertiser_time_zone"
        )
        or "",
        "hourly_stats_aggregated_by_audience_time_zone": row.get(
            "hourly_stats_aggregated_by_audience_time_zone"
        )
        or "",
        "hour": (
            row.get("hourly_stats_aggregated_by_advertiser_time_zone")
            or row.get("hourly_stats_aggregated_by_audience_time_zone")
            or ""
        ),
        "amount_spent_usd": float(row.get("spend") or 0),
        "impressions": int(float(row.get("impressions") or 0)),
        "reach": int(float(row.get("reach") or 0)),
        "clicks_all": clicks,
        "inline_link_clicks": link_clicks,
        "purchases": purchases,
        "meta_purchases": purchases,
        "purchases_value": purchase_value,
        "meta_purchase_value": purchase_value,
        "frequency": float(row.get("frequency") or 0),
        "cpc": float(row.get("cpc") or 0),
        "cpm": float(row.get("cpm") or 0),
        "ctr": float(row.get("ctr") or 0),
    }


class MetaGraphClient:
    def __init__(self) -> None:
        self.app_id = (
            os.environ.get("GROUND_TRUTH_APP_ID", "").strip()
            or os.environ.get("FB_APP_ID", "").strip()
        )
        self.app_secret = (
            os.environ.get("GROUND_TRUTH_APP_SECRET", "").strip()
            or os.environ.get("FB_APP_SECRET", "").strip()
        )
        self.default_account = (
            os.environ.get("GROUND_TRUTH_AD_ACCOUNT_ID", "").strip()
            or os.environ.get("FB_AD_ACCOUNT_ID", "").strip()
        ).removeprefix("act_")
        self.user_access_token = (
            os.environ.get("GROUND_TRUTH_ACCESS_TOKEN", "").strip()
            or os.environ.get("FB_ACCESS_TOKEN", "").strip()
        )
        self._access_token: Optional[str] = None

    def validate_config(self) -> None:
        missing = []
        if not self.app_id:
            missing.append("GROUND_TRUTH_APP_ID")
        if not self.app_secret:
            missing.append("GROUND_TRUTH_APP_SECRET")
        if missing:
            raise RuntimeError(
                "Missing Facebook credentials in .env:\n"
                + "\n".join(f"  - {k}" for k in missing)
            )

    def resolve_account_id(self, account_id: str = "") -> str:
        raw = (account_id or self.default_account).strip().removeprefix("act_")
        if not raw:
            raise ValueError(
                "account_id is required (e.g. act_114810198697538) "
                "or set GROUND_TRUTH_AD_ACCOUNT_ID in .env"
            )
        return raw

    def exchange_token(self) -> str:
        """Obtain app access token from APP_ID + APP_SECRET (client_credentials)."""
        self.validate_config()
        params = {
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "grant_type": "client_credentials",
        }
        with httpx.Client(timeout=60.0) as client:
            res = client.get(f"{FB_BASE}/oauth/access_token", params=params)
            data = res.json() if res.content else {}

        if not res.is_success or not data.get("access_token"):
            err = data.get("error")
            if isinstance(err, dict):
                msg = err.get("message") or str(err)
                raise MetaGraphError(f"App token request failed: {msg}")
            raise MetaGraphError(
                f"App token request failed (HTTP {res.status_code}): {data}"
            )

        self._access_token = data["access_token"]
        return self._access_token

    def access_token(self) -> str:
        if self.user_access_token:
            return self.user_access_token
        if not self._access_token:
            return self.exchange_token()
        return self._access_token

    @staticmethod
    def _safe_log_url(url: str, params: Optional[dict[str, Any]] = None) -> str:
        """Loggable URL with secrets redacted (defect D-8)."""
        safe_params = dict(params or {})
        for key in ("access_token", "input_token", "client_secret"):
            if key in safe_params:
                safe_params[key] = "***"
        if safe_params:
            return f"{url}?{urlencode({k: str(v) for k, v in safe_params.items()})}"
        redacted = url
        for key in ("access_token", "input_token", "client_secret"):
            if f"{key}=" in redacted:
                # crude redact of query value
                parts = redacted.split(f"{key}=")
                if len(parts) > 1:
                    rest = parts[1]
                    amp = rest.find("&")
                    redacted = parts[0] + f"{key}=***" + (rest[amp:] if amp >= 0 else "")
        return redacted

    def _graph_get(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """
        GET Graph API.

        Always pass query params as a dict for relative paths. Never embed the
        query string in `path` — Graph silently drops malformed/duplicated
        params (root cause of D-1 / D-2 / D-4).
        """
        if path.startswith("http"):
            log_url = self._safe_log_url(path)
            logger.info("Graph GET %s", log_url)
            with httpx.Client(timeout=180.0) as client:
                res = client.get(path)
                data = res.json() if res.content else {}
        else:
            # Strip any accidental query fragment from path.
            clean_path = path.split("?", 1)[0].lstrip("/")
            q = dict(params or {})
            if "access_token" not in q:
                q["access_token"] = self.access_token()
            # Never send date_preset alongside time_range — date_preset wins.
            if "time_range" in q and "date_preset" in q:
                q.pop("date_preset", None)
            url = f"{FB_BASE}/{clean_path}"
            log_url = self._safe_log_url(url, q)
            logger.info("Graph GET %s", log_url)
            with httpx.Client(timeout=180.0) as client:
                res = client.get(url, params=q)
                data = res.json() if res.content else {}

        if not res.is_success:
            err = data.get("error")
            if isinstance(err, dict):
                code = err.get("code")
                msg = err.get("message") or str(err)
                if code == 190:
                    self._access_token = None
                    raise MetaGraphError(
                        f"Facebook auth failed: {msg}. Check GROUND_TRUTH_APP_ID / "
                        "GROUND_TRUTH_APP_SECRET / GROUND_TRUTH_ACCESS_TOKEN."
                    )
                raise MetaGraphError(f"Graph API error: {msg}")
            raise MetaGraphError(f"Graph API HTTP {res.status_code}: {data}")
        return data

    def _graph_post(
        self, path: str, params: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        clean_path = path.split("?", 1)[0].lstrip("/")
        q = dict(params or {})
        if "access_token" not in q:
            q["access_token"] = self.access_token()
        url = f"{FB_BASE}/{clean_path}"
        logger.info("Graph POST %s", self._safe_log_url(url, q))
        with httpx.Client(timeout=180.0) as client:
            res = client.post(url, data=q)
            data = res.json() if res.content else {}
        if not res.is_success:
            err = data.get("error")
            if isinstance(err, dict):
                raise MetaGraphError(f"Graph API error: {err.get('message') or err}")
            raise MetaGraphError(f"Graph API HTTP {res.status_code}: {data}")
        return data

    def _paginate(
        self, path: str, params: Optional[dict[str, Any]] = None, *, max_pages: int = 50
    ) -> list[dict[str, Any]]:
        """Follow paging.next until exhausted (defect D-6)."""
        rows: list[dict[str, Any]] = []
        data = self._graph_get(path, params)
        rows.extend(data.get("data") or [])
        pages = 1
        next_url = (data.get("paging") or {}).get("next")
        while next_url and pages < max_pages:
            data = self._graph_get(next_url)
            batch = data.get("data") or []
            if not batch:
                break
            rows.extend(batch)
            next_url = (data.get("paging") or {}).get("next")
            pages += 1
        return rows

    def fetch_insights(
        self,
        account_id: str,
        *,
        level: str,
        since: date,
        until: date,
        breakdowns: Optional[str] = None,
        time_increment: Any = 1,
        limit: int = 500,
        filtering: Optional[list[dict[str, Any]]] = None,
        object_id: str = "",
        use_async: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Paginated insights fetch with correct Graph params (D-1..D-4, D-7, D-8).

        time_range MUST be a JSON string. level / breakdowns / time_increment /
        fields must be sent as query params (not baked into a broken path).
        """
        act = self.resolve_account_id(account_id)
        # JSON string — raw dict is silently discarded by Graph (D-1).
        time_range = json.dumps(
            {"since": since.isoformat(), "until": until.isoformat()},
            separators=(",", ":"),
        )
        params: dict[str, Any] = {
            "fields": ",".join(INSIGHT_FIELDS),
            "level": level,
            "time_range": time_range,
            "time_increment": time_increment,
            "limit": min(limit, 500),
            "action_attribution_windows": json.dumps(
                ["7d_click", "1d_view"], separators=(",", ":")
            ),
        }
        if breakdowns:
            params["breakdowns"] = breakdowns
        if filtering:
            params["filtering"] = json.dumps(filtering)

        edge = f"{object_id}/insights" if object_id else f"act_{act}/insights"

        # Wide ad-level ranges: prefer async report run (D-7).
        day_span = (until - since).days
        if use_async or (level == "ad" and day_span > 14 and not object_id):
            try:
                return self._fetch_insights_async(edge, params)
            except MetaGraphError as exc:
                logger.warning("Async insights failed (%s); falling back to sync", exc)

        raw = self._paginate(edge, params)
        return [normalize_insight_row(r) for r in raw]

    def _fetch_insights_async(
        self, edge: str, params: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """POST /insights → report_run_id → poll until complete (D-7)."""
        post_params = dict(params)
        data = self._graph_post(edge, post_params)
        run_id = data.get("report_run_id") or data.get("id")
        if not run_id:
            raise MetaGraphError(f"Async insights did not return report_run_id: {data}")

        # Poll job status
        for _ in range(60):
            status = self._graph_get(
                str(run_id),
                {"fields": "async_status,async_percent_completion"},
            )
            state = (status.get("async_status") or "").upper()
            if state == "JOB_COMPLETED":
                break
            if state in ("JOB_FAILED", "JOB_SKIPPED"):
                raise MetaGraphError(f"Async insights job failed: {status}")
            time.sleep(2)
        else:
            raise MetaGraphError("Async insights job timed out")

        raw = self._paginate(f"{run_id}/insights", {"limit": 500})
        return [normalize_insight_row(r) for r in raw]

    def resolve_campaign_id(self, account_id: str, campaign: str) -> Optional[str]:
        """Resolve campaign name (or id) to Graph campaign id (D-5)."""
        q = (campaign or "").strip()
        if not q:
            return None
        if q.isdigit() or q.startswith("120"):
            return q.removeprefix("act_")

        rows = self.list_campaign_objects(account_id=account_id, search=q, limit=200)
        q_lower = q.lower()
        exact = [r for r in rows if (r.get("name") or "").lower() == q_lower]
        if exact:
            return exact[0].get("id")
        partial = [
            r for r in rows if q_lower in (r.get("name") or "").lower()
        ]
        if len(partial) == 1:
            return partial[0].get("id")
        if partial:
            # Prefer ACTIVE exact-ish match
            active = [
                r
                for r in partial
                if (r.get("status") or "").upper() == "ACTIVE"
            ]
            if len(active) == 1:
                return active[0].get("id")
            return partial[0].get("id")
        return None

    def fetch_insights_for_campaign(
        self,
        account_id: str,
        campaign: str,
        *,
        level: str,
        since: date,
        until: date,
        breakdowns: Optional[str] = None,
        time_increment: Any = 1,
    ) -> list[dict[str, Any]]:
        """Fetch insights filtered to one campaign (D-5)."""
        campaign_id = self.resolve_campaign_id(account_id, campaign)
        if campaign_id:
            return self.fetch_insights(
                account_id,
                level=level,
                since=since,
                until=until,
                breakdowns=breakdowns,
                time_increment=time_increment,
                object_id=campaign_id,
            )
        # Fallback: account-level filtering by campaign name
        return self.fetch_insights(
            account_id,
            level=level,
            since=since,
            until=until,
            breakdowns=breakdowns,
            time_increment=time_increment,
            filtering=[
                {
                    "field": "campaign.name",
                    "operator": "CONTAIN",
                    "value": campaign.strip(),
                }
            ],
        )

    _AD_ACCOUNT_FIELDS = (
        "account_id,name,account_status,currency,"
        "business{id,name},owner,parent_advertiser_id"
    )

    @staticmethod
    def account_status_label(status: Any) -> str:
        labels = {
            1: "ACTIVE",
            2: "DISABLED",
            3: "UNSETTLED",
            7: "PENDING_RISK_REVIEW",
            8: "PENDING_SETTLEMENT",
            9: "IN_GRACE_PERIOD",
            100: "PENDING_CLOSURE",
            101: "CLOSED",
            201: "ANY_ACTIVE",
            202: "ANY_CLOSED",
        }
        try:
            code = int(status)
        except (TypeError, ValueError):
            return str(status or "")
        return labels.get(code, str(code))

    def _normalize_ad_account_row(
        self,
        row: dict[str, Any],
        *,
        relationship: str,
        parent_business: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        account_id = (row.get("account_id") or str(row.get("id") or "")).removeprefix(
            "act_"
        )
        biz = row.get("business") if isinstance(row.get("business"), dict) else {}
        parent = parent_business if isinstance(parent_business, dict) else biz
        return {
            "account_id": account_id,
            "name": row.get("name") or "",
            "currency": row.get("currency") or "",
            "account_status": row.get("account_status"),
            "account_status_label": self.account_status_label(row.get("account_status")),
            "relationship": relationship,
            "parent_business_id": (parent or {}).get("id") or "",
            "parent_business_name": (parent or {}).get("name") or "",
            "owner": row.get("owner") or "",
            "parent_advertiser_id": row.get("parent_advertiser_id") or "",
        }

    def list_ad_accounts(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.list_all_ad_accounts(limit=limit)

    def list_all_ad_accounts(self, limit: int = 50) -> list[dict[str, Any]]:
        """List ad accounts with parent Business Manager."""
        cap = max(1, min(limit, 500))
        fields = self._AD_ACCOUNT_FIELDS
        seen: set[str] = set()
        rows: list[dict[str, Any]] = []

        def _append(
            items: list[dict[str, Any]],
            relationship: str,
            parent_business: Optional[dict[str, Any]] = None,
        ) -> None:
            for item in items:
                normalized = self._normalize_ad_account_row(
                    item, relationship=relationship, parent_business=parent_business
                )
                aid = normalized["account_id"]
                if not aid or aid in seen:
                    continue
                seen.add(aid)
                rows.append(normalized)

        try:
            data = self._graph_get(
                "me/adaccounts", {"fields": fields, "limit": min(cap, 100)}
            )
            _append(data.get("data") or [], "user")
            next_url = (data.get("paging") or {}).get("next")
            while next_url and len(rows) < cap:
                data = self._graph_get(next_url)
                _append(data.get("data") or [], "user")
                next_url = (data.get("paging") or {}).get("next")
            if rows:
                return rows[:cap]
        except MetaGraphError:
            pass

        act = self.default_account
        if not act:
            return rows[:cap]

        try:
            seed = self._graph_get(f"act_{act}", {"fields": fields})
        except MetaGraphError as exc:
            if not self.user_access_token:
                raise MetaGraphError(
                    f"{exc} Set GROUND_TRUTH_ACCESS_TOKEN (ads_read) in .env, "
                    "or grant this app ads_read on the ad account in Business Manager."
                ) from exc
            raise

        biz = seed.get("business") if isinstance(seed.get("business"), dict) else {}
        biz_id = (biz.get("id") or "").strip()
        parent = {"id": biz_id, "name": biz.get("name") or ""} if biz_id else None

        if biz_id:
            for relationship, edge in (
                ("owned", "owned_ad_accounts"),
                ("client", "client_ad_accounts"),
            ):
                try:
                    batch = self._paginate(
                        f"{biz_id}/{edge}",
                        {"fields": fields, "limit": min(cap, 100)},
                    )
                    _append(batch, relationship, parent)
                except MetaGraphError:
                    continue

        if not rows:
            _append([seed], "default", parent)

        return rows[:cap]

    def list_accounts_with_spend(
        self,
        since: date,
        until: date,
        *,
        account_limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List ad accounts with account-level spend for a date range."""
        accounts = self.list_all_ad_accounts(limit=account_limit)
        time_range = json.dumps(
            {"since": since.isoformat(), "until": until.isoformat()},
            separators=(",", ":"),
        )
        results: list[dict[str, Any]] = []
        for acct in accounts:
            aid = acct["account_id"]
            spend = 0.0
            impressions = 0
            clicks = 0
            try:
                data = self._graph_get(
                    f"act_{aid}/insights",
                    {
                        "fields": "spend,impressions,clicks",
                        "level": "account",
                        "time_range": time_range,
                        "time_increment": "all_days",
                        "limit": 1,
                    },
                )
                rows = data.get("data") or []
                if rows:
                    spend = float(rows[0].get("spend") or 0)
                    impressions = int(float(rows[0].get("impressions") or 0))
                    clicks = int(float(rows[0].get("clicks") or 0))
            except MetaGraphError:
                pass
            results.append(
                {
                    **acct,
                    "account_id": f"act_{aid}",
                    "spend": round(spend, 2),
                    "impressions": impressions,
                    "clicks": clicks,
                }
            )
        results.sort(key=lambda row: row.get("spend", 0), reverse=True)
        return results

    def list_campaign_objects(
        self, account_id: str = "", search: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        act = self.resolve_account_id(account_id)
        params: dict[str, Any] = {
            "fields": (
                "id,name,status,effective_status,objective,"
                "daily_budget,lifetime_budget,budget_remaining,"
                "bid_strategy,start_time,stop_time"
            ),
            "limit": min(max(limit, 1), 200),
        }
        if search.strip():
            params["filtering"] = json.dumps(
                [
                    {
                        "field": "name",
                        "operator": "CONTAIN",
                        "value": search.strip(),
                    }
                ]
            )
        rows = self._paginate(f"act_{act}/campaigns", params)
        return rows[:limit] if limit else rows

    def list_adset_objects(
        self, account_id: str = "", search: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        act = self.resolve_account_id(account_id)
        params: dict[str, Any] = {
            "fields": (
                "id,name,status,effective_status,campaign_id,"
                "daily_budget,lifetime_budget,budget_remaining,"
                "optimization_goal,billing_event,bid_amount,"
                "targeting,start_time,end_time"
            ),
            "limit": min(max(limit, 1), 200),
        }
        if search.strip():
            params["filtering"] = json.dumps(
                [
                    {
                        "field": "name",
                        "operator": "CONTAIN",
                        "value": search.strip(),
                    }
                ]
            )
        rows = self._paginate(f"act_{act}/adsets", params)
        return rows[:limit] if limit else rows

    def list_ad_creatives(
        self,
        account_id: str = "",
        search: str = "",
        limit: int = 50,
        *,
        include_preview: bool = True,
    ) -> list[dict[str, Any]]:
        """List ads with creative thumbnail / preview info."""
        act = self.resolve_account_id(account_id)
        params: dict[str, Any] = {
            "fields": (
                "id,name,status,effective_status,adset_id,campaign_id,"
                "creative{id,name,thumbnail_url,image_url,object_type,"
                "title,body,call_to_action_type,link_url,video_id}"
            ),
            "limit": min(max(limit, 1), 100),
        }
        if search.strip():
            params["filtering"] = json.dumps(
                [
                    {
                        "field": "name",
                        "operator": "CONTAIN",
                        "value": search.strip(),
                    }
                ]
            )
        rows = self._paginate(f"act_{act}/ads", params)
        out: list[dict[str, Any]] = []
        for row in rows[:limit]:
            creative = row.get("creative") if isinstance(row.get("creative"), dict) else {}
            ad_id = str(row.get("id") or "")
            preview_url = ""
            if include_preview and ad_id:
                try:
                    prev = self._graph_get(
                        f"{ad_id}/previews",
                        {"ad_format": "DESKTOP_FEED_STANDARD"},
                    )
                    bodies = prev.get("data") or []
                    if bodies:
                        # iframe HTML; extract src if present
                        html = bodies[0].get("body") or ""
                        preview_url = html
                        if 'src="' in html:
                            preview_url = html.split('src="', 1)[1].split('"', 1)[0]
                except MetaGraphError:
                    preview_url = ""
            out.append(
                {
                    "ad_id": ad_id,
                    "ad_name": row.get("name") or "",
                    "status": row.get("effective_status") or row.get("status") or "",
                    "adset_id": row.get("adset_id") or "",
                    "campaign_id": row.get("campaign_id") or "",
                    "creative_id": creative.get("id") or "",
                    "creative_name": creative.get("name") or "",
                    "thumbnail_url": creative.get("thumbnail_url") or "",
                    "image_url": creative.get("image_url") or "",
                    "title": creative.get("title") or "",
                    "body": creative.get("body") or "",
                    "cta": creative.get("call_to_action_type") or "",
                    "link_url": creative.get("link_url") or "",
                    "video_id": creative.get("video_id") or "",
                    "preview_url": preview_url,
                    "ads_manager_url": (
                        f"https://www.facebook.com/adsmanager/manage/ads"
                        f"?act={act}&selected_ad_ids={ad_id}"
                        if ad_id
                        else ""
                    ),
                }
            )
        return out

    def get_adset_targeting(
        self, account_id: str = "", search: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return ad sets with a readable targeting summary."""
        rows = self.list_adset_objects(
            account_id=account_id, search=search, limit=limit
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            targeting = row.get("targeting") if isinstance(row.get("targeting"), dict) else {}
            out.append(
                {
                    "adset_id": row.get("id") or "",
                    "adset_name": row.get("name") or "",
                    "status": row.get("effective_status") or row.get("status") or "",
                    "campaign_id": row.get("campaign_id") or "",
                    "optimization_goal": row.get("optimization_goal") or "",
                    "targeting_summary": self._summarize_targeting(targeting),
                    "targeting": targeting,
                }
            )
        return out

    @staticmethod
    def _summarize_targeting(targeting: dict[str, Any]) -> str:
        if not targeting:
            return "(no targeting)"
        parts: list[str] = []
        age_min = targeting.get("age_min")
        age_max = targeting.get("age_max")
        if age_min or age_max:
            parts.append(f"age {age_min or '?'}-{age_max or '?'}")
        genders = targeting.get("genders")
        if genders:
            gmap = {1: "male", 2: "female"}
            parts.append(
                "gender "
                + ",".join(gmap.get(int(g), str(g)) for g in genders)
            )
        geos = targeting.get("geo_locations") or {}
        countries = geos.get("countries") or []
        regions = geos.get("regions") or []
        cities = geos.get("cities") or []
        if countries:
            parts.append("countries " + ",".join(str(c) for c in countries[:8]))
        if regions:
            names = [
                (r.get("name") if isinstance(r, dict) else str(r)) for r in regions[:6]
            ]
            parts.append("regions " + ",".join(names))
        if cities:
            names = [
                (c.get("name") if isinstance(c, dict) else str(c)) for c in cities[:6]
            ]
            parts.append("cities " + ",".join(names))
        for key, label in (
            ("flexible_spec", "interests/behaviors"),
            ("interests", "interests"),
            ("behaviors", "behaviors"),
            ("custom_audiences", "custom audiences"),
            ("excluded_custom_audiences", "excluded audiences"),
            ("publisher_platforms", "platforms"),
            ("facebook_positions", "FB positions"),
            ("instagram_positions", "IG positions"),
            ("device_platforms", "devices"),
        ):
            val = targeting.get(key)
            if not val:
                continue
            if key == "flexible_spec" and isinstance(val, list):
                names: list[str] = []
                for block in val[:3]:
                    if not isinstance(block, dict):
                        continue
                    for k in ("interests", "behaviors", "life_events", "family_statuses"):
                        for item in block.get(k) or []:
                            if isinstance(item, dict) and item.get("name"):
                                names.append(str(item["name"]))
                if names:
                    parts.append(f"{label}: " + ", ".join(names[:10]))
            elif isinstance(val, list):
                names = []
                for item in val[:8]:
                    if isinstance(item, dict):
                        names.append(str(item.get("name") or item.get("id") or item))
                    else:
                        names.append(str(item))
                parts.append(f"{label}: " + ", ".join(names))
        return "; ".join(parts) if parts else json.dumps(targeting)[:240]

    @staticmethod
    def _budget_display(raw: Any) -> str:
        """Meta budgets are in account currency minor units (cents)."""
        try:
            cents = float(raw)
        except (TypeError, ValueError):
            return ""
        if cents <= 0:
            return ""
        return f"{cents / 100:,.2f}"

    def list_campaign_budgets(
        self, account_id: str = "", search: str = "", limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self.list_campaign_objects(
            account_id=account_id, search=search, limit=limit
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "campaign_id": row.get("id") or "",
                    "campaign_name": row.get("name") or "",
                    "status": row.get("status") or "",
                    "effective_status": row.get("effective_status") or "",
                    "objective": row.get("objective") or "",
                    "daily_budget": self._budget_display(row.get("daily_budget")),
                    "lifetime_budget": self._budget_display(row.get("lifetime_budget")),
                    "budget_remaining": self._budget_display(row.get("budget_remaining")),
                    "bid_strategy": row.get("bid_strategy") or "",
                    "start_time": row.get("start_time") or "",
                    "stop_time": row.get("stop_time") or "",
                }
            )
        return out

    def update_object_status(
        self, object_id: str, status: str
    ) -> dict[str, Any]:
        """Pause or activate a campaign / ad set / ad (needs ads_management)."""
        oid = (object_id or "").strip()
        if not oid:
            raise MetaGraphError("object_id is required")
        desired = (status or "").strip().upper()
        if desired not in ("ACTIVE", "PAUSED"):
            raise MetaGraphError("status must be ACTIVE or PAUSED")
        return self._graph_post(oid, {"status": desired})

    def debug_token(self, *, input_token: Optional[str] = None) -> dict[str, Any]:
        """Inspect a token (defaults to the active connector token)."""
        self.validate_config()
        app_token = f"{self.app_id}|{self.app_secret}"
        data = self._graph_get(
            "debug_token",
            {
                "input_token": input_token or self.access_token(),
                "access_token": app_token,
            },
        )
        return data.get("data") or {}

    def validate_user_token(self) -> dict[str, Any]:
        """Fail fast when GROUND_TRUTH_ACCESS_TOKEN does not match the configured app."""
        if not self.user_access_token:
            return {}

        try:
            debug = self.debug_token(input_token=self.user_access_token)
        except MetaGraphError as exc:
            msg = str(exc)
            if "did not match the Viewing App" in msg or "App_id in the input_token" in msg:
                raise MetaGraphError(
                    f"GROUND_TRUTH_ACCESS_TOKEN was not generated for app {self.app_id}. "
                    "Open Graph API Explorer, select this app, add ads_read, "
                    "and generate a new token."
                ) from exc
            raise

        if not debug.get("is_valid"):
            raise MetaGraphError(
                "GROUND_TRUTH_ACCESS_TOKEN is invalid or expired. "
                "Generate a new token in Graph API Explorer."
            )

        token_app = str(debug.get("app_id") or "").strip()
        if token_app and self.app_id and token_app != self.app_id:
            raise MetaGraphError(
                f"GROUND_TRUTH_ACCESS_TOKEN belongs to app {token_app}, "
                f"but GROUND_TRUTH_APP_ID is {self.app_id}. "
                "Select the configured app in Graph API Explorer and generate a new token."
            )

        scopes = debug.get("scopes") or []
        if "ads_read" not in scopes and "ads_management" not in scopes:
            raise MetaGraphError(
                "GROUND_TRUTH_ACCESS_TOKEN is missing ads_read / ads_management. "
                "In Graph API Explorer, add those permissions before generating the token."
            )
        return debug


_client: Optional[MetaGraphClient] = None


def graph_client() -> MetaGraphClient:
    global _client
    if _client is None:
        _client = MetaGraphClient()
    return _client
