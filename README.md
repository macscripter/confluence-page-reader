# Atlassian API (Confluence read, Jira read + write)

HTTP API for **Confluence pages and Jira issues**. The three read endpoints
serve both products and pick the product per request, so callers do not need
to know or care which one they are hitting. The Jira-specific routes add
comments, transitions, projects, and every write the MCP server offers:
create, update, comment, transition, assign, link, delete. Confluence stays
read-only here.

## Read endpoints (both products)

| Endpoint | Confluence | Jira |
|---|---|---|
| `GET /health` | Liveness check, no credentials needed | |
| `GET /search?query=...&limit=...` | CQL `text ~ "query"` | JQL `text ~ "query"` |
| `GET /search?query=...&project_key=...` | | Free text inside one project (empty `query` lists the project's latest issues) |
| `GET /search?cql=...` | Raw CQL (forces Confluence) | |
| `GET /search?jql=...` | | Raw JQL (forces Jira) |
| `GET /pages/{id}` | Page by numeric id | Issue by key (`PROJ-123`) |
| `GET /pages/by-title?space_key=&title=` | Exact page title in a space | Best summary match in a project |

Aliases with identical behaviour, for readable URLs: `/issues/{key}`,
`/items/{id}`, `/items/by-title`.

## Jira read endpoints

| Endpoint | Returns |
|---|---|
| `GET /issues/{key}/comments?limit=` | `{issue_key, comments: [{id, author, created, updated, text}]}`, oldest first |
| `GET /issues/{key}/transitions` | `{issue_key, transitions: [{id, name, to_status}]}` available from the current status |
| `GET /projects?limit=` | `{projects: [{key, name, id, type}]}` visible to the token owner |
| `GET /projects/{key}/issue-types` | `{project, issue_types: [{id, name, subtask}]}` |
| `GET /issue-link-types` | `{issue_link_types: [{name, outward, inward}]}` |

## Jira write endpoints

All bodies are JSON objects. Every write is attributed in Jira to the owner of
the token sent.

| Endpoint | Body | Notes |
|---|---|---|
| `POST /issues` | `project_key`, `summary`, `issue_type?` (default `Task`), `description?`, `priority?`, `labels?`, `assignee?`, `parent_key?`, `extra_fields?` | `201`. `parent_key` makes a sub-task (the type must then be a sub-task type); on Cloud it also files a story under an epic |
| `PATCH /issues/{key}` (`PUT` accepted) | any of `summary`, `description`, `priority`, `labels`, `assignee`, `extra_fields` | Only the given fields change. `labels` replaces the whole set. Status changes go through transitions |
| `DELETE /issues/{key}?confirm=true&delete_subtasks=true` | | Without `confirm`: `400` with a preview. An issue with sub-tasks needs `delete_subtasks=true`. Jira has no trash for issues |
| `POST /issues/{key}/comments` | `text` | `201` with `comment_id` |
| `PUT /issues/{key}/comments/{id}` | `text` | Only comments the token owner may edit, in practice their own |
| `DELETE /issues/{key}/comments/{id}?confirm=true` | | Without `confirm`: `400` with a preview |
| `POST /issues/{key}/transitions` | `transition`, `comment?`, `resolution?` | `transition` is an id, a name, or the target status, case-insensitive. No match: `400` with the available list |
| `PUT /issues/{key}/assignee` | `assignee` | Username (Data Center) or accountId (Cloud); an email or display name is looked up. `""` unassigns, `"-1"` uses the project default |
| `POST /issue-links` | `from_key`, `to_key`, `link_type?` (default `Relates`) | Read as `from_key <link_type> to_key`. A type name or its outward/inward wording; the inward wording flips the direction. No match: `400` with the available types |

Notes:

- Text bodies (`description`, `text`, `comment`). Cloud uses REST v3, where
  these are ADF JSON: plain text is converted (blank line = new paragraph,
  single newline = line break), and a JSON string that already is an ADF
  document is passed through. Data Center uses REST v2 and takes wiki markup
  as-is. Reads return the raw body in either format under `storage_body`.
- People. Data Center keys users by username, Cloud by accountId. Pass
  either directly, or an email address / display name and the server looks
  it up; an ambiguous name fails with the candidates listed.
- Custom fields. `extra_fields` is a raw Jira `fields` object merged as-is,
  e.g. `{"duedate": "2026-09-30"}` or a Data Center epic link
  `{"customfield_10014": "SC3-1"}`.
- Search results are capped at 100 per call.

## Credentials

The image contains **no credentials**. Every caller sends their own personal
token on each request as HTTP headers.

| Header | Required | Meaning |
|---|---|---|
| `X-Atlassian-Url` | yes | Base URL, e.g. `https://jira.yourcompany.com` |
| `X-Atlassian-Token` | yes | PAT (Data Center) or API token (Cloud) |
| `X-Atlassian-Email` | Cloud only | Its presence switches auth from Bearer (Data Center) to Basic email+token (Cloud) |
| `X-Atlassian-Product` | no | `confluence` or `jira`; overrides detection on the shared read endpoints |

The legacy `X-Confluence-*` headers are still accepted and pin the product to
Confluence, so existing callers keep working unchanged.

On Data Center a Personal Access Token is per product: send a Jira PAT to
the Jira routes and a Confluence PAT to the Confluence ones. On Cloud the
same API token and email work for both.

## How the product is chosen

The Jira-specific routes (`/issues/...`, `/projects...`, `/issue-links`,
`/issue-link-types`) always target Jira. For the shared read endpoints, in
order, strongest signal first:

1. `X-Atlassian-Product` header, when present.
2. Legacy `X-Confluence-*` headers with no `X-Atlassian-Url` -> Confluence.
3. Identifier shape: `PROJ-123` is a Jira key, `131664681` a Confluence page id.
4. Query parameter: `jql` or `project_key` -> Jira, `cql` -> Confluence.
5. Target URL: `/wiki` or a host containing `confluence` -> Confluence, a host
   containing `jira` -> Jira.
6. Default: Confluence.

Rule 5 alone is not reliable (on Cloud both products share one host, and
Data Center host names are arbitrary), which is why it sits last. Send
`X-Atlassian-Product` whenever you want certainty.

## Response shape

Both products return the same keys on the read endpoints, so consumers keep
one code path: `product`, `id`, `title`, `space`, `version`, `text_preview`,
`storage_body`, `url`. For an issue, `id` is the key, `title` the summary,
`space` the project key, and `version` is null. Issue-specific values sit in
an extra `fields` object: `issue_id`, `status`, `issue_type`, `priority`,
`assignee`, `reporter`, `resolution`, `labels`, `components`,
`fix_versions`, `parent`, `subtasks`, `links`, `created`, `updated`,
`due_date`. Search results for Jira also carry `issue_type`, `priority`
and `assignee`.

## API versions used

| | Data Center / Server | Cloud |
|---|---|---|
| Confluence | `/rest/api` | `/wiki/rest/api` |
| Jira | `/rest/api/2` | `/rest/api/3` |

Jira Cloud moved issue search to `/search/jql`. The server tries the route
that matches the deployment first and falls back to the other, so both
generations work.

## Build and run

```bash
docker build -t atlassian-api .
docker run -d --name atlassian-api -p 8000:8000 atlassian-api
```

## Use

```bash
curl http://localhost:8000/health

# Confluence page
curl "http://localhost:8000/pages/131664681" \
  -H "X-Atlassian-Url: https://confluence.yourcompany.com" \
  -H "X-Atlassian-Token: <token>"

# Jira issue, same endpoint
curl "http://localhost:8000/pages/SIMPL-42" \
  -H "X-Atlassian-Url: https://jira.yourcompany.com" \
  -H "X-Atlassian-Token: <token>"

# Raw JQL
curl -G "http://localhost:8000/search" \
  --data-urlencode 'jql=project = SIMPL AND status != Done ORDER BY updated DESC' \
  -H "X-Atlassian-Url: https://jira.yourcompany.com" \
  -H "X-Atlassian-Token: <token>"

# Create an issue
curl -X POST "http://localhost:8000/issues" \
  -H "X-Atlassian-Url: https://jira.yourcompany.com" \
  -H "X-Atlassian-Token: <token>" \
  -H "Content-Type: application/json" \
  -d '{"project_key": "SIMPL", "summary": "Review BP02 comments", "issue_type": "Task",
       "description": "First paragraph.\n\nSecond paragraph.", "labels": ["sc3"]}'

# Comment, transition, assign
curl -X POST "http://localhost:8000/issues/SIMPL-42/comments" \
  -H "X-Atlassian-Url: https://jira.yourcompany.com" -H "X-Atlassian-Token: <token>" \
  -H "Content-Type: application/json" -d '{"text": "Answered on the Confluence page."}'

curl -X POST "http://localhost:8000/issues/SIMPL-42/transitions" \
  -H "X-Atlassian-Url: https://jira.yourcompany.com" -H "X-Atlassian-Token: <token>" \
  -H "Content-Type: application/json" -d '{"transition": "Done", "resolution": "Done"}'

curl -X PUT "http://localhost:8000/issues/SIMPL-42/assignee" \
  -H "X-Atlassian-Url: https://jira.yourcompany.com" -H "X-Atlassian-Token: <token>" \
  -H "Content-Type: application/json" -d '{"assignee": "alex@yourcompany.com"}'

# Delete: preview first, then confirm
curl -X DELETE "http://localhost:8000/issues/SIMPL-42" \
  -H "X-Atlassian-Url: https://jira.yourcompany.com" -H "X-Atlassian-Token: <token>"
curl -X DELETE "http://localhost:8000/issues/SIMPL-42?confirm=true" \
  -H "X-Atlassian-Url: https://jira.yourcompany.com" -H "X-Atlassian-Token: <token>"
```

Errors come back as JSON: `400` for missing credentials, missing or invalid
parameters or bodies, unmatched transitions or link types, and unconfirmed
deletes (with a preview); Atlassian's own status (`401`, `403`, `404`, ...)
passed through with its response text in `detail`; `502` when the host
cannot be reached; `405` for a method a route does not support.

## Security

* The token travels in every request header. On localhost or a trusted
  network that is fine; anywhere else, put the container behind TLS
  (reverse proxy, or Railway's built-in HTTPS) so headers are encrypted
  in transit.
* Each request is attributed in Confluence or Jira to the owner of the
  token sent, writes included. Anyone who can reach the service with a
  token can change Jira on that token's behalf, so do not expose it
  without TLS and network controls.
