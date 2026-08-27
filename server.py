"""
Atlassian REST API: read Confluence pages, read and write Jira issues.

The same three read endpoints serve both products. Which product answers is
decided per request, from the identifier shape, the query parameters, the
target URL, or an explicit header (see _product below). The Jira-specific
routes (/issues/..., /projects, /issue-links, /issue-link-types) always
target Jira.

Read endpoints (both products):
  GET /health                                   liveness, no credentials needed
  GET /search?query=...&limit=...               free-text search
      ...&cql=...                               raw CQL   (forces Confluence)
      ...&jql=...                               raw JQL   (forces Jira)
      ...&project_key=...                       Jira only: restrict free text to a project
  GET /pages/{id}                               page by numeric id, or issue by key
  GET /pages/by-title?space_key=...&title=...   exact title in a space,
                                                or best summary match in a project

  Aliases, identical behaviour, for readable URLs:
  GET /issues/{key}, GET /items/{id}, GET /items/by-title

Jira read endpoints:
  GET /issues/{key}/comments?limit=...          comments, oldest first
  GET /issues/{key}/transitions                 workflow transitions available now
  GET /projects?limit=...                       projects visible to the token owner
  GET /projects/{key}/issue-types               issue types a project accepts
  GET /issue-link-types                         link types (name, outward, inward)

Jira write endpoints (JSON bodies, see the README):
  POST   /issues                                create an issue or sub-task
  PATCH  /issues/{key}          (PUT accepted)  update only the fields given
  DELETE /issues/{key}?confirm=true             delete (no trash in Jira)
  POST   /issues/{key}/comments                 add a comment
  PUT    /issues/{key}/comments/{id}            edit a comment
  DELETE /issues/{key}/comments/{id}?confirm=true
  POST   /issues/{key}/transitions              move through the workflow
  PUT    /issues/{key}/assignee                 assign, unassign, or project default
  POST   /issue-links                           link two issues

Confluence stays read-only here.

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

Every write is attributed in Jira to the owner of the token sent.

Start command: uvicorn server:app --host 0.0.0.0 --port 8000
"""

import re
import json
import html as _html
from dataclasses import dataclass

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

_ACCEPT = {"Accept": "application/json"}
_TAG_RE = re.compile(r"<[^>]+>")

# PROJ-123, AB1-9: a Jira issue key. Confluence page ids are all digits.
_ISSUE_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")

# Fields returned for an issue. Keeps responses small and predictable; anything
# else (custom fields included) is reachable through extra_fields on writes.
_JIRA_ISSUE_FIELDS = ("summary,description,status,issuetype,project,priority,assignee,"
                      "reporter,labels,components,fixVersions,created,updated,duedate,"
                      "resolution,parent,subtasks,issuelinks")
# Lighter set for search results.
_JIRA_SEARCH_FIELDS = "summary,status,issuetype,project,priority,assignee,updated"

_TRUE = {"1", "true", "yes", "on"}


class CredentialsError(Exception):
    pass


class BadRequest(Exception):
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
      4. Query parameters: jql or project_key -> Jira, cql -> Confluence.
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

    if request.query_params.get("jql") or request.query_params.get("project_key"):
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
@dataclass
class _Ctx:
    client: httpx.Client
    api_base: str
    site_root: str
    product: str
    cloud: bool


def _conf(request, identifier: str = "", force_product: str = "") -> _Ctx:
    """Build a client for the product this request targets. The caller closes the client.

    force_product wins over detection: the Jira-specific routes pass "jira" so a Cloud
    site (one host for both products) never falls back to the Confluence default.
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

    product = force_product or _product(request, identifier)
    is_cloud = bool(email)
    site_root = url[:-5] if url.endswith("/wiki") else url
    if product == "jira" and is_cloud and site_root.endswith("/jira"):
        site_root = site_root[:-5]  # Cloud REST and /browse links live at the site root

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
    return _Ctx(client, api_base, site_root, product, is_cloud)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _req(client: httpx.Client, method: str, path: str, **kwargs):
    resp = client.request(method, path, **kwargs)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _get(client: httpx.Client, path: str, params: dict) -> dict:
    return _req(client, "GET", path, params=params)


def _strip_tags(value: str) -> str:
    """Crude storage-XHTML -> readable text for Confluence previews."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", value))).strip()


def _named(field):
    """Display value of a Jira object field (status, user, priority, ...)."""
    if isinstance(field, dict):
        return field.get("displayName") or field.get("name") or field.get("value") or field.get("key")
    return field


def _jql_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _flag(request, name: str) -> bool:
    return (request.query_params.get(name) or "").strip().lower() in _TRUE


async def _json_body(request) -> dict:
    raw = await request.body()
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise BadRequest("Body must be valid JSON.") from None
    if not isinstance(data, dict):
        raise BadRequest("Body must be a JSON object.")
    return data


def _text(body: dict, key: str) -> str:
    value = body.get(key)
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value).strip()


# --------------------------------------------------------------------------
# Jira text bodies: ADF on Cloud (REST v3), wiki markup on Data Center (v2)
# --------------------------------------------------------------------------
def _adf_text(node) -> str:
    """Flatten an Atlassian Document Format body to plain text, keeping paragraph
    breaks, list bullets and table cells readable."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(_adf_text(n) for n in node)
    if isinstance(node, dict):
        t = node.get("type")
        attrs = node.get("attrs") or {}
        if t == "text":
            return node.get("text", "")
        if t == "hardBreak":
            return "\n"
        if t == "mention":
            return attrs.get("text") or ""
        if t == "emoji":
            return attrs.get("text") or attrs.get("shortName") or ""
        if t == "inlineCard":
            return attrs.get("url") or ""
        inner = _adf_text(node.get("content", []))
        if t == "listItem":
            return "- " + inner.rstrip("\n") + "\n"
        if t in ("bulletList", "orderedList", "table", "tableRow"):
            return inner + "\n"
        if t in ("tableCell", "tableHeader"):
            return inner.strip() + " | "
        if t in ("paragraph", "heading", "codeBlock", "blockquote", "rule"):
            return inner + "\n\n"
        return inner
    return ""


def _plain_lines(text: str) -> str:
    """Tidy plain text: single spaces, no trailing spaces, at most one blank line in a row."""
    text = re.sub(r"[ \t]+", " ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _adf_doc(text: str) -> dict:
    """Plain text -> minimal ADF document. A blank line starts a new paragraph; a single
    newline inside a paragraph becomes a hard break, so line structure survives."""
    paragraphs = []
    for block in re.split(r"\n\s*\n", text.strip()):
        content = []
        for i, line in enumerate(block.split("\n")):
            if i:
                content.append({"type": "hardBreak"})
            if line:
                content.append({"type": "text", "text": line})
        if content:
            paragraphs.append({"type": "paragraph", "content": content})
    if not paragraphs:
        paragraphs.append({"type": "paragraph", "content": []})
    return {"type": "doc", "version": 1, "content": paragraphs}


def _jira_body(text: str, cloud: bool):
    """Body value for a description or comment.

    Cloud wants ADF JSON: plain text is converted, and a JSON string that already is an
    ADF document ({"type": "doc", ...}) is passed through untouched. Data Center takes
    wiki markup as a plain string, used as-is.
    """
    text = text or ""
    if not cloud:
        return text
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(stripped)
            if isinstance(doc, dict) and doc.get("type") == "doc":
                return doc
        except ValueError:
            pass
    return _adf_doc(text)


def _body_text(body) -> tuple[str, str]:
    """Return (plain_text, raw_body) for a description or comment body of either format."""
    if body is None:
        return "", ""
    if isinstance(body, dict):  # Cloud: ADF
        return _plain_lines(_adf_text(body)), json.dumps(body)
    raw = str(body)  # Data Center: wiki markup; drop any embedded HTML tags, keep the lines
    return _plain_lines(_html.unescape(_TAG_RE.sub(" ", raw))), raw


# --------------------------------------------------------------------------
# Jira: users, search, payload shapes
# --------------------------------------------------------------------------
def _find_jira_user(ctx: _Ctx, value: str) -> dict:
    """Resolve an email address or display name to a single Jira user record."""
    param = "query" if ctx.cloud else "username"
    users = _req(ctx.client, "GET", "/user/search", params={param: value, "maxResults": 20})
    if not isinstance(users, list):
        users = []
    wanted = value.strip().lower()
    exact = [u for u in users if wanted in {
        (u.get("emailAddress") or "").lower(),
        (u.get("displayName") or "").lower(),
        (u.get("name") or "").lower(),
    }]
    pick = exact or users
    if len(pick) == 1:
        return pick[0]
    if not pick:
        raise BadRequest(f"No Jira user matches '{value}'.")
    names = ", ".join(f"{u.get('displayName')} <{u.get('emailAddress') or u.get('name') or '?'}>"
                      for u in pick[:10])
    raise BadRequest(f"'{value}' matches several Jira users, be more specific: {names}")


def _jira_user(ctx: _Ctx, value: str) -> dict:
    """User payload for assignee/reporter. Data Center keys users by username, Cloud by
    accountId. An email address or display name is looked up; anything else is used as-is."""
    value = (value or "").strip()
    if "@" in value or " " in value:
        u = _find_jira_user(ctx, value)
        return {"accountId": u.get("accountId")} if ctx.cloud else {"name": u.get("name")}
    return {"accountId": value} if ctx.cloud else {"name": value}


def _jira_search(ctx: _Ctx, jql: str, limit: int) -> list:
    """Run a JQL search. Cloud retired GET /search in favour of /search/jql; Data Center
    only has /search. Try the expected route first, fall back to the other."""
    params = {"jql": jql, "maxResults": max(1, min(limit, 100)), "fields": _JIRA_SEARCH_FIELDS}
    paths = ("/search/jql", "/search") if ctx.cloud else ("/search", "/search/jql")
    last: httpx.HTTPStatusError | None = None
    for path in paths:
        try:
            return _get(ctx.client, path, params).get("issues", [])
        except httpx.HTTPStatusError as e:
            last = e
            if e.response.status_code not in (404, 405, 410):
                raise
    raise last  # both routes missing: surface the last error


def _issue_url(ctx: _Ctx, key: str) -> str:
    return f"{ctx.site_root}/browse/{key}"


def _issue_links(links: list) -> list[dict]:
    out = []
    for ln in links:
        t = ln.get("type") or {}
        if ln.get("outwardIssue"):
            other, relation = ln["outwardIssue"], t.get("outward")
        elif ln.get("inwardIssue"):
            other, relation = ln["inwardIssue"], t.get("inward")
        else:
            continue
        out.append({"id": ln.get("id"), "type": t.get("name"), "relation": relation,
                    "issue": other.get("key"),
                    "status": _named((other.get("fields") or {}).get("status"))})
    return out


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


def _issue_payload(ctx: _Ctx, issue: dict) -> dict:
    """Same keys as a page, so callers keep one code path, plus a 'fields' block."""
    f = issue.get("fields", {}) or {}
    preview, raw = _body_text(f.get("description"))
    key = issue.get("key")
    return {
        "product": "jira",
        "id": key,
        "title": f.get("summary"),
        "space": (f.get("project") or {}).get("key"),
        "version": None,  # issues have no version number; see fields.updated
        "text_preview": preview[:4000],
        "storage_body": raw,
        "url": _issue_url(ctx, key),
        "fields": {
            "issue_id": issue.get("id"),
            "status": _named(f.get("status")),
            "issue_type": _named(f.get("issuetype")),
            "priority": _named(f.get("priority")),
            "assignee": _named(f.get("assignee")),
            "reporter": _named(f.get("reporter")),
            "resolution": _named(f.get("resolution")),
            "labels": f.get("labels") or [],
            "components": [_named(c) for c in f.get("components") or []],
            "fix_versions": [_named(v) for v in f.get("fixVersions") or []],
            "parent": (f.get("parent") or {}).get("key"),
            "subtasks": [s.get("key") for s in f.get("subtasks") or []],
            "links": _issue_links(f.get("issuelinks") or []),
            "created": f.get("created"),
            "updated": f.get("updated"),
            "due_date": f.get("duedate"),
        },
    }


def _issue_summary(ctx: _Ctx, i: dict) -> dict:
    f = i.get("fields") or {}
    return {
        "product": "jira",
        "id": i.get("key"),
        "title": f.get("summary"),
        "space": (f.get("project") or {}).get("key"),
        "status": _named(f.get("status")),
        "issue_type": _named(f.get("issuetype")),
        "priority": _named(f.get("priority")),
        "assignee": _named(f.get("assignee")),
        "updated": f.get("updated"),
        "url": _issue_url(ctx, i.get("key")),
    }


def _fetch_issue(ctx: _Ctx, key: str) -> dict:
    return _issue_payload(ctx, _get(ctx.client, f"/issue/{key}", {"fields": _JIRA_ISSUE_FIELDS}))


def _fetch_transitions(ctx: _Ctx, key: str) -> list[dict]:
    data = _get(ctx.client, f"/issue/{key}/transitions", {})
    return [{"id": t.get("id"), "name": t.get("name"), "to_status": _named(t.get("to"))}
            for t in data.get("transitions", [])]


def _issue_fields(ctx: _Ctx, body: dict) -> dict:
    """Build the Jira `fields` object shared by create and update from the given values only."""
    fields: dict = {}
    if _text(body, "summary"):
        fields["summary"] = _text(body, "summary")
    if _text(body, "description"):
        fields["description"] = _jira_body(body["description"], ctx.cloud)
    if _text(body, "priority"):
        fields["priority"] = {"name": _text(body, "priority")}
    if body.get("labels") is not None:
        labels = body["labels"]
        if not isinstance(labels, list):
            raise BadRequest("labels must be a JSON array of strings.")
        fields["labels"] = [str(lb).strip() for lb in labels if str(lb).strip()]
    if _text(body, "assignee"):
        fields["assignee"] = _jira_user(ctx, _text(body, "assignee"))
    extra = body.get("extra_fields")
    if extra:
        if not isinstance(extra, dict):
            raise BadRequest("extra_fields must be a JSON object.")
        fields.update(extra)
    return fields


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, (CredentialsError, BadRequest)):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, httpx.HTTPStatusError):
        body = (exc.response.text or "")[:600]
        return JSONResponse({"error": f"Atlassian {exc.response.status_code}", "detail": body},
                            status_code=exc.response.status_code)
    if isinstance(exc, httpx.HTTPError):
        return JSONResponse({"error": f"Network error reaching Atlassian: {exc}"}, status_code=502)
    return JSONResponse({"error": str(exc)}, status_code=500)


def _jira_endpoint(fn):
    """Wrap a Jira handler: credentials, JSON body, error mapping, client cleanup.

    fn(ctx, request, body) returns a dict (sent as 200) or a JSONResponse.
    """
    async def handler(request):
        try:
            ctx = _conf(request, force_product="jira")
        except CredentialsError as e:
            return _error(e)
        try:
            body = await _json_body(request) if request.method in ("POST", "PUT", "PATCH") else {}
            result = fn(ctx, request, body)
            return result if isinstance(result, JSONResponse) else JSONResponse(result)
        except Exception as e:
            return _error(e)
        finally:
            ctx.client.close()
    handler.__name__ = fn.__name__
    handler.__doc__ = fn.__doc__
    return handler


# --------------------------------------------------------------------------
# Endpoints: both products
# --------------------------------------------------------------------------
async def health(request):
    return JSONResponse({"status": "ok", "service": "atlassian-reader",
                         "products": ["confluence", "jira"],
                         "capabilities": ["confluence:read", "jira:read", "jira:write"]})


async def search(request):
    """Free-text, raw CQL or raw JQL search. Returns a list of summaries."""
    try:
        ctx = _conf(request)
    except CredentialsError as e:
        return _error(e)
    try:
        query = request.query_params.get("query", "")
        limit = int(request.query_params.get("limit", "25"))

        if ctx.product == "jira":
            jql = request.query_params.get("jql", "")
            if not jql:
                clauses = []
                project_key = request.query_params.get("project_key", "")
                if project_key:
                    clauses.append(f"project = {_jql_quote(project_key)}")
                if query:
                    clauses.append(f"text ~ {_jql_quote(query)}")
                if not clauses:
                    return JSONResponse({"error": "Provide 'query', 'project_key' or 'jql'."},
                                        status_code=400)
                jql = " AND ".join(clauses) + " ORDER BY updated DESC"
            return JSONResponse([_issue_summary(ctx, i) for i in _jira_search(ctx, jql, limit)])

        cql = request.query_params.get("cql", "")
        if not cql:
            if not query:
                return JSONResponse({"error": "Provide 'query' or 'cql'."}, status_code=400)
            safe = query.replace('"', '\\"')
            cql = f'type=page AND text ~ "{safe}"'
        data = _get(ctx.client, "/content/search",
                    {"cql": cql, "limit": limit, "expand": "space,version"})
        return JSONResponse([{
            "product": "confluence",
            "id": r.get("id"),
            "title": r.get("title"),
            "space": r.get("space", {}).get("key"),
            "version": r.get("version", {}).get("number"),
            "url": f"{ctx.site_root}/pages/viewpage.action?pageId={r.get('id')}",
        } for r in data.get("results", [])])
    except Exception as e:
        return _error(e)
    finally:
        ctx.client.close()


async def get_item(request):
    """Read a Confluence page by numeric id, or a Jira issue by key."""
    identifier = request.path_params["item_id"]
    try:
        ctx = _conf(request, identifier)
    except CredentialsError as e:
        return _error(e)
    try:
        if ctx.product == "jira":
            return JSONResponse(_fetch_issue(ctx, identifier))
        p = _get(ctx.client, f"/content/{identifier}", {"expand": "body.storage,version,space"})
        return JSONResponse(_page_payload(ctx.site_root, p))
    except Exception as e:
        return _error(e)
    finally:
        ctx.client.close()


async def get_item_by_title(request):
    """Confluence: exact page title in a space. Jira: best summary match in a project."""
    try:
        ctx = _conf(request)
    except CredentialsError as e:
        return _error(e)
    try:
        space_key = request.query_params.get("space_key", "")
        title = request.query_params.get("title", "")
        if not space_key or not title:
            return JSONResponse({"error": "Provide 'space_key' and 'title'."}, status_code=400)

        if ctx.product == "jira":
            jql = (f"project = {_jql_quote(space_key)} AND summary ~ {_jql_quote(title)} "
                   f"ORDER BY updated DESC")
            issues = _jira_search(ctx, jql, 1)
            if not issues:
                return JSONResponse(
                    {"found": False,
                     "message": f"No issue with summary matching '{title}' in project '{space_key}'."},
                    status_code=404)
            # Jira matches summaries by word, not exactly: this is the best match.
            return JSONResponse(_fetch_issue(ctx, issues[0]["key"]))

        data = _get(ctx.client, "/content", {
            "spaceKey": space_key, "title": title,
            "expand": "body.storage,version,space", "limit": 1,
        })
        results = data.get("results", [])
        if not results:
            return JSONResponse(
                {"found": False, "message": f"No page titled '{title}' in space '{space_key}'."},
                status_code=404)
        return JSONResponse(_page_payload(ctx.site_root, results[0]))
    except Exception as e:
        return _error(e)
    finally:
        ctx.client.close()


# --------------------------------------------------------------------------
# Endpoints: Jira read
# --------------------------------------------------------------------------
@_jira_endpoint
def issue_comments(ctx, request, body):
    """GET /issues/{key}/comments: {id, author, created, updated, text}, oldest first."""
    key = request.path_params["item_id"]
    limit = max(1, min(int(request.query_params.get("limit", "50")), 1000))
    data = _get(ctx.client, f"/issue/{key}/comment", {"maxResults": limit})
    out = []
    for c in data.get("comments", []):
        text, _ = _body_text(c.get("body"))
        out.append({"id": c.get("id"), "author": _named(c.get("author")),
                    "created": c.get("created"), "updated": c.get("updated"), "text": text})
    return {"issue_key": key, "comments": out}


@_jira_endpoint
def issue_transitions(ctx, request, body):
    """GET /issues/{key}/transitions: what the token owner can do from the current status."""
    key = request.path_params["item_id"]
    return {"issue_key": key, "transitions": _fetch_transitions(ctx, key)}


@_jira_endpoint
def projects(ctx, request, body):
    """GET /projects: projects visible to the token owner."""
    limit = max(1, min(int(request.query_params.get("limit", "50")), 100))
    if ctx.cloud:
        values = _get(ctx.client, "/project/search", {"maxResults": limit}).get("values", [])
    else:
        values = _req(ctx.client, "GET", "/project")
        values = values[:limit] if isinstance(values, list) else []
    return {"projects": [{"key": p.get("key"), "name": p.get("name"), "id": p.get("id"),
                          "type": p.get("projectTypeKey")} for p in values]}


@_jira_endpoint
def project_issue_types(ctx, request, body):
    """GET /projects/{key}/issue-types: {id, name, subtask}."""
    key = request.path_params["project_key"]
    p = _get(ctx.client, f"/project/{key}", {})
    return {"project": key,
            "issue_types": [{"id": t.get("id"), "name": t.get("name"), "subtask": bool(t.get("subtask"))}
                            for t in p.get("issueTypes", [])]}


@_jira_endpoint
def issue_link_types(ctx, request, body):
    """GET /issue-link-types: {name, outward, inward}."""
    types = _get(ctx.client, "/issueLinkType", {}).get("issueLinkTypes", [])
    return {"issue_link_types": [{"name": t.get("name"), "outward": t.get("outward"),
                                  "inward": t.get("inward")} for t in types]}


# --------------------------------------------------------------------------
# Endpoints: Jira write
# --------------------------------------------------------------------------
@_jira_endpoint
def create_issue(ctx, request, body):
    """POST /issues with {project_key, summary, issue_type?, description?, priority?, labels?,
    assignee?, parent_key?, extra_fields?}. parent_key makes a sub-task (issue_type must then be
    a sub-task type); on Cloud it also files a story under an epic."""
    project_key, summary = _text(body, "project_key"), _text(body, "summary")
    if not project_key or not summary:
        raise BadRequest("Provide 'project_key' and 'summary'.")
    issue_type = _text(body, "issue_type") or "Task"
    fields = {"project": {"key": project_key}, "summary": summary, "issuetype": {"name": issue_type}}
    fields.update({k: v for k, v in _issue_fields(ctx, body).items() if k != "summary"})
    parent_key = _text(body, "parent_key")
    if parent_key:
        fields["parent"] = {"key": parent_key}
    res = _req(ctx.client, "POST", "/issue", json={"fields": fields})
    key = res.get("key")
    return JSONResponse({"created": True, "key": key, "id": res.get("id"), "project": project_key,
                         "issue_type": issue_type, "parent": parent_key or None,
                         "url": _issue_url(ctx, key)}, status_code=201)


@_jira_endpoint
def update_issue(ctx, request, body):
    """PATCH or PUT /issues/{key} with any of {summary, description, priority, labels, assignee,
    extra_fields}. Only the given fields change; labels replaces the whole set."""
    key = request.path_params["item_id"]
    fields = _issue_fields(ctx, body)
    if not fields:
        raise BadRequest("No fields given. Send at least one of summary, description, priority, "
                         "labels, assignee, extra_fields.")
    _req(ctx.client, "PUT", f"/issue/{key}", json={"fields": fields})
    return {"issue_key": key, "updated": True, "fields": sorted(fields), "url": _issue_url(ctx, key)}


@_jira_endpoint
def delete_issue(ctx, request, body):
    """DELETE /issues/{key}?confirm=true[&delete_subtasks=true]. Without confirm: 400 + preview."""
    key = request.path_params["item_id"]
    cur = _fetch_issue(ctx, key)
    subtasks = cur["fields"].get("subtasks") or []
    if not _flag(request, "confirm"):
        return JSONResponse({
            "issue_key": key, "deleted": False,
            "would_delete": {"summary": cur.get("title"), "status": cur["fields"].get("status"),
                             "subtasks": subtasks},
            "note": "Preview only. Repeat with ?confirm=true to delete."
                    + (" This issue has sub-tasks: also pass delete_subtasks=true." if subtasks else ""),
        }, status_code=400)
    delete_subtasks = _flag(request, "delete_subtasks")
    if subtasks and not delete_subtasks:
        return JSONResponse({"issue_key": key, "deleted": False, "subtasks": subtasks,
                             "error": "Issue has sub-tasks. Pass delete_subtasks=true to delete them too."},
                            status_code=400)
    _req(ctx.client, "DELETE", f"/issue/{key}",
         params={"deleteSubtasks": "true" if delete_subtasks else "false"})
    return {"issue_key": key, "deleted": True, "subtasks_deleted": subtasks if delete_subtasks else []}


@_jira_endpoint
def add_comment(ctx, request, body):
    """POST /issues/{key}/comments with {text}."""
    key = request.path_params["item_id"]
    if not _text(body, "text"):
        raise BadRequest("Provide 'text'.")
    res = _req(ctx.client, "POST", f"/issue/{key}/comment",
               json={"body": _jira_body(body["text"], ctx.cloud)})
    return JSONResponse({"issue_key": key, "commented": True, "comment_id": res.get("id"),
                         "url": _issue_url(ctx, key)}, status_code=201)


@_jira_endpoint
def update_comment(ctx, request, body):
    """PUT /issues/{key}/comments/{id} with {text}."""
    key, cid = request.path_params["item_id"], request.path_params["comment_id"]
    if not _text(body, "text"):
        raise BadRequest("Provide 'text'.")
    res = _req(ctx.client, "PUT", f"/issue/{key}/comment/{cid}",
               json={"body": _jira_body(body["text"], ctx.cloud)})
    text, _ = _body_text(res.get("body"))
    return {"issue_key": key, "comment_id": cid, "ok": True, "updated": res.get("updated"), "text": text}


@_jira_endpoint
def delete_comment(ctx, request, body):
    """DELETE /issues/{key}/comments/{id}?confirm=true. Without confirm: 400 + preview."""
    key, cid = request.path_params["item_id"], request.path_params["comment_id"]
    cur = _get(ctx.client, f"/issue/{key}/comment/{cid}", {})
    text, _ = _body_text(cur.get("body"))
    if not _flag(request, "confirm"):
        return JSONResponse({"issue_key": key, "comment_id": cid, "deleted": False,
                             "would_delete": {"author": _named(cur.get("author")), "text": text[:500]},
                             "note": "Preview only. Repeat with ?confirm=true to delete."},
                            status_code=400)
    _req(ctx.client, "DELETE", f"/issue/{key}/comment/{cid}")
    return {"issue_key": key, "comment_id": cid, "deleted": True}


@_jira_endpoint
def transition_issue(ctx, request, body):
    """POST /issues/{key}/transitions with {transition, comment?, resolution?}. transition is an
    id, a name, or the target status name, case-insensitive. No match: 400 + the available list."""
    key = request.path_params["item_id"]
    wanted = _text(body, "transition").lower()
    if not wanted:
        raise BadRequest("Provide 'transition' (id, name, or target status).")
    available = _fetch_transitions(ctx, key)
    match = next((t for t in available if wanted in {
        str(t["id"]).lower(), (t["name"] or "").lower(), (t["to_status"] or "").lower(),
    }), None)
    if not match:
        return JSONResponse({"issue_key": key, "transitioned": False,
                             "error": f"No transition '{body.get('transition')}' is available "
                                      f"from the current status.",
                             "available": available}, status_code=400)
    payload: dict = {"transition": {"id": match["id"]}}
    resolution, comment = _text(body, "resolution"), _text(body, "comment")
    if resolution:
        payload["fields"] = {"resolution": {"name": resolution}}
    if comment:
        payload["update"] = {"comment": [{"add": {"body": _jira_body(body["comment"], ctx.cloud)}}]}
    _req(ctx.client, "POST", f"/issue/{key}/transitions", json=payload)
    return {"issue_key": key, "transitioned": True, "transition": match["name"],
            "status": match["to_status"], "commented": bool(comment), "url": _issue_url(ctx, key)}


@_jira_endpoint
def assign_issue(ctx, request, body):
    """PUT /issues/{key}/assignee with {assignee}: username or accountId, email or display name
    (looked up), "" to unassign, "-1" for the project default."""
    key = request.path_params["item_id"]
    if "assignee" not in body:
        raise BadRequest("Provide 'assignee' (\"\" to unassign, \"-1\" for the project default).")
    value = _text(body, "assignee")
    id_key = "accountId" if ctx.cloud else "name"
    if not value:
        payload = {id_key: None}
    elif value == "-1":
        payload = {id_key: "-1"}
    else:
        payload = _jira_user(ctx, value)
    _req(ctx.client, "PUT", f"/issue/{key}/assignee", json=payload)
    return {"issue_key": key, "assigned": True,
            "assignee": None if not value else ("(project default)" if value == "-1" else value),
            "url": _issue_url(ctx, key)}


@_jira_endpoint
def link_issues(ctx, request, body):
    """POST /issue-links with {from_key, to_key, link_type?}, read as from_key <link_type> to_key.
    link_type matches a type name or its outward/inward wording; the inward wording flips the
    direction. No match: 400 + the available types."""
    from_key, to_key = _text(body, "from_key"), _text(body, "to_key")
    if not from_key or not to_key:
        raise BadRequest("Provide 'from_key' and 'to_key'.")
    wanted = (_text(body, "link_type") or "Relates").lower()
    types = _get(ctx.client, "/issueLinkType", {}).get("issueLinkTypes", [])
    chosen, flip = None, False
    for t in types:
        if wanted in {(t.get("name") or "").lower(), (t.get("outward") or "").lower()}:
            chosen = t
            break
        if wanted == (t.get("inward") or "").lower():
            chosen, flip = t, True
            break
    if not chosen:
        return JSONResponse({"linked": False,
                             "error": f"No issue link type matches '{body.get('link_type')}'.",
                             "available": [{"name": t.get("name"), "outward": t.get("outward"),
                                            "inward": t.get("inward")} for t in types]},
                            status_code=400)
    outward, inward = (to_key, from_key) if flip else (from_key, to_key)
    _req(ctx.client, "POST", "/issueLink", json={"type": {"name": chosen["name"]},
                                                 "outwardIssue": {"key": outward},
                                                 "inwardIssue": {"key": inward}})
    return JSONResponse({"linked": True, "type": chosen["name"],
                         "text": f"{outward} {chosen.get('outward')} {inward}",
                         "url": _issue_url(ctx, from_key)}, status_code=201)


# by-title routes must be registered before the {item_id} catch-alls.
# Several routes share a path with different methods; Starlette picks by method.
app = Starlette(routes=[
    Route("/health", health),
    Route("/search", search),
    Route("/pages/by-title", get_item_by_title),
    Route("/pages/{item_id}", get_item),
    # Aliases: same handlers, readable URLs per product.
    Route("/items/by-title", get_item_by_title),
    Route("/items/{item_id}", get_item),
    Route("/issues/{item_id}", get_item),
    # Jira read
    Route("/issues/{item_id}/comments", issue_comments),
    Route("/issues/{item_id}/transitions", issue_transitions),
    Route("/projects", projects),
    Route("/projects/{project_key}/issue-types", project_issue_types),
    Route("/issue-link-types", issue_link_types),
    # Jira write
    Route("/issues", create_issue, methods=["POST"]),
    Route("/issues/{item_id}", update_issue, methods=["PATCH", "PUT"]),
    Route("/issues/{item_id}", delete_issue, methods=["DELETE"]),
    Route("/issues/{item_id}/comments", add_comment, methods=["POST"]),
    Route("/issues/{item_id}/comments/{comment_id}", update_comment, methods=["PUT"]),
    Route("/issues/{item_id}/comments/{comment_id}", delete_comment, methods=["DELETE"]),
    Route("/issues/{item_id}/transitions", transition_issue, methods=["POST"]),
    Route("/issues/{item_id}/assignee", assign_issue, methods=["PUT"]),
    Route("/issue-links", link_issues, methods=["POST"]),
])
