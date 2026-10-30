# Open-source readiness review

Reviewed on 30 October 2026. The source is technically portable, with one
publication prerequisite still unresolved: **the repository has no license**.
Choose and add a license before presenting this as an open-source project.

## Credentials and repository contents

- Gitleaks scanned every reachable commit and found no secrets.
- Source and history checks found no personal AWS account IDs, AWS profile names,
  personal filesystem paths, internal repository addresses, or embedded URL
  authentication.
- AWS authentication uses boto3's credential chain; no credentials are shipped.
  Each person supplies their own profile or runtime credentials.
- Raw SDK responses, resource manifests, and teardown receipts are ignored under
  `artifacts/`. They may include account IDs, resource ARNs, retrieved text, and
  SDK metadata. Keep them local.
- `.env` files, local AWS configuration, common credential files, and private-key
  files are ignored. Ignore rules help prevent accidental commits; they are not
  a substitute for reviewing staged changes.
- The committed live evidence is a small sanitized summary. The sample document
  contains synthetic data.
- Git commit authorship remains visible in history.

## Portability and scope

The scripts derive source and default artifact locations from their own file
locations. They do not depend on a developer's home directory or working
directory. `manage.py` accepts `--source` and `--manifest` overrides.

The AWS account and credential profile are supplied at runtime. Singapore,
Sydney, Cohere Embed v3, and Nova Lite for the split path are deliberate
experiment defaults. The generation inference profile is configurable through
`--inference-profile` in both query entrypoints.

The frozen dependency lock uses public PyPI package sources. Local validation
uses `make check`; it does not require AWS credentials or create resources.
Live provisioning and inference require the permissions and model access listed
in [AWS prerequisites](AWS_PREREQUISITES.md), and incur AWS charges.

Validation passed in a freshly exported checkout at a different filesystem
location, with a new virtual environment and empty AWS configuration files:
frozen dependency installation, Ruff lint and format checks, and all 30 offline
tests. All three script entrypoints also displayed help when launched from
outside the checkout. This relocation check used Python 3.13 on macOS; it does
not establish execution on every supported Python version or operating system.

## Evidence limits

The earlier Nova Lite paths were tested against live AWS. The current global
Claude Haiku 4.5 profile was resolved as active in Singapore, but generation with
that profile has only received offline regression testing. The saved Nova Lite
results do not establish Haiku generation success.

Secret scans establish that no secrets were detected in the reviewed source
and history. They do not certify every possible disclosure pattern or decide
copyright ownership and publication rights.
