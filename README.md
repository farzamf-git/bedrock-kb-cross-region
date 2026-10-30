# Bedrock Knowledge Base cross-Region test

A small, reproducible experiment answering: **Can a Bedrock Knowledge Base in
Singapore use a generation model in another AWS Region?**

**Yes, through a supported inference profile.** Alternatively, application code
can retrieve from the Singapore KB and invoke an on-demand model at another
Region's runtime endpoint. The live test also found that passing a Sydney
foundation-model ARN directly to Singapore `RetrieveAndGenerate` was rejected.

This repository includes query code, temporary resource creation and cleanup,
offline regression tests, an architecture diagram, and a sanitized live-test
record. The experiment uses only the synthetic [sample document](sample.txt).

## Quick start

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and an AWS credential
profile with permission to create the temporary resources and invoke the selected
models. Credentials are resolved through the normal boto3 credential chain.

```bash
uv sync --frozen
make check

# Read-only discovery; without --kb-id no generation calls are made.
uv run --frozen python probe.py --profile YOUR_PROFILE
```

For the complete live experiment, replace the example account and profile:

```bash
uv run --frozen python scripts/live_smoke.py \
  --account 123456789012 \
  --profile YOUR_PROFILE \
  --inference-profile global.anthropic.claude-haiku-4-5-20251001-v1:0
```

The current default is the **global Claude Haiku 4.5** inference profile.
Both entrypoints accept `--inference-profile` to select another supported
profile. Global routing may process the query and retrieved text outside APAC.

The runner creates a private S3 source bucket, an S3 vector bucket/index, a
dedicated IAM service role, a Singapore KB, and a data source. It indexes one
document, runs both generation paths and the foreign-ARN comparison, then
attempts cleanup even when the probe fails. **This creates AWS resources and
makes paid calls.** Exit zero requires both generation paths to pass and cleanup
to succeed.

Raw SDK responses, resource IDs, and manifests remain in ignored `artifacts/`.
The runner also produces a summary without account IDs, principal ARNs, request
IDs, headers, or raw source references.

## Query an existing Knowledge Base

Use an ingested vector KB in Singapore. To check the known answer, first ingest
`sample.txt` through its configured data source.

```bash
uv run --frozen python probe.py \
  --account 123456789012 \
  --profile YOUR_PROFILE \
  --kb-region ap-southeast-1 \
  --kb-id YOUR_KB_ID \
  --question "What is the verification code for Project Orchid?" \
  --expected-text ORCHID-7319 \
  --inference-profile global.anthropic.claude-haiku-4-5-20251001-v1:0 \
  --remote-region ap-southeast-2 \
  --remote-model amazon.nova-lite-v1:0 \
  --test-foreign-arn \
  --output artifacts/results.json
```

`BOTH_PATHS_PASSED` requires successful retrieval and both generation calls.
When `--expected-text` is supplied, it must appear in both answers. Without it,
the result establishes API success only. The optional foreign-ARN comparison
records the actual AWS result and does not determine the supported paths' pass
status.

## Manage a temporary KB separately

```bash
uv run --frozen python manage.py create \
  --account 123456789012 --profile YOUR_PROFILE

# Use resources.kb_id from artifacts/resources.json with probe.py.

uv run --frozen python manage.py cleanup \
  --account 123456789012 --profile YOUR_PROFILE
```

Creation refuses to overwrite an existing manifest. Use `--manifest` to select a
new run path after the previous resources have been removed. Cleanup checks the
expected account, manifest Region, ownership tags, source bucket contents, and
role policies before deleting resources. It verifies absence after deletion and
stops dependent deletions when a check fails.

If KB creation timed out after the buckets, index, and role were created, use
`resume-kb-creation` with the same account, Region, and manifest. The saved
idempotency token prevents a duplicate KB request. Resumption intentionally
refuses to run after a KB ID has been recorded. Inspect the manifest and AWS state
for failures at other stages; do not delete the manifest and blindly rerun.

## What the test establishes

The saved live evidence records the earlier Nova Lite configuration. The current
Haiku 4.5 profile was resolved as `ACTIVE` in Singapore through a read-only AWS
lookup, and the code change passed offline tests. It has not been rerun against
a live KB; the earlier results do not establish Haiku generation success.

The provider-live test on **30 October 2026** indexed one document with zero
failures and returned the correct answer through both paths. All temporary AWS
resources were removed and their absence verified.

| Path | Result |
| --- | --- |
| Singapore `RetrieveAndGenerate` → APAC Nova Lite profile | Correct answer with native citation |
| Singapore `RetrieveAndGenerate` → Sydney model ARN directly | `ValidationException` |
| Singapore `Retrieve` → Sydney Nova Lite `Converse` | Correct answer with application source label |

An inference profile can also route to Singapore. A successful profile request
does not prove that it left Singapore. To establish its actual processing
Region, inspect the corresponding Bedrock CloudTrail event's
`additionalEventData.inferenceRegion`. Use the split path with an in-Region
on-demand model when your application needs to choose a specific inference
Region.

The earlier native Nova Lite RAG response included a tool-style preamble.
Production use needs prompt tuning and quality evaluation. The fixture is a
smoke test, not a benchmark; parsing and production data were not tested.

## Code and documentation

| File | Purpose |
| --- | --- |
| [probe.py](probe.py) | Read-only discovery and the three query comparisons |
| [manage.py](manage.py) | Scoped provisioning, ingestion, recovery, and cleanup |
| [scripts/live_smoke.py](scripts/live_smoke.py) | Complete disposable live experiment |
| [tests/](tests/) | Offline request, semantic pass/fail, and cleanup regression tests |
| [Architecture](docs/ARCHITECTURE.md) | Region boundaries, call paths, lifecycle, and tradeoffs |
| [Testing](docs/TESTING.md) | Local checks and provider-live evidence |
| [AWS prerequisites](docs/AWS_PREREQUISITES.md) | Models, permissions, residency, and failure handling |
| [Open-source readiness](docs/OPEN_SOURCE_READINESS.md) | Credential, portability, and license review |

AWS documents [inference-profile use with Knowledge Bases](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-use.html),
[KB model and Region support](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-supported.html),
and [cross-Region routing](https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html).

## License

This project is licensed under the [Apache License 2.0](LICENSE).
