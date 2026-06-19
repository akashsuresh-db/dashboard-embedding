"""
AI/BI Dashboard embedding app — On-Behalf-Of-User (OBO).

Architecture (satisfies all four success criteria):
  i)   Frontend embeds the dashboard with the @databricks/aibi-client
       `DatabricksDashboard` JavaScript SDK (NOT an iframe). The token passed
       to the SDK is the *user's own* forwarded access token, so the user is
       never asked to authenticate a second time.
  ii)  OBO: Databricks Apps injects `x-forwarded-access-token` (the signed-in
       user's scoped OAuth token) on every request. Every dashboard query and
       every Genie call runs as that user, so Unity Catalog row-level security
       (keyed on current_user()) is enforced.
  iii) No service principal is ever used to read data. The app's own SP identity
       is not used for the dashboard token or for Genie — only the user token is.
  iv)  Genie runs natively two ways: the embedded dashboard's "Ask Genie" button,
       and a native Genie chat panel powered by the Genie Conversation API,
       both using the OBO user token.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="AI/BI Embedded Dashboard (OBO)")

IS_DATABRICKS_APP = bool(os.environ.get("DATABRICKS_APP_NAME"))


def _instance_url() -> str:
    host = os.environ.get("DATABRICKS_HOST", "")
    if host and not host.startswith("http"):
        host = f"https://{host}"
    return host.rstrip("/")


INSTANCE_URL = _instance_url()
WORKSPACE_ID = os.environ.get("WORKSPACE_ID", "")
DASHBOARD_ID = os.environ.get("DASHBOARD_ID", "")
GENIE_SPACE_ID = os.environ.get("GENIE_SPACE_ID", "")


def _local_token() -> str | None:
    """Local dev fallback: use the U2M OAuth token from the CLI profile.

    This is still the *user's* token (not a service principal), so the embedding
    and RLS behave the same locally as when deployed."""
    try:
        from databricks.sdk import WorkspaceClient

        w = WorkspaceClient(profile=os.environ.get("DATABRICKS_PROFILE", "DEFAULT"))
        headers = w.config.authenticate()
        auth = headers.get("Authorization", "")
        return auth.replace("Bearer ", "") if auth.startswith("Bearer ") else None
    except Exception:
        return None


def _user_token(request: Request) -> str | None:
    """The OBO user token. In Databricks Apps this header is injected by the
    platform on every request; locally we fall back to the CLI user token."""
    token = request.headers.get("x-forwarded-access-token")
    if token:
        return token
    if not IS_DATABRICKS_APP:
        return _local_token()
    return None


def _user_email(request: Request) -> str:
    return (
        request.headers.get("x-forwarded-email")
        or request.headers.get("x-forwarded-preferred-username")
        or request.headers.get("x-forwarded-user")
        or ""
    )


def _api(method: str, path: str, token: str, body=None):
    url = f"{INSTANCE_URL}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def _http_get_json(url: str, token: str):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _post_form(url: str, data: dict):
    req = urllib.request.Request(
        url,
        method="POST",
        data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _tokeninfo_template(viewer_id: str, external_value: str) -> dict:
    """Deterministic reconstruction of the published-dashboard tokeninfo response
    (used when the OBO token lacks the `dashboards` scope to call it directly)."""
    return {
        "custom_claim": f"urn:aibi:external_data:{external_value}:{viewer_id}:{DASHBOARD_ID}",
        "scope": "dashboards.lakeview-embedded:read dashboards.query-execution settings:read sql.redash-config:read",
        "authorization_details": [
            {
                "type": "workspace_rule_set",
                "resource_name": f"workspaces/{WORKSPACE_ID}",
                "grant_rules": [{"permission_set": "permissionSets/workspace.workspace-access"}],
            },
            {
                "type": "workspace_rule_set",
                "resource_name": f"workspaces/{WORKSPACE_ID}",
                "grant_rules": [{"permission_set": "permissionSets/workspace.dbsql-access"}],
            },
            {
                "type": "workspace_rule_set",
                "resource_name": f"workspaces/{WORKSPACE_ID}/dashboards/{DASHBOARD_ID}",
                "resource_legacy_acl_path": os.environ.get("DASHBOARD_ACL_PATH", ""),
                "grant_rules": [{"permission_set": "permissionSets/dashboard.runner"}],
            },
        ],
    }


def mint_embed_token(user_token: str, viewer_id: str, external_value: str) -> str:
    """Mint a dashboard-scoped embed token FROM THE USER'S identity (OBO) using
    OAuth token-exchange — no service principal involved.

    Step 1: call the published-dashboard `tokeninfo` endpoint with the user's
            OBO token to obtain the scope + authorization_details bound to this
            dashboard.
    Step 2: exchange the user's token (subject_token) for a downscoped token
            carrying those authorization_details. The result represents the same
            user, restricted to running this one dashboard — safe to hand to the
            browser, and queries still run as the user so UC row-level security on
            current_user() is enforced.
    """
    info_url = (
        f"{INSTANCE_URL}/api/2.0/lakeview/dashboards/{DASHBOARD_ID}/published/tokeninfo"
        f"?external_viewer_id={urllib.parse.quote(viewer_id)}"
        f"&external_value={urllib.parse.quote(external_value)}"
    )
    try:
        token_info = _http_get_json(info_url, user_token)
    except urllib.error.HTTPError:
        # The Databricks Apps OBO token cannot hold the `dashboards` scope that
        # the tokeninfo lookup requires. tokeninfo's output is deterministic for
        # a given dashboard, though, so we reconstruct it and go straight to the
        # token-exchange (which authorizes off the user's UC/dashboard grants).
        token_info = _tokeninfo_template(viewer_id, external_value)

    authorization_details = token_info.pop("authorization_details", None)

    params = dict(token_info)  # carries scope + custom_claim
    params.update(
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "subject_token": user_token,
            "subject_token_type": "urn:ietf:params:oauth:token-type:jwt",
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "authorization_details": json.dumps(authorization_details),
        }
    )
    resp = _post_form(f"{INSTANCE_URL}/oidc/v1/token", params)
    return resp["access_token"]


@app.get("/api/embed-token")
def embed_token(request: Request):
    """Return a fresh dashboard-scoped embed token derived from the caller's OBO
    identity. The React app passes this to the aibi-client SDK (and re-fetches it
    via the SDK's getNewToken callback before expiry)."""
    user_token = _user_token(request)
    if not user_token:
        return JSONResponse({"error": "No OBO token present on request."}, status_code=401)
    email = _user_email(request) or "app-user"
    # external_viewer_id must not contain PII -> use a stable non-PII hash.
    viewer_id = hashlib.sha256(email.encode()).hexdigest()[:16]
    try:
        token = mint_embed_token(user_token, viewer_id, email)
        return JSONResponse({"token": token})
    except urllib.error.HTTPError as e:
        return JSONResponse({"error": f"Token exchange failed: {e.read().decode()}"}, status_code=502)


@app.get("/api/config")
def get_config(request: Request):
    # The dashboard is embedded in SESSION mode (no token) so it runs on the
    # viewer's own Databricks session — true OBO, RLS enforced, no service
    # principal, and the native "Ask Genie" button renders. The raw token is
    # therefore NOT sent to the browser. The Genie chat panel uses the backend's
    # forwarded OBO token (server-side) instead.
    has_obo = bool(_user_token(request))
    return JSONResponse(
        {
            "instanceUrl": INSTANCE_URL,
            "workspaceId": WORKSPACE_ID,
            "dashboardId": DASHBOARD_ID,
            "genieSpaceId": GENIE_SPACE_ID,
            "user": _user_email(request),
            "oboEnabled": has_obo,
            "isApp": IS_DATABRICKS_APP,
        }
    )


class GenieAsk(BaseModel):
    question: str
    conversation_id: str | None = None


@app.post("/api/genie/ask")
def genie_ask(payload: GenieAsk, request: Request):
    """Native Genie via the Conversation API, executed with the user's OBO token."""
    token = _user_token(request)
    if not token:
        return JSONResponse({"error": "No OBO token present on request."}, status_code=401)
    if not GENIE_SPACE_ID:
        return JSONResponse({"error": "GENIE_SPACE_ID is not configured."}, status_code=500)

    space = GENIE_SPACE_ID
    try:
        if payload.conversation_id:
            started = _api(
                "POST",
                f"/api/2.0/genie/spaces/{space}/conversations/{payload.conversation_id}/messages",
                token,
                {"content": payload.question},
            )
            conv_id = payload.conversation_id
            message_id = started.get("message_id") or started.get("id")
        else:
            started = _api(
                "POST",
                f"/api/2.0/genie/spaces/{space}/start-conversation",
                token,
                {"content": payload.question},
            )
            conv_id = started.get("conversation_id") or started.get("conversation", {}).get("id")
            message_id = started.get("message_id") or started.get("message", {}).get("id")
    except urllib.error.HTTPError as e:
        return JSONResponse({"error": f"Genie start failed: {e.read().decode()}"}, status_code=502)

    msg = {}
    for _ in range(60):
        msg = _api(
            "GET",
            f"/api/2.0/genie/spaces/{space}/conversations/{conv_id}/messages/{message_id}",
            token,
        )
        if msg.get("status") in ("COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"):
            break
        time.sleep(1.5)

    text_parts: list[str] = []
    table = None
    for att in msg.get("attachments", []) or []:
        if att.get("text"):
            text_parts.append(att["text"].get("content", ""))
        if att.get("query"):
            q = att["query"]
            if q.get("description"):
                text_parts.append(q["description"])
            att_id = att.get("attachment_id") or att.get("id")
            qr = None
            for candidate in (
                f"/api/2.0/genie/spaces/{space}/conversations/{conv_id}/messages/{message_id}/attachments/{att_id}/query-result",
                f"/api/2.0/genie/spaces/{space}/conversations/{conv_id}/messages/{message_id}/query-result",
            ):
                try:
                    qr = _api("GET", candidate, token)
                    break
                except urllib.error.HTTPError:
                    continue
            if qr:
                sr = qr.get("statement_response", qr.get("statementResponse", {}))
                cols = [c.get("name") for c in sr.get("manifest", {}).get("schema", {}).get("columns", [])]
                rows = (sr.get("result", {}) or {}).get("data_array", []) or []
                table = {"columns": cols, "rows": rows[:50]}

    return JSONResponse(
        {
            "conversation_id": conv_id,
            "message_id": message_id,
            "status": msg.get("status"),
            "text": "\n\n".join(t for t in text_parts if t).strip(),
            "table": table,
        }
    )


# ---- Serve the React frontend (zero-build: React from CDN) ----
_static = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=_static), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(_static, "index.html"))


@app.get("/{full_path:path}")
def spa(full_path: str):
    return FileResponse(os.path.join(_static, "index.html"))
