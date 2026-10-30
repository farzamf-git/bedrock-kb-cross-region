# AWS prerequisites

The example targets Singapore (`ap-southeast-1`) with these resources:

| Purpose | Selected resource |
| --- | --- |
| Embeddings | `cohere.embed-english-v3` in Singapore |
| Managed RAG generation | `apac.amazon.nova-lite-v1:0` profile resolved in Singapore |
| Explicit remote generation | `amazon.nova-lite-v1:0` on demand in Sydney (`ap-southeast-2`) |
| Vector store | S3 Vectors, 1024 dimensions, float32, cosine |
| Chunking | `NONE`, because the entire synthetic fixture is below 300 tokens |

Verify availability and invocation access in your own account. A successful
model listing does not establish invocation access. Cohere Embed v3 has fixed
1024 dimensions: configure the vector index with that dimension and omit
configurable dimensions from the KB request.

## Caller permissions

Provisioning requires the relevant S3 bucket/object configuration operations,
S3 Vectors bucket/index operations and tagging, IAM role/policy operations,
`iam:PassRole` for the temporary service role, and Bedrock KB/data-source/
ingestion management operations. Cleanup needs the corresponding describe,
tag-read, and deletion operations.

Querying requires:

- `bedrock:GetInferenceProfile`, `bedrock:GetKnowledgeBase`, and
  `bedrock:ListKnowledgeBases` for discovery.
- `bedrock:Retrieve` and `bedrock:RetrieveAndGenerate`.
- `bedrock:InvokeModel` for the selected profile, eligible destination models,
  and the explicit Sydney model.

Use a role scoped to the temporary resources and selected models. The script
creates a scoped KB service role; it does not grant or modify the caller's
permissions. Organization SCPs must permit the profile's destinations and the
explicit remote Region. A regional embedding model, a generation profile, and a
remote runtime endpoint are separate permissions and availability checks.

## Data residency

The profile can process the question and retrieved context in any eligible
destination, including Singapore. The split path sends that same context to the
chosen remote runtime endpoint. Use synthetic data for this experiment. Match
the allowed processing Regions to the requirements of real data before adapting
the example.

The raw local report includes source text, references, account/resource IDs,
and SDK metadata. Keep `artifacts/` out of source control. The committed live
summary uses a small field allowlist.

## Recovery

- An account mismatch stops before mutations.
- A profile ARN from a different source Region stops before the KB query.
- Model `AccessDeniedException` is reported once, without automatic retries.
- IAM role propagation is allowed before the resource policy and KB calls.
- KB creation checkpoints its token. `resume-kb-creation` reuses it after an
  uncertain outcome and refuses to run once the KB ID is recorded.
- Creation refuses to overwrite a manifest, preventing accidental replay.
- Cleanup refuses resources with unexpected ownership or contents.
- A cleanup failure leaves `CLEANUP_INCOMPLETE` and the error in the manifest.
  Inspect and retry that same cleanup; do not delete the manifest.

References: [KB permissions](https://docs.aws.amazon.com/bedrock/latest/userguide/kb-permissions.html),
[S3 Vectors setup](https://docs.aws.amazon.com/bedrock/latest/userguide/knowledge-base-setup.html),
and [inference-profile IAM](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html).
