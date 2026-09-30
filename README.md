# Ground Truth MCP Connector

Claude connector for **GroundTruth** ad reporting. Ask about accounts, campaigns, ad groups, creatives, store visits, locations, audiences, and performance in plain language. Data comes live from the [GroundTruth Reporting API](https://api-docs.groundtruth.com/) (`reporting.groundtruth.com`).

Same Cloud Run + Claude OAuth + MCP pattern as `meta-mcp-connector`.

## What you need

| Item | Where it comes from |
|------|--------------------|
| `GROUND_TRUTH_USER_ID` | GroundTruth support (Reporting API access, sent as `X-GT-USER-ID`) |
| `GROUND_TRUTH_API_KEY` | GroundTruth support (sent as `X-GT-API-KEY`) |
| `GROUND_TRUTH_ORG_ID` | Your GroundTruth organization ID (Ads Manager / your account rep) |
| `GROUND_TRUTH_ACCOUNT_ID` | Optional default account ID |
| GCP project ID with billing | For Cloud Run hosting |
| `gcloud` CLI, logged in | `gcloud auth login` |

API credentials are not self-serve: ask GroundTruth support for Reporting API credentials.

## Project structure

```
ground_truth_mcp_server.py   # MCP server, tools, OAuth wiring, /health
ground_truth_api.py          # GroundTruth Reporting API client
ground_truth_oauth.py        # Claude-compatible OAuth (stateless, signed)
deploy-cloudrun.ps1          # Deploy to Google Cloud Run
Dockerfile, requirements.txt, .env.example
```

## Local setup

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # fill in GROUND_TRUTH_* values
python ground_truth_mcp_server.py --http
```

MCP endpoint: `http://127.0.0.1:8001/mcp` (already in `.cursor/mcp.json`).

## MCP tools

| Tool | Purpose |
|------|---------|
| `help_ground_truth` | What you can ask |
| `get_integration_status` | Credential + connectivity check |
| `list_accounts` | Accounts in the org with spend |
| `list_campaigns` | Campaigns (filter by account / name) with totals |
| `get_org_summary` | Org-wide KPIs |
| `get_account_summary` | Account KPIs + per-campaign table |
| `get_campaign_summary` | Campaign KPIs incl. visits, reach, secondary actions |
| `get_daily_trend` | Day-by-day campaign metrics |
| `get_adgroup_performance` | Ad groups in a campaign |
| `get_creative_performance` | Top creatives in a campaign |
| `get_location_breakdown` | By state / DMA / zipcode / county |
| `get_demographics_breakdown` | By age / gender / age × gender |
| `get_device_breakdown` | By device / publisher type |
| `get_hourly_performance` | Time of day |
| `get_dimension_breakdown` | Behavioral audience, category, brand affinity, publisher, network, audio |
| `get_poi_performance` | Store visits by POI / location |
| `get_conversion_tracking` | Conversion tracking per ad group |
| `raw_report` | Any other `/demand/...` Reporting API endpoint |

Campaigns can be passed as a numeric ID or a name (name lookup needs `GROUND_TRUTH_ORG_ID`).

## Deploy to Cloud Run

```powershell
.\deploy-cloudrun.ps1 -ProjectId "YOUR_GCP_PROJECT_ID"
```

The script stores `GROUND_TRUTH_API_KEY` in Secret Manager, creates a permanent OAuth signing secret (once), deploys, and prints the connector URL.

Then in Claude: **Settings → Connectors → Add custom connector** → URL `https://YOUR-SERVICE-URL/mcp` → leave OAuth Client ID empty → Connect.

## Staying connected (no reconnects)

Claude asks you to reconnect when something it holds (MCP session, OAuth client, or token) stops being recognized by the server. This connector keeps nothing Claude depends on in server memory:

- **Stateless MCP transport** (`stateless_http=True`, `json_response=True`): no MCP session IDs to lose on restart, and no long-lived SSE streams for Cloud Run to cut off.
- **Signed OAuth client IDs**: Claude's Dynamic Client Registration is encoded in the client ID itself, so token refresh works on any instance, including after a redeploy.
- **Signed access/refresh tokens**: 90-day access tokens, 10-year refresh tokens, verified by signature.
- **Permanent signing secret** (`MCP_OAUTH_JWT_SECRET` in Secret Manager): generated once and reused on every deploy. Rotating it logs Claude out.
- **Cloud Run**: `--min-instances 1`, `--no-cpu-throttling`, `--timeout 3600`, so there are no cold starts and no request timeouts.

After the first successful Connect you should not need to reconnect, even across deploys.

## Verify deployment

| Check | URL |
|-------|-----|
| Health | `https://YOUR-SERVICE-URL/health` (`oauth_enabled: true`, `oauth_sessions_survive_deploys: true`) |
| OAuth discovery | `https://YOUR-SERVICE-URL/.well-known/oauth-authorization-server` |
| Protected resource | `https://YOUR-SERVICE-URL/.well-known/oauth-protected-resource/mcp` |

## Troubleshooting

- *"Couldn't connect"*: the URL must end with `/mcp`.
- *"Couldn't register with sign-in service"*: check `/health` shows `oauth_enabled: true` and `public_url` matches the service URL.
- Tools return `GroundTruth auth failed (HTTP 403)`: the user ID / API key is wrong, or that user lacks access to the requested org/account/campaign.
- Dates look wrong: set `GROUND_TRUTH_DATE_FORMAT` if your API expects a format other than `YYYY-MM-DD`.

## Security

- Never commit `.env` (gitignored).
- `--allow-unauthenticated` makes the URL reachable, but every MCP call needs a valid OAuth token; unauthenticated calls get `401`.
