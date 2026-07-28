"""
Confluence pages reader (read-only REST API).

Endpoints:
  GET /health                                   liveness, no credentials needed
  GET /search?query=...&cql=...&limit=...       free-text or raw-CQL page search
  GET /pages/by-title?space_key=...&title=...   exact-title lookup in a space
  GET /pages/{page_id}                          title, space, version, text preview, raw storage body

Credentials are NOT stored in the image. Every request must carry them as
HTTP headers:

  X-Confluence-Url:    https://confluence.yourcompany.com
  X-Confluence-Token:  <PAT (Data Center) or API token (Cloud)>
  X-Confluence-Email:  <Cloud only; presence of this header selects Cloud auth>

Data Center / Server -> Bearer token auth (no email header).
Cloud                -> Basic auth with email + API token (email header present).

One running container therefore serves any Confluence instance and any
user; each caller is attributed to the owner of the token they send.

Start command: uvicorn server:app --host 0.0.0.0 --port 8000
"""

import re
import html as _html

import httpx
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

_ACCEPT = {"Accept": "application/json"}
_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# Per-request configuration (from headers)
# --------------------------------------------------------------------------
class CredentialsError(Exception):
    pass


def _conf(request) -> tuple[httpx.Client, str]:
    """Read credentials from the incoming request headers and build a client.

    Returns (client, api_base). The caller must close the client.
    """
    url = request.headers.get("x-confluence-url", "").strip().rstrip("/")
    token = request.headers.get("x-confluence-token", "").strip()
    email = request.headers.get("x-confluence-email", "").strip()

    if not url or not token:
        raise CredentialsError(
            "Missing credentials. Send X-Confluence-Url and X-Confluence-Token "
            "headers (plus X-Confluence-Email for Cloud)."
        )

    if email:  # Cloud: Basic auth with email + API token
        base = url[:-5] if url.endswith("/wiki") else url
        api_base = f"{base}/wiki/rest/api"
        client = httpx.Client(base_url=api_base, auth=(email, token),
                              headers=_ACCEPT, timeout=30.0)
    else:  # Data Center / Server: Bearer PAT
        api_base = f"{url}/rest/api"
        client = httpx.Client(base_url=api_base,
                              headers={**_ACCEPT, "Authorization": f"Bearer {token}"},
                              timeout=30.0)
    return client, api_base


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _get(client: httpx.Client, path: str, params: dict) -> dict:
    resp = client.get(path, params=params)
    resp.raise_for_status()
    return resp.json() if resp.content else {}


def _strip_tags(storage: str) -> str:
    """Crude storage-XHTML -> readable text for previews."""
    if not storage:
        return ""
    return re.sub(r"\s+", " ", _html.unescape(_TAG_RE.sub(" ", storage))).strip()


def _page_url(api_base: str, page_id: str) -> str:
    root = api_base.rsplit("/rest/api", 1)[0]
    return f"{root}/pages/viewpage.action?pageId={page_id}"


def _page_payload(api_base: str, p: dict) -> dict:
    storage = p.get("body", {}).get("storage", {}).get("value", "")
    return {
        "id": p.get("id"),
        "title": p.get("title"),
        "space": p.get("space", {}).get("key"),
        "version": p.get("version", {}).get("number"),
        "text_preview": _strip_tags(storage)[:4000],
        "storage_body": storage,
        "url": _page_url(api_base, p.get("id")),
    }


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, CredentialsError):
        return JSONResponse({"error": str(exc)}, status_code=400)
    if isinstance(exc, httpx.HTTPStatusError):
        body = (exc.response.text or "")[:600]
        return JSONResponse({"error": f"Confluence {exc.response.status_code}", "detail": body},
                            status_code=exc.response.status_code)
    if isinstance(exc, httpx.HTTPError):
        return JSONResponse({"error": f"Network error reaching Confluence: {exc}"}, status_code=502)
    return JSONResponse({"error": str(exc)}, status_code=500)


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------
async def health(request):
    return JSONResponse({"status": "ok", "service": "confluence-pages-reader"})


async def search(request):
    """Free-text or raw-CQL page search. Returns [{id, title, space, version, url}]."""
    try:
        client, api_base = _conf(request)
    except CredentialsError as e:
        return _error(e)
    try:
        query = request.query_params.get("query", "")
        cql = request.query_params.get("cql", "")
        limit = int(request.query_params.get("limit", "25"))
        if not cql:
            if not query:
                return JSONResponse({"error": "Provide 'query' or 'cql'."}, status_code=400)
            safe = query.replace('"', '\\"')
            cql = f'type=page AND text ~ "{safe}"'
        data = _get(client, "/content/search",
                    {"cql": cql, "limit": limit, "expand": "space,version"})
        out = [{
            "id": r.get("id"),
            "title": r.get("title"),
            "space": r.get("space", {}).get("key"),
            "version": r.get("version", {}).get("number"),
            "url": _page_url(api_base, r.get("id")),
        } for r in data.get("results", [])]
        return JSONResponse(out)
    except Exception as e:
        return _error(e)
    finally:
        client.close()


async def get_page(request):
    """Read a page by ID."""
    try:
        client, api_base = _conf(request)
    except CredentialsError as e:
        return _error(e)
    try:
        page_id = request.path_params["page_id"]
        p = _get(client, f"/content/{page_id}", {"expand": "body.storage,version,space"})
        return JSONResponse(_page_payload(api_base, p))
    except Exception as e:
        return _error(e)
    finally:
        client.close()


async def get_page_by_title(request):
    """Find a page by its exact title within a space; same payload as /pages/{id}."""
    try:
        client, api_base = _conf(request)
    except CredentialsError as e:
        return _error(e)
    try:
        space_key = request.query_params.get("space_key", "")
        title = request.query_params.get("title", "")
        if not space_key or not title:
            return JSONResponse({"error": "Provide 'space_key' and 'title'."}, status_code=400)
        data = _get(client, "/content", {
            "spaceKey": space_key, "title": title,
            "expand": "body.storage,version,space", "limit": 1,
        })
        results = data.get("results", [])
        if not results:
            return JSONResponse(
                {"found": False, "message": f"No page titled '{title}' in space '{space_key}'."},
                status_code=404)
        return JSONResponse(_page_payload(api_base, results[0]))
    except Exception as e:
        return _error(e)
    finally:
        client.close()


# /pages/by-title must be registered before /pages/{page_id}
app = Starlette(routes=[
    Route("/health", health),
    Route("/search", search),
    Route("/pages/by-title", get_page_by_title),
    Route("/pages/{page_id}", get_page),
])
