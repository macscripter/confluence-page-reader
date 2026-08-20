"""
Atlassian reader (read-only REST API) for Confluence pages and Jira issues.

The same three endpoints serve both products. Which product answers is
decided per request, from the identifier shape, the query parameters, the
target URL, or an explicit header (see _product below).

Endpoints:
  GET /health                                   liveness, no credentials needed
  GET /search?query=...&limit=...               free-text search
      ...&cql=...                               raw CQL   (forces Confluence)
      ...&jql=...                               raw JQL   (forces Jira)
  GET /pages/{id}                               page by numeric id, or issue by key
  GET /pages/by-title?space_key=...&title=...   exact title in a space,
                                                or best summary match in a project

  Aliases, identical behaviour, for readable URLs:
  GET /issues/{key}, GET /items/{id}, GET /items/by-title

Credentials are NOT stored in the image. Every request carries them as
HTTP headers:

  X-Atlassian-Url:     https://jira.yourcompany.com
  X-Atlassian-Token:   <PAT (Data Center) or API token (Cloud)>
  X-Atlassian-Email:   <Cloud only; presence of this header selects Cloud auth>
  X-Atlassian-Product: confluence | jira   (optional, overrides detection)

The legacy X-Confluence-* headers are still accepted and pin the product to
Confluence, so existing callers keep working unchanged.

Auth is identical for both products:
  Data Center / Server -> Bearer token.
  Cloud                -> Basic auth with email + API token.

Start command: uvicorn server:app --host 0.0.0.0 --port 8000
"""

import re
import json
import html as _html

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

_ACCEPT = {"Accept": "application/json"}
_TAG_RE = re.compile(r"<[^>]+>")

# PROJ-123, AB1-9: a Jira issue key. Confluence page ids are all digits.
_ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")

# Jira issue fields worth returning; keeps responses small and predictable.
_JIRA_FIELDS = ("summary,description,status,issuetype,project,priority,"
                "assignee,reporter,labels,created,updated,resolution,parent")


class CredentialsError(Exception):
    pass


# --------------------------------------------------------------------------
# Product detection
# --------------------------------------------------------------------------
def _raw_url(request) -> str:
    return (request.headers.get("x-atlassian-url")
            or request.headers.get("x-confluence-url")
            or "").strip().rstrip("/")


def _product(request, identifier: str = "") -> str:
    """Decide whether this request targets Confluence or Jira.

    Signals, strongest first:
      1. Explicit X-Atlassian-Product header.
      2. Legacy X-Confluence-* headers with no X-Atlassian-Url -> Confluence.
      3. Identifier shape: PROJ-123 is a Jira key, 12345 a Confluence page id.
      4. Query parameters: jql -> Jira, cql -> Confluence.
      5. Target URL: /wiki or a confluence host -> Confluence, jira host -> Jira.
      6. Default: Confluence (backwards compatible).
    """
    explicit = request.headers.get("x-atlassian-product", "").strip().lower()
    if explicit in ("confluence", "jira"):
        return explicit

    if request.headers.get("x-confluence-url") and not request.headers.get("x-atlassian-url"):
        return "confluence"

    if identifier:
        if _ISSUE_KEY_RE.match(identifier):
            return "jira"
        if identifier.isdigit():
            return "confluence"

    if request.query_params.get("jql"):
        return "jira"
    if request.query_params.get("cql"):
        return "confluence"

    url = _raw_url(request).lower()
    host = url.split("://")[-1].split("/")[0]
    if "/wiki" in url or "confluence" in host:
        return "confluence"
    if "jira" in host:
        return "jira"

    return "confluence"


# --------------------------------------------------------------------------
# Per-request configuration (from headers)
# --------------------------------------------------------------------------
def _conf(request, identifier: str = "") -> tuple[httpx.Client, str, str, str]:
    """Build a client for the product this request targets.

    Returns (client, api_base, site_root, product). The caller must close
    the client.
    """
    url = _raw_url(request)
    token = (request.headers.get("x-atlassian-token")
             or request.headers.get("x-confluence-token") or "").strip()
    email = (request.headers.get("x-atlassian-email")
             or request.headers.get("x-confluence-email") or "").strip()

    if not url or not token:
        raise CredentialsError(
            "Missing credentials. Send X-Atlassian-Url and X-Atlassian-Token "
            "headers (plus X-Atlassian-Email for Cloud)."
        )

    product = _product(request, identifier)
    is_cloud = bool(email)
    site_root = url[:-5] if url.endswith("/wiki") else url

    if product == "jira":
        api_base = f"{site_root}/rest/api/{'3' if is_cloud else '2'}"
    else:
        api_base = f"{site_root}/wiki/rest/api" if is_cloud else f"{site_root}/rest/api"

    if is_cloud:  # Basic auth with email + API token
        client = httpx.Client(base_url=api_base, auth=(email, token),
                              headers=_ACCEPT, timeout=30.0)
    else:  # Data Center / Server: Bearer PAT
        client = httpx.Client(base_url=api_base,
                              headers={**_ACCEPT, "Authorization": f"Bearer {token}"},
                              timeout=30.0)
    return client, api_base, site_root, product


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _get(client: httpx.Client, path: str, params: dict) -> dict:
    resp = client.get(path, params=params)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _strip_tags(value: str) -> str:
    """Crude storage-XHTML or wiki markup -> readable text for previews."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", value))).strip()


def _adf_text(node) -> str:
    """Flatten an Atlassian Document Format description (Jira Cloud) to text."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(_adf_text(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return _adf_text(node.get("content", []))
    return ""


def _describe(description) -> tuple[str, str]:
    """Return (preview_text, raw_body) for a Jira description of either format."""
    if description is None:
        return "", ""
    if isinstance(description, dict):  # Cloud: ADF
        return re.sub(r"\s+", " ", _adf_text(description)).strip(), json.dumps(description)
    return _strip_tags(str(description)), str(description)  # Data Center: wiki markup


def _named(field):
    if isinstance(field, dict):
        return field.get("displayName") or field.get("name") or field.get("key")
    return field


# --------------------------------------------------------------------------
# Response mapping (one shape, both products)
# --------------------------------------------------------------------------
def _page_payload(site_root: str, p: dict) -> dict:
    storage = p.get("body", {}).get("storage", {}).get("value", "")
    return {
        "product": "confluence",
        "id": p.get("id"),
        "title": p.get("title"),
        "space": p.get("space", {}).get("key"),
        "version": p.get("version", {}).get("number"),
        "text_preview": _strip_tags(storage)[:4000],
        "storage_body": storage,
        "url": f"{site_root}/pages/viewpage.action?pageId={p.get('id')}",
    }


def _issue_payload(site_root: str, issue: dict) -> dict:
    """Same keys as a page, so callers keep one code path, plus a 'fields' block."""
    f = issue.get("fields", {}) or {}
    preview, raw = _describe(f.get("description"))
    return {
        "product": "jira",
        "id": issue.get("key"),
        "title": f.get("summary"),
        "space": (f.get("project") or {}).get("key"),
        "version": None,  # issues have no version number; see fields.updated
        "text_preview": preview[:4000],
        "storage_body": raw,
        "url": f"{site_root}/browse/{issue.get('key')}",
        "fields": {
            "status": _named(f.get("status")),
            "issue_type": _named(f.get("issuetype")),
            "priority": _named(f.get("priority")),
            "assignee": _named(f.get("assignee")),
            "reporter": _named(f.get("reporter")),
            "resolution": _named(f.get("resolution")),
            "labels": f.get("labels") or [],
            "parent": (f.get("parent") or {}).get("key"),
            "created": f.get("created"),
            "updated": f.get("updated"),
        },
    }


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, CredentialsError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, httpx.HTTPStatusError):
        body = (exc.response.text or "")[:600]
        return JSONResponse({"error": f"Atlassian {exc.response.status_code}", "detail": body},
                            status_code=exc.response.status_code)
    if isinstance(exc, httpx.HTTPError):
        return JSONResponse({"error": f"Network error reaching Atlassian: {exc}"}, status_code=502)
    return JSONResponse({"error": str(exc)}, status_code=500)


# --------------------------------------------------------------------------
# Jira search (Cloud moved this route; try the new one, fall back)
# --------------------------------------------------------------------------
def _jira_search(client: httpx.Client, jql: str, limit: int) -> list:
    params = {"jql": jql, "maxResults": limit, "fields": _JIRA_FIELDS}
    try:
        return _get(client, "/search/jql", params).get("issues", [])
    except httpx.HTTPStatusError as e:
        if e.response.status_code not in (404, 405, 410):
            raise
    return _get(client, "/search", params).get("issues", [])


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
async def health(request):
    return JSONResponse({"status": "ok", "service": "atlassian-reader",
                         "products": ["confluence", "jira"]})


async def search(request):
    """Free-text, raw CQL or raw JQL search. Returns a list of summaries."""
    try:
        client, _, site_root, product = _conf(request)
    except CredentialsError as e:
        return _error(e)
    try:
        query = request.query_params.get("query", "")
        limit = int(request.query_params.get("limit", "25"))

        if product == "jira":
            jql = request.query_params.get("jql", "")
            if not jql:
                if not query:
                    return JSONResponse({"error": "Provide 'query' or 'jql'."}, status_code=400)
                safe = query.replace('"', '\\"')
                jql = f'text ~ "{safe}" ORDER BY updated DESC'
            issues = _jira_search(client, jql, limit)
            return JSONResponse([{
                "product": "jira",
                "id": i.get("key"),
                "title": (i.get("fields") or {}).get("summary"),
                "space": ((i.get("fields") or {}).get("project") or {}).get("key"),
                "status": _named((i.get("fields") or {}).get("status")),
                "updated": (i.get("fields") or {}).get("updated"),
                "url": f"{site_root}/browse/{i.get('key')}",
            } for i in issues])

        cql = request.query_params.get("cql", "")
        if not cql:
            if not query:
                return JSONResponse({"error": "Provide 'query' or 'cql'."}, status_code=400)
            safe = query.replace('"', '\\"')
            cql = f'type=page AND text ~ "{safe}"'
        data = _get(client, "/content/search",
                    {"cql": cql, "limit": limit, "expand": "space,version"})
        return JSONResponse([{
            "product": "confluence",
            "id": r.get("id"),
            "title": r.get("title"),
            "space": r.get("space", {}).get("key"),
            "version": r.get("version", {}).get("number"),
            "url": f"{site_root}/pages/viewpage.action?pageId={r.get('id')}",
        } for r in data.get("results", [])])
    except Exception as e:
        return _error(e)
    finally:
        client.close()


async def get_item(request):
    """Read a Confluence page by numeric id, or a Jira issue by key."""
    identifier = request.path_params["item_id"]
    try:
        client, _, site_root, product = _conf(request, identifier)
    except CredentialsError as e:
        return _error(e)
    try:
        if product == "jira":
            issue = _get(client, f"/issue/{identifier}", {"fields": _JIRA_FIELDS})
            return JSONResponse(_issue_payload(site_root, issue))
        p = _get(client, f"/content/{identifier}", {"expand": "body.storage,version,space"})
        return JSONResponse(_page_payload(site_root, p))
    except Exception as e:
        return _error(e)
    finally:
        client.close()


async def get_item_by_title(request):
    """Confluence: exact page title in a space. Jira: best summary match in a project."""
    try:
        client, _, site_root, product = _conf(request)
    except CredentialsError as e:
        return _error(e)
    try:
        space_key = request.query_params.get("space_key", "")
        title = request.query_params.get("title", "")
        if not space_key or not title:
            return JSONResponse({"error": "Provide 'space_key' and 'title'."}, status_code=400)

        if product == "jira":
            safe = title.replace('"', '\\"')
            jql = f'project = "{space_key}" AND summary ~ "{safe}" ORDER BY updated DESC'
            issues = _jira_search(client, jql, 1)
            if not issues:
                return JSONResponse(
                    {"found": False,
                     "message": f"No issue with summary matching '{title}' in project '{space_key}'."},
                    status_code=404)
            # Jira matches summaries by word, not exactly: this is the best match.
            return JSONResponse(_issue_payload(site_root, issues[0]))

        data = _get(client, "/content", {
            "spaceKey": space_key, "title": title,
            "expand": "body.storage,version,space", "limit": 1,
        })
        results = data.get("results", [])
        if not results:
            return JSONResponse(
                {"found": False, "message": f"No page titled '{title}' in space '{space_key}'."},
                status_code=404)
        return JSONResponse(_page_payload(site_root, results[0]))
    except Exception as e:
        return _error(e)
    finally:
        client.close()


# by-title routes must be registered before the {item_id} catch-alls
app = Starlette(routes=[
    Route("/health", health),
    Route("/search", search),
    Route("/pages/by-title", get_item_by_title),
    Route("/pages/{item_id}", get_item),
    # Aliases: same handlers, readable URLs per product.
    Route("/items/by-title", get_item_by_title),
    Route("/items/{item_id}", get_item),
    Route("/issues/{item_id}", get_item),
])
