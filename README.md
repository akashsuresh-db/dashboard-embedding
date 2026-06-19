# AI/BI Dashboard Embedding with OBO — Solution

A React app (Databricks App) that embeds an AI/BI dashboard using the
`@databricks/aibi-client` **JavaScript SDK** (not an iframe), authenticating
every dashboard query and Genie call as the **signed-in user** (On-Behalf-Of).

## Live resources

Fill in the table below with your own workspace and resource identifiers after
deployment. Do **not** commit real IDs or URLs to a public repository.

| Resource | Identifier |
|---|---|
| App URL | `https://<your-app-name>-<workspace-id>.aws.databricksapps.com` |
| App name | `<your-app-name>` |
| Dashboard | `<YOUR_DASHBOARD_ID>` — *your dashboard title* |
| Genie space | `<YOUR_GENIE_SPACE_ID>` — *your Genie space title* |
| Table (RLS) | `<catalog>.<schema>.<table>` |
| Row-filter UDF | `<catalog>.<schema>.<rls_function>(col) → col = current_user()` |
| SQL warehouse | `<YOUR_WAREHOUSE_ID>` |

## How each success criterion is met

### i) JavaScript embed (not iframe), uses app auth, no second login
The frontend (`static/app.js`) calls `new DatabricksDashboard({ instanceUrl,
workspaceId, dashboardId, token, container })` from the `@databricks/aibi-client`
SDK and renders into a `<div>` — there is no hardcoded `<iframe src=".../embed/...">`.
The `token` is the user's own forwarded access token, so the already-authenticated
app session is reused; the user is not prompted to authenticate again.

### ii) OBO authentication + RLS
The app has **User authorization (OBO)** enabled with scopes `sql`, `genie`,
`dashboards.genie`. Databricks injects the user's scoped token on every request
via the `x-forwarded-access-token` header (`app.py:_user_token`). Because both the
dashboard queries and Genie run as the user, the Unity Catalog **row filter**
keyed on `current_user()` is enforced: the user sees only rows where they are the
assigned representative.

### iii) No service principal used for data
The app's own service principal identity is never used to read data. The dashboard
token and the Genie Conversation API both use the **user OBO token** only. (The
dashboard is published *without* embedded credentials, so it runs as the viewer.)

### iv) Genie works natively via the app
Two ways, both over OBO:
1. The embedded dashboard's built-in **Ask Genie** button.
2. A native **Ask Genie** chat panel in the app, powered by the Genie
   Conversation API (`/api/genie/ask` in `app.py`) using the user's OBO token.

> Note: the Microsoft doc originally referenced (external-embed) describes a
> different pattern that **requires a service principal** and **disables Ask
> Genie**. That pattern cannot satisfy criteria (iii) and (iv). This solution uses
> the internal/basic embedding model with the aibi-client JS SDK + Apps OBO, which
> satisfies all four.

## Architecture

```
Browser (user already SSO'd to Databricks)
   |  GET /api/config            -- Apps proxy injects x-forwarded-access-token
   v
FastAPI backend (app.py)         -- returns {instanceUrl, workspaceId, dashboardId, token=USER OBO token}
   |
   v
static/app.js (React via CDN)
   |  new DatabricksDashboard({... token: userToken ...}).initialize()   <- JS SDK, no iframe
   v
AI/BI dashboard renders as the USER -> UC row filter on current_user() -> RLS
   |
   +- Ask Genie panel -> POST /api/genie/ask -> Genie Conversation API (OBO) -> RLS-filtered answers
```

## Files
- `app.py` — FastAPI backend: `/api/config`, `/api/genie/ask`, serves `static/`.
- `static/index.html`, `static/app.js`, `static/index.css` — zero-build React frontend
  (React from CDN; no bundler is used).
- `app.yaml` — app command + env vars (fill in your WORKSPACE_ID, DASHBOARD_ID, GENIE_SPACE_ID).
- `requirements.txt` — fastapi, uvicorn, databricks-sdk, pydantic.

## Redeploy
```bash
# Replace <your-workspace-path>, <your-app-name>, and <your-cli-profile> with your own values.
databricks sync . /Workspace/Users/<your-email>/embed-obo-app \
  --exclude node_modules --exclude __pycache__ --exclude .git -p <your-cli-profile>
databricks apps deploy <your-app-name> \
  --source-code-path /Workspace/Users/<your-email>/embed-obo-app -p <your-cli-profile>
```
After changing OBO scopes, restart: `databricks apps stop ... && databricks apps start ...`.
