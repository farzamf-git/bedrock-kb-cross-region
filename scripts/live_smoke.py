#!/usr/bin/env python3
"""Create a synthetic KB, run the probe, and always attempt scoped cleanup."""

import argparse
import hashlib
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--region", default="ap-southeast-1")
    args = parser.parse_args()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = ROOT / "artifacts" / run_id
    output.mkdir(parents=True)
    manifest = output / "resources.json"
    common = ["--account", args.account]
    if args.profile:
        common += ["--profile", args.profile]
    lifecycle = [
        sys.executable,
        str(ROOT / "manage.py"),
        *common,
        "--region",
        args.region,
        "--manifest",
        str(manifest),
    ]
    probe_code = None
    cleanup_code = None
    try:
        subprocess.run([*lifecycle, "create"], check=True)
        state = json.loads(manifest.read_text())
        with (output / "probe-stdout.json").open("w") as stdout:
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "probe.py"),
                    *common,
                    "--kb-region",
                    args.region,
                    "--kb-id",
                    state["resources"]["kb_id"],
                    "--question",
                    "What is the verification code for Project Orchid?",
                    "--expected-text",
                    "ORCHID-7319",
                    "--test-foreign-arn",
                    "--output",
                    str(output / "results.json"),
                ],
                stdout=stdout,
                check=False,
            )
        probe_code = result.returncode
    finally:
        if manifest.exists():
            cleanup_code = subprocess.run([*lifecycle, "cleanup"], check=False).returncode
        print(f"Local artifacts: {output}", flush=True)
    if probe_code != 0 or cleanup_code != 0:
        return 1
    results = json.loads((output / "results.json").read_text())
    state = json.loads(manifest.read_text())
    summary = {
        "schema_version": 1,
        "tested_at_utc": results["tested_at_utc"],
        "evidence_type": "AWS provider-live synthetic smoke test",
        "kb_region": results["kb_region"],
        "remote_region": results["remote_region"],
        "inference_profile": "apac.amazon.nova-lite-v1:0",
        "remote_model": "amazon.nova-lite-v1:0",
        "status": results["status"],
        "expected_text_found": results["expected_text_found"],
        "native_citation_count": results["native_citation_count"],
        "retrieved_chunks": len(
            results["tests"]["retrieve_home_region"]["response"]["retrievalResults"]
        ),
        "ingestion_statistics": state["ingestion"]["statistics"],
        "operations": {
            name: {
                key: value for key, value in item.items() if key in {"status", "seconds", "code"}
            }
            for name, item in results["tests"].items()
            if name
            in {
                "retrieve_home_region",
                "retrieve_and_generate_profile",
                "retrieve_and_generate_foreign_arn",
                "retrieve_then_remote_converse",
            }
        },
        "cleanup_status": state["status"],
        "cleanup_resources": state["cleanup"],
        "code_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in ("manage.py", "probe.py", "sample.txt")
        },
        "limitations": [
            "One document and one question; not a performance or quality benchmark.",
            "The inference-profile request's actual processing Region was not observed.",
            "Parsing was not tested.",
        ],
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
