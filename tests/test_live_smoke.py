import json
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from scripts import live_smoke


@pytest.mark.parametrize("override", [None, "global.anthropic.claude-sonnet-4-5-20250929-v1:0"])
def test_runner_forwards_profile_and_records_probe_selection(monkeypatch, tmp_path, override):
    monkeypatch.setattr(live_smoke, "ROOT", tmp_path)
    for name in ("manage.py", "probe.py", "sample.txt"):
        (tmp_path / name).write_text("synthetic fixture\n")
    selected = override or "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[1].endswith("manage.py"):
            manifest = Path(command[command.index("--manifest") + 1])
            state = {
                "resources": {"kb_id": "ABCDEFGHIJ"},
                "ingestion": {"statistics": {"numberOfNewDocumentsIndexed": 1}},
                "status": "DELETED_AND_VERIFIED" if command[-1] == "cleanup" else "READY",
                "cleanup": {},
            }
            manifest.write_text(json.dumps(state))
        else:
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "tested_at_utc": "2026-10-30T00:00:00+00:00",
                        "kb_region": "ap-southeast-1",
                        "remote_region": "ap-southeast-2",
                        "inference_profile": selected,
                        "remote_model": "amazon.nova-lite-v1:0",
                        "status": "BOTH_PATHS_PASSED",
                        "expected_text_found": {"native": True, "split": True},
                        "native_citation_count": 1,
                        "tests": {
                            "retrieve_home_region": {
                                "status": "SUCCESS",
                                "response": {"retrievalResults": [{}]},
                            }
                        },
                    }
                )
            )
        return CompletedProcess(command, 0)

    monkeypatch.setattr(live_smoke.subprocess, "run", run)
    args = ["--account", "123456789012"]
    if override:
        args += ["--inference-profile", override]
    assert live_smoke.main(args) == 0
    query = commands[1]
    if override:
        assert query[query.index("--inference-profile") + 1] == override
    else:
        assert "--inference-profile" not in query
    assert commands[-1][-1] == "cleanup"
    assert all("--inference-profile" not in command for command in (commands[0], commands[-1]))
    summary = json.loads(next((tmp_path / "artifacts").glob("*/summary.json")).read_text())
    assert summary["inference_profile"] == selected
    assert summary["remote_model"] == "amazon.nova-lite-v1:0"
