# Atlassian reader (REST API)

Read-only HTTP API for **Confluence pages and Jira issues**, over the same
three endpoints. The product is chosen per request, so callers do not need
to know or care which one they are hitting.

| Endpoint | Confluence | Jira |
|---|---|---|
| `GET /health` | Liveness check, no credentials needed | |
| `GET /search?query=...&limit=...` | CQL `text ~ "query"` | JQL `text ~ "query"` |
| `GET /search?cql=...` | Raw CQL (forces Confluence) | |
| `GET /search?jql=...` | | Raw JQL (forces Jira) |
| `GET /pages/{id}` | Page by numeric id | Issue by key (`PROJ-123`) |
| `GET /pages/by-title?space_key=&title=` | Exact page title in a space | Best summary match in a project |

Aliases with identical behaviour, for readable URLs: `/issues/{key}`,
`/items/{id}`, `/items/by-title`.

No write, comment, attachment or admin operations are included.

## Credentials

The image contains **no credentials**. Every caller sends their own personal
token on each request as HTTP headers.

| Header | Required | Meaning |
|---|---|---|
| `X-Atlassian-Url` | yes | Base URL, e.g. `https://jira.yourcompany.com` |
| `X-Atlassian-Token` | yes | PAT (Data Center) or API token (Cloud) |
| `X-Atlassian-Email` | Cloud only | Its presence switches auth from Bearer (Data Center) to Basic email+token (Cloud) |
| `X-Atlassian-Product` | no | `confluence` or `jira`; overrides detection |

The legacy `X-Confluence-*` headers are still accepted and pin the product to
Confluence, so existing callers keep working unchanged.

## How the product is chosen

In order, strongest signal first:

1. `X-Atlassian-Product` header, when present.
2. Legacy `X-Confluence-*` headers with no `X-Atlassian-Url` -> Confluence.
3. Identifier shape: `PROJ-123` is a Jira key, `131664681` a Confluence page id.
4. Query parameter: `jql` -> Jira, `cql` -> Confluence.
5. Target URL: `/wiki` or a host containing `confluence` -> Confluence, a host
   containing `jira` -> Jira.
6. Default: Confluence.

Rule 5 alone is not reliable (on Cloud both products share one host, and
Data Center host names are arbitrary), which is why it sits last. Send
`X-Atlassian-Product` whenever you want certainty.

## Response shape

Both products return the same keys, so consumers keep one code path:
`product`, `id`, `title`, `space`, `version`, `text_preview`, `storage_body`,
`url`. For an issue, `id` is the key, `title` the summary, `space` the project
key, and `version` is null. Issue-specific values (status, issue type,
assignee, reporter, priority, resolution, labels, parent, created, updated)
sit in an extra `fields` object.

## API versions used

| | Data Center / Server | Cloud |
|---|---|---|
| Confluence | `/rest/api` | `/wiki/rest/api` |
| Jira | `/rest/api/2` | `/rest/api/3` |

Jira Cloud moved issue search to `/search/jql`. The reader tries that first
and falls back to `/search`, so both generations work.

## Build and run

```bash
docker build -t atlassian-reader .
docker run -d --name atlassian-reader -p 8000:8000 atlassian-reader
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
```

Errors come back as JSON: `400` for missing credentials or parameters,
Atlassian's own status (`401`, `404`, ...) passed through, `502` when the
host cannot be reached.

## Security

* The token travels in every request header. On localhost or a trusted
  network that is fine; anywhere else, put the container behind TLS
  (reverse proxy, or Railway's built-in HTTPS) so headers are encrypted
  in transit.
* Each request is attributed in Confluence or Jira to the owner of the
  token sent.
