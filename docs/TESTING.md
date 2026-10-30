# Testing and evidence

## Local validation

```bash
uv sync --frozen
make check
make test
```

The offline tests use fake AWS clients and the installed botocore request
contracts. They make no network calls and do not create resources. They check:

- Profile ARNs and native RAG requests match the SDK contract.
- Retrieval and generation use the intended regional clients.
- Source text survives prompt construction and output token limits are explicit.
- API success with an incorrect known answer fails the semantic check.
- Empty retrieval, wrong source-Region profiles, and account mismatches stop
  dependent calls.
- Foreign-ARN rejection is recorded separately from the supported paths.
- Access-denied calls are not retried or counted as passing.
- Credential-provider exception text is not copied into reports.
- Cleanup refuses mismatched tags, unexpected objects, truncated listings, and
  unrelated role policies.
- Permission failures are not treated as resource absence.
- Deletion is verified; already-absent resources support safe cleanup replay.
- A KB creation timeout preserves the same idempotency token for resumption.
- Fixed-dimension Cohere models omit configurable embedding dimensions.
- Failed ingestion is never marked ready.

## Provider-live smoke test

```bash
uv run --frozen python scripts/live_smoke.py \
  --account 123456789012 --profile YOUR_PROFILE
```

This requires explicit AWS credentials and makes paid calls. It creates one
synthetic Singapore vector KB, checks `ORCHID-7319` in both generation answers,
and attempts cleanup in a `finally` block.

The runner saves raw output locally under `artifacts/` and emits a sanitized
`summary.json`. The committed [30 October 2026 summary](evidence/2026-10-30.json)
records observed outcomes and code hashes. It does not include account IDs,
principal ARNs, request IDs, HTTP headers, or raw retrieved references.

The recorded test establishes native inference-profile interoperability and the
explicit Singapore-retrieval/Sydney-generation path. It does not establish the
profile request's actual inference destination, parsing support, production
quality, or a latency benchmark. The default native Nova Lite answer contained
a tool-style preamble; evaluate a suitable generation prompt before production
use.
