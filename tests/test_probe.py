import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from botocore.session import get_session
from botocore.validate import validate_parameters

import probe

ACCOUNT = "123456789012"
PROFILE_ARN = (
    f"arn:aws:bedrock:ap-southeast-1:{ACCOUNT}:inference-profile/apac.amazon.nova-lite-v1:0"
)
CHUNKS = [{"content": {"text": "The verification code is ORCHID-7319."}, "score": 0.93}]


@pytest.fixture
def aws(monkeypatch):
    clients = {
        name: Mock()
        for name in ("sts", "bedrock", "bedrock-agent", "bedrock-agent-runtime", "bedrock-runtime")
    }
    clients["sts"].get_caller_identity.return_value = {"Account": ACCOUNT}
    clients["bedrock"].get_inference_profile.return_value = {
        "inferenceProfileArn": PROFILE_ARN,
        "models": [
            {"modelArn": "arn:aws:bedrock:ap-southeast-2::foundation-model/amazon.nova-lite-v1:0"}
        ],
    }
    clients["bedrock-agent"].get_knowledge_base.return_value = {"knowledgeBase": {}}
    clients["bedrock-agent-runtime"].retrieve.return_value = {"retrievalResults": CHUNKS}
    clients["bedrock-agent-runtime"].retrieve_and_generate.return_value = {
        "output": {"text": "ORCHID-7319"},
        "citations": [{}],
    }
    clients["bedrock-runtime"].converse.return_value = {
        "output": {"message": {"content": [{"text": "ORCHID-7319 [1]"}]}}
    }
    session = Mock()
    session.client.side_effect = lambda service, **kwargs: clients[service]
    monkeypatch.setattr(probe.boto3, "Session", Mock(return_value=session))
    return clients, session


def run(tmp_path, *extra):
    output = tmp_path / "nested" / "result.json"
    code = probe.main(
        [
            "--account",
            ACCOUNT,
            "--kb-id",
            "ABCDEFGHIJ",
            "--question",
            "What is the verification code?",
            "--expected-text",
            "ORCHID-7319",
            "--output",
            str(output),
            *extra,
        ]
    )
    return code, json.loads(output.read_text())


def test_requests_match_current_sdk_contract():
    sdk = get_session()
    validate_parameters(
        probe.rag_request("ABCDEFGHIJ", PROFILE_ARN, "Question"),
        sdk.get_service_model("bedrock-agent-runtime")
        .operation_model("RetrieveAndGenerate")
        .input_shape,
    )
    validate_parameters(
        probe.converse_request("amazon.nova-lite-v1:0", CHUNKS, "Question"),
        sdk.get_service_model("bedrock-runtime").operation_model("Converse").input_shape,
    )


def test_split_preserves_source_labels_and_bounds_output():
    request = probe.converse_request(
        "model", CHUNKS + [{"content": {"text": "Second source"}}], "Q"
    )
    text = request["messages"][0]["content"][0]["text"]
    assert "[1] The verification code is ORCHID-7319." in text
    assert "[2] Second source" in text
    assert request["inferenceConfig"]["maxTokens"] == 256


def test_nontext_chunks_cannot_silently_pass():
    with pytest.raises(ValueError):
        probe.converse_request("model", [{"content": {"byteContent": "AA=="}}], "Q")


def test_end_to_end_paths_use_correct_regions_and_known_answer(aws, tmp_path):
    clients, session = aws
    code, report = run(tmp_path)
    assert code == 0
    assert report["status"] == "BOTH_PATHS_PASSED"
    assert all(report["expected_text_found"].values())
    native = clients["bedrock-agent-runtime"].retrieve_and_generate.call_args.kwargs
    assert (
        native["retrieveAndGenerateConfiguration"]["knowledgeBaseConfiguration"]["modelArn"]
        == PROFILE_ARN
    )
    region_by_service = {
        call.args[0]: call.kwargs["region_name"] for call in session.client.call_args_list
    }
    assert region_by_service["bedrock-agent-runtime"] == "ap-southeast-1"
    assert region_by_service["bedrock-runtime"] == "ap-southeast-2"
    assert all(
        call.kwargs["config"].retries["total_max_attempts"] == 1
        for call in session.client.call_args_list
    )


def test_foreign_arn_rejection_is_reported_without_failing_supported_paths(aws, tmp_path):
    clients, _ = aws
    runtime = clients["bedrock-agent-runtime"]
    good = runtime.retrieve_and_generate.return_value
    runtime.retrieve_and_generate.side_effect = [
        good,
        ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Invalid service Region"}},
            "RetrieveAndGenerate",
        ),
    ]
    code, report = run(tmp_path, "--test-foreign-arn")
    assert code == 0
    assert report["tests"]["retrieve_and_generate_foreign_arn"]["code"] == "ValidationException"
    assert runtime.retrieve_and_generate.call_count == 2


def test_api_success_with_wrong_answer_fails(aws, tmp_path):
    clients, _ = aws
    clients["bedrock-runtime"].converse.return_value["output"]["message"]["content"] = [
        {"text": "I don't know"}
    ]
    code, report = run(tmp_path)
    assert code == 1
    assert not report["expected_text_found"]["retrieve_then_remote_converse"]


def test_empty_retrieval_stops_generation(aws, tmp_path):
    clients, _ = aws
    clients["bedrock-agent-runtime"].retrieve.return_value = {"retrievalResults": []}
    code, report = run(tmp_path)
    assert code == 1
    assert report["status"] == "NO_RETRIEVAL_RESULTS"
    clients["bedrock-agent-runtime"].retrieve_and_generate.assert_not_called()
    clients["bedrock-runtime"].converse.assert_not_called()


def test_profile_from_wrong_region_stops_before_kb_query(aws, tmp_path):
    clients, _ = aws
    clients["bedrock"].get_inference_profile.return_value["inferenceProfileArn"] = (
        PROFILE_ARN.replace("ap-southeast-1", "us-east-1")
    )
    code, report = run(tmp_path)
    assert code == 1
    assert report["status"] == "PROFILE_ARN_MUST_USE_KB_REGION"
    clients["bedrock-agent-runtime"].retrieve.assert_not_called()


def test_account_mismatch_stops_before_profile_and_generation(aws, tmp_path):
    clients, _ = aws
    clients["sts"].get_caller_identity.return_value = {"Account": "999999999999"}
    code, report = run(tmp_path)
    assert code == 1
    assert report["status"] == "ACCOUNT_MISMATCH"
    clients["bedrock"].get_inference_profile.assert_not_called()
    clients["bedrock-agent-runtime"].retrieve.assert_not_called()


def test_generation_access_denied_is_not_retried_or_reported_as_pass(aws, tmp_path):
    clients, _ = aws
    clients["bedrock-agent-runtime"].retrieve_and_generate.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "Model permission denied"}},
        "RetrieveAndGenerate",
    )
    code, report = run(tmp_path)
    assert code == 1
    assert report["tests"]["retrieve_and_generate_profile"]["code"] == "AccessDeniedException"
    assert clients["bedrock-agent-runtime"].retrieve_and_generate.call_count == 1


def test_discovery_does_not_generate(aws, tmp_path):
    clients, _ = aws
    clients["bedrock-agent"].get_paginator.return_value.paginate.return_value = [
        {"knowledgeBaseSummaries": [{"knowledgeBaseId": "ABCDEFGHIJ"}]},
        {"knowledgeBaseSummaries": [{"knowledgeBaseId": "KLMNOPQRST"}]},
    ]
    output = tmp_path / "discovery.json"
    assert probe.main(["--output", str(output)]) == 0
    report = json.loads(output.read_text())
    assert report["status"] == "DISCOVERY_ONLY"
    assert len(report["tests"]["knowledge_bases"]["response"]["knowledgeBases"]) == 2
    clients["bedrock-agent-runtime"].retrieve.assert_not_called()


def test_credentials_provider_error_text_is_not_logged(aws, tmp_path):
    clients, _ = aws
    clients["sts"].get_caller_identity.side_effect = RuntimeError("private-provider-output")
    code, report = run(tmp_path)
    assert code == 1
    assert "private-provider-output" not in json.dumps(report)
