# Ground Truth MCP server for Google Cloud Run / Docker
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=http \
    HOST=0.0.0.0 \
    PORT=8080

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY ground_truth_mcp_server.py ground_truth_oauth.py ground_truth_api.py ./

# GroundTruth credentials injected at runtime (Secret Manager on Cloud Run).
# GROUND_TRUTH_USER_ID, GROUND_TRUTH_API_KEY, GROUND_TRUTH_ORG_ID, MCP_OAUTH_JWT_SECRET
# MCP_PUBLIC_URL must be set to the Cloud Run service URL for Claude OAuth.

EXPOSE 8080

CMD ["python", "ground_truth_mcp_server.py"]
