# Collector fixtures

One JSON file per source, replayed offline by `tests/replay.py`. Nothing here
touches the network: `replay.install()` patches
`atlas.collectors.base._http_get`, the single request primitive, so collectors
run their real request/retry/parse code against a stored response.

## Shape

```json
{
  "source": "himalayas",
  "url": "https://himalayas.app/jobs/api/search?limit=20",
  "captured": "2026-10-05",
  "cases": {
    "normal":   { "status": 200, "headers": {}, "body": {}, "provenance": "recorded" },
    "empty":    { "status": 200, "headers": {}, "body": {}, "provenance": "recorded" },
    "malformed":{ "status": 200, "headers": {}, "body": "<html>", "provenance": "synthetic" },
    "http_429": { "status": 429, "headers": { "Retry-After": "60" }, "body": {}, "provenance": "synthetic" },
    "http_500": { "status": 500, "headers": {}, "body": {}, "provenance": "synthetic" }
  }
}
```

A `body` that is a JSON string is served as a raw text body (used for the
malformed-JSON and XML/Atom cases); any other `body` is served as JSON.

## The five cases

Every source must cover all five — they are the resilience contract:

| case | what it proves |
| --- | --- |
| `normal` | the happy path maps a real response into `Job` objects |
| `empty` | a legitimately empty listing yields `[]`, not an error |
| `malformed` | an unusable payload yields `[]` via `safe_fetch`, never a traceback |
| `http_429` | a rate limit is retried up to `max_attempts`, honouring `Retry-After`, then yields `[]` |
| `http_500` | a server error is retried the same way, then yields `[]` |

## Provenance

`provenance` is recorded per case, never assumed:

- `recorded` — captured from the live endpoint on the date in `captured`.
- `synthetic` — hand-written stand-ins that no live endpoint reliably produces
  (429/500 bodies, and non-JSON payloads), kept minimal and obviously shaped.

Live endpoints are volatile: companies change, feeds rotate, and an error page
served with a 200 status would otherwise be "recorded" as truth. Keeping the
fixtures at the HTTP boundary means a source rotating its schema surfaces as a
`malformed` failure rather than a silently changed expected value.

These fixtures are committed. The JobD labelling samples in
`eval/labeling_pool.jsonl` are local-only and never committed.