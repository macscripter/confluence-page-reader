# Confluence pages reader (REST API)

Read-only HTTP API for Confluence pages.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Liveness check, no credentials needed |
| `GET /search?query=...&cql=...&limit=...` | Free-text or raw-CQL page search |
| `GET /pages/{page_id}` | Title, space, version, text preview, raw storage body |
| `GET /pages/by-title?space_key=...&title=...` | Exact-title lookup in a space, same payload as `/pages/{id}` |

No write, comment, attachment or admin operations are included.

The image contains **no credentials**. Every caller sends their own personal
token on each request as HTTP headers, so one running container serves any
Confluence instance and any user, and credentials can be rotated or the
target instance switched without rebuilding anything.

| Header | Required | Meaning |
|---|---|---|
| `X-Confluence-Url` | yes | Base URL, e.g. `https://confluence.yourcompany.com` |
| `X-Confluence-Token` | yes | PAT (Data Center) or API token (Cloud) |
| `X-Confluence-Email` | Cloud only | Its presence switches auth from Bearer (Data Center) to Basic email+token (Cloud) |

## 1. Build

```bash
docker build -t confluence-pages-reader .
```

## 2. Run

```bash
docker run -d --name confluence-reader -p 8000:8000 confluence-pages-reader
```

## 3. Use

```bash
curl http://localhost:8000/health

curl "http://localhost:8000/pages/131664681" \
  -H "X-Confluence-Url: https://confluence.yourcompany.com" \
  -H "X-Confluence-Token: <your_token>"

curl "http://localhost:8000/search?query=DOME&limit=10" \
  -H "X-Confluence-Url: https://confluence.yourcompany.com" \
  -H "X-Confluence-Token: <your_token>"
```

Errors come back as JSON: `400` for missing credentials or parameters,
Confluence's own status (`401`, `404`, ...) passed through, `502` when the
Confluence host cannot be reached.

## Security

* The token travels in every request header. On localhost or a trusted
  network that is fine; anywhere else, put the container behind TLS
  (reverse proxy, or Railway's built-in HTTPS) so headers are encrypted
  in transit.
* Each request is attributed in Confluence to the owner of the token sent.
