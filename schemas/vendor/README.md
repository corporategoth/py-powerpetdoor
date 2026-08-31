# Vendored schemas

Third-party schemas, committed so validation runs offline and against a
pinned version rather than whatever the network serves today.

| File | Source | Why |
|------|--------|-----|
| `asyncapi-3.0.0.json` | <https://asyncapi.com/definitions/3.0.0.json> | Validates `schemas/asyncapi.json`. Fetching it at test time would make the suite depend on the network and on AsyncAPI's CDN being up. |

Not automated: Dependabot has no notion of a downloaded JSON file. Refresh
by hand if this project ever targets a newer AsyncAPI version, and expect
the generator to need changes at the same time.
