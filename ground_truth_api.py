"""
GroundTruth Reporting API client for the Ground Truth MCP connector.

Docs: https://api-docs.groundtruth.com/ (Reporting "demand" API at
https://reporting.groundtruth.com). Every request needs BOTH headers:

  X-GT-USER-ID  → GROUND_TRUTH_USER_ID
  X-GT-API-KEY  → GROUND_TRUTH_API_KEY

Credentials are issued by GroundTruth support on request (not self-serve).
The API only answers over HTTP/1.1 (httpx default).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import date, timedelta
from typing import Any, Optional

import httpx

logger = logging.getLogger("ground-truth-api")

DEFAULT_BASE_URL = "https://reporting.groundtruth.com"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
ORG_CACHE_TTL_SECONDS = 600


class GroundTruthError(Exception):
    """GroundTruth Reporting API error."""


def _env(*keys: str) -> str:
    for key in keys:
        val = os.environ.get(key, "").strip()
        if val:
            return val
    return ""


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def extract_rows(payload: Any) -> list[dict[str, Any]]:
    """Normalize list / {data: [...]} / single-object responses to a row list."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("data", "results", "rows", "records", "items", "result"):
            inner = payload.get(key)
            if isinstance(inner, list):
                return [r for r in inner if isinstance(r, dict)]
            if isinstance(inner, dict):
                return extract_rows(inner)
        return [payload]
    return []


# Reporting API uses short metric names on breakdown endpoints (imp, clks, vst …).
_METRIC_ALIASES = {
    "impressions": ("impressions", "imp"),
    "clicks": ("clicks", "clks"),
    "spend": ("spend",),
    "ctr": ("ctr", "click_through_rate"),
    "cpm": ("cpm",),
    "visits": ("visits", "vst"),
    "open_hour_visits": ("open_hour_visits", "oh_vst"),
    "projected_visits": ("projected_visits", "prj_vst"),
    "secondary_actions": ("total_sa", "secondary_actions", "sa"),
    "secondary_action_rate": ("sar", "secondary_action_rate"),
    "visit_rate": ("svr", "vr"),
    "reach": ("cumulative_reach", "daily_reach"),
    "video_completes": ("video_end", "vid_100"),
    "conversions": ("total_conversions",),
}


def metric(row: dict[str, Any], name: str) -> float:
    """Read a metric from a row regardless of long/short field naming."""
    for key in _METRIC_ALIASES.get(name, (name,)):
        if row.get(key) is not None:
            return _to_float(row.get(key))
    return 0.0


class GroundTruthClient:
    def __init__(self) -> None:
        self.user_id = _env("GROUND_TRUTH_USER_ID")
        self.api_key = _env("GROUND_TRUTH_API_KEY")
        self.org_id = _env("GROUND_TRUTH_ORG_ID")
        self.tenant_id = _env("GROUND_TRUTH_TENANT_ID")
        self.default_account = _env("GROUND_TRUTH_ACCOUNT_ID")
        self.base_url = (_env("GROUND_TRUTH_API_BASE") or DEFAULT_BASE_URL).rstrip("/")
        self.date_format = _env("GROUND_TRUTH_DATE_FORMAT") or "%Y-%m-%d"
        self._http = httpx.Client(
            timeout=httpx.Timeout(120.0, connect=15.0),
            http2=False,
            headers={"Accept": "application/json"},
        )
        self._org_cache: dict[tuple[str, str, str], tuple[float, list[dict[str, Any]]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Config / transport
    # ------------------------------------------------------------------

    def validate_config(self) -> None:
        missing = [
            key
            for key, val in (
                ("GROUND_TRUTH_USER_ID", self.user_id),
                ("GROUND_TRUTH_API_KEY", self.api_key),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                "Missing GroundTruth credentials:\n"
                + "\n".join(f"  - {k}" for k in missing)
                + "\nRequest them from GroundTruth support (Reporting API access)."
            )

    def fmt_date(self, d: date) -> str:
        return d.strftime(self.date_format)

    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        """GET a Reporting API path with auth headers, retrying transient failures."""
        self.validate_config()
        clean = "/" + path.lstrip("/")
        if not clean.startswith("/demand/"):
            raise GroundTruthError("Only /demand/... Reporting API paths are allowed.")
        query = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
        headers = {"X-GT-USER-ID": self.user_id, "X-GT-API-KEY": self.api_key}
        url = f"{self.base_url}{clean}"

        last_exc: Optional[Exception] = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                logger.info("GT GET %s %s", clean, query)
                res = self._http.get(url, params=query, headers=headers)
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning("GT request error (attempt %d): %s", attempt, exc)
                time.sleep(min(2**attempt, 8))
                continue

            if res.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                logger.warning("GT HTTP %s (attempt %d), retrying", res.status_code, attempt)
                time.sleep(min(2**attempt, 8))
                continue

            try:
                data = res.json() if res.content else None
            except ValueError:
                data = res.text

            if res.status_code in (401, 403):
                raise GroundTruthError(
                    f"GroundTruth auth failed (HTTP {res.status_code}). Check "
                    "GROUND_TRUTH_USER_ID / GROUND_TRUTH_API_KEY and that this user "
                    "has access to the requested org/account/campaign."
                )
            if not res.is_success:
                raise GroundTruthError(f"GroundTruth HTTP {res.status_code}: {str(data)[:500]}")
            return data

        raise GroundTruthError(f"GroundTruth request failed after retries: {last_exc}")

    def rows(self, path: str, params: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
        return extract_rows(self.get(path, params))

    def _range(self, start: date, end: date) -> dict[str, str]:
        return {"start_date": self.fmt_date(start), "end_date": self.fmt_date(end)}

    # ------------------------------------------------------------------
    # Org / account discovery
    # ------------------------------------------------------------------

    def resolve_org_id(self, org_id: str = "") -> str:
        raw = (org_id or self.org_id).strip()
        if not raw:
            raise ValueError("organization_id is required (or set GROUND_TRUTH_ORG_ID).")
        return raw

    def org_totals(self, start: date, end: date, org_id: str = "") -> list[dict[str, Any]]:
        """Per-campaign totals for every account in the organization (cached briefly)."""
        oid = self.resolve_org_id(org_id)
        key = (oid, start.isoformat(), end.isoformat())
        now = time.time()
        with self._lock:
            hit = self._org_cache.get(key)
            if hit and now - hit[0] < ORG_CACHE_TTL_SECONDS:
                return hit[1]
        rows = self.rows(f"/demand/v1/org/{oid}/totals", self._range(start, end))
        with self._lock:
            self._org_cache[key] = (now, rows)
        return rows

    def account_count(self, tenant_id: str = "") -> list[dict[str, Any]]:
        tid = (tenant_id or self.tenant_id).strip()
        if not tid:
            raise ValueError("tenant_id is required (or set GROUND_TRUTH_TENANT_ID).")
        return self.rows(f"/demand/v1/org/account_count/{tid}")

    def list_accounts(self, start: date, end: date, org_id: str = "") -> list[dict[str, Any]]:
        """Roll org totals up to one row per account."""
        accounts: dict[str, dict[str, Any]] = {}
        for r in self.org_totals(start, end, org_id):
            aid = str(r.get("account_id") or "").split(".")[0]
            if not aid:
                continue
            acct = accounts.setdefault(
                aid,
                {
                    "account_id": aid,
                    "account_name": r.get("account_name") or "",
                    "currency": r.get("currency") or "",
                    "account_status": r.get("account_status") or "",
                    "timezone": r.get("account_timezone") or "",
                    "campaigns": 0,
                    "impressions": 0.0,
                    "clicks": 0.0,
                    "spend": 0.0,
                    "visits": 0.0,
                },
            )
            acct["campaigns"] += 1
            for m in ("impressions", "clicks", "spend", "visits"):
                acct[m] += metric(r, m)
        return sorted(accounts.values(), key=lambda a: a["spend"], reverse=True)

    def list_campaigns(
        self,
        start: date,
        end: date,
        *,
        account_id: str = "",
        search: str = "",
        org_id: str = "",
    ) -> list[dict[str, Any]]:
        aid = (account_id or "").strip()
        q = (search or "").strip().lower()
        out = []
        for r in self.org_totals(start, end, org_id):
            if aid and str(r.get("account_id") or "").split(".")[0] != aid:
                continue
            name = str(r.get("campaign_name") or "")
            cid = str(r.get("campaign_id") or "").split(".")[0]
            if q and q not in name.lower() and q != cid:
                continue
            out.append({**r, "campaign_id": cid})
        return out

    def resolve_campaign_id(self, campaign: str, *, account_id: str = "") -> str:
        """Accept a numeric campaign id or a (partial) campaign name."""
        q = (campaign or "").strip()
        if not q:
            raise ValueError("campaign (id or name) is required.")
        if q.isdigit():
            return q
        if not (self.org_id or "").strip():
            raise ValueError(
                "Pass a numeric campaign id, or set GROUND_TRUTH_ORG_ID so names can be looked up."
            )
        end = date.today()
        start = end - timedelta(days=365)
        rows = self.list_campaigns(start, end, account_id=account_id, search=q)
        if not rows:
            raise ValueError(f"No campaign matching '{q}' in the last 12 months.")
        exact = [r for r in rows if str(r.get("campaign_name") or "").lower() == q.lower()]
        return str((exact or rows)[0]["campaign_id"])

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def account_totals(self, account_id: str, start: date, end: date) -> list[dict[str, Any]]:
        aid = (account_id or self.default_account).strip()
        if not aid:
            raise ValueError("account_id is required (or set GROUND_TRUTH_ACCOUNT_ID).")
        return self.rows(
            f"/demand/v1/account/{aid}/totals",
            {**self._range(start, end), "all_campaigns": 1},
        )

    def campaign_totals(
        self, campaign_id: str, start: date, end: date, *, by: str = ""
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = self._range(start, end)
        if by == "adgroup":
            params["all_adgroups"] = 1
        elif by == "creative":
            params["all_creatives"] = 1
        return self.rows(f"/demand/v1/campaign/{campaign_id}/totals", params)

    def campaign_daily(
        self, campaign_id: str, start: date, end: date, *, by_adgroup: bool = False
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = self._range(start, end)
        if by_adgroup:
            params["all_adgroups"] = 1
        return self.rows(f"/demand/v1/campaign/{campaign_id}/daily", params)

    def adgroup_daily(self, adgroup_id: str, start: date, end: date) -> list[dict[str, Any]]:
        return self.rows(f"/demand/v1/adgroup/{adgroup_id}/daily", self._range(start, end))

    def creatives_daily(self, campaign_id: str, start: date, end: date) -> list[dict[str, Any]]:
        return self.rows(f"/demand/v1/creatives/{campaign_id}/daily", self._range(start, end))

    def campaign_locations(
        self,
        campaign_id: str,
        start: date,
        end: date,
        *,
        location_type: str = "state",
        sort_metric: str = "impressions",
    ) -> list[dict[str, Any]]:
        return self.rows(
            f"/demand/v1/campaign/locations/{campaign_id}",
            {
                **self._range(start, end),
                "location_type": location_type,
                "metric": sort_metric,
                "key": 0,
            },
        )

    def campaign_demographic(
        self, campaign_id: str, start: date, end: date, *, by: str = "age_gender"
    ) -> list[dict[str, Any]]:
        key = {"age": 1, "gender": 2, "age_gender": 3}[by]
        return self.rows(
            f"/demand/v1/campaign/demographic/{campaign_id}",
            {**self._range(start, end), "key": key},
        )

    def campaign_device_type(self, campaign_id: str, start: date, end: date) -> list[dict[str, Any]]:
        return self.rows(f"/demand/v1/campaign/device_type/{campaign_id}", self._range(start, end))

    def campaign_time_of_day(
        self, campaign_id: str, start: date, end: date, *, level: str = "campaign"
    ) -> list[dict[str, Any]]:
        seg = {"campaign": "campaign", "adgroup": "adgroups", "creative": "creatives"}[level]
        return self.rows(
            f"/demand/v1/timeseries/tod/export/{seg}/{campaign_id}", self._range(start, end)
        )

    def campaign_dimension(
        self,
        dimension: str,
        campaign_id: str,
        start: date,
        end: date,
        *,
        level: str = "campaign",
    ) -> list[dict[str, Any]]:
        """
        v2 breakdown endpoints: behavioral_audience, category, brand_affinity,
        publisher, network, product (POI), audio/streaming_genre, audio/podcast_topic,
        audio/podcast_series, audio/publisher.
        """
        seg = {"campaign": "campaign", "adgroup": "adgroups", "creative": "creatives"}[level]
        version = "v3" if dimension == "product" else "v2"
        return self.rows(
            f"/demand/{version}/{seg}/{dimension}/{campaign_id}", self._range(start, end)
        )

    def conversion_tracking(self, account_id: str, start: date, end: date) -> list[dict[str, Any]]:
        aid = (account_id or self.default_account).strip()
        if not aid:
            raise ValueError("account_id is required (or set GROUND_TRUTH_ACCOUNT_ID).")
        return self.rows(
            f"/demand/v1/account/conversion_tracking/{aid}", self._range(start, end)
        )


_client: Optional[GroundTruthClient] = None


def gt_client() -> GroundTruthClient:
    global _client
    if _client is None:
        _client = GroundTruthClient()
    return _client
