import json
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, ReadTimeoutError

import manage

ACCOUNT = "123456789012"


def error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "TestOperation")


@pytest.fixture
def setup(tmp_path):
    settings = manage.Settings(account=ACCOUNT, manifest=tmp_path / "artifacts" / "resources.json")
    state = {
        "account": ACCOUNT,
        "region": settings.region,
        "profile": None,
        "name": "temporary-fixture",
        "run_id": "owned-run",
        "tags": {"RunId": "owned-run"},
        "resources": {},
        "cleanup": {},
    }
    clients = {service: Mock() for service in ("sts", "s3", "s3vectors", "iam", "bedrock-agent")}
    return settings, state, clients


def test_account_mismatch_prevents_every_mutation(monkeypatch, setup):
    settings, _, clients = setup
    clients["sts"].get_caller_identity.return_value = {"Account": "999999999999"}
    session = Mock()
    session.client.side_effect = lambda service, **kwargs: clients[service]
    monkeypatch.setattr(manage.boto3, "Session", Mock(return_value=session))
    with pytest.raises(RuntimeError, match="Unexpected account"):
        manage.main(["create", "--account", ACCOUNT, "--manifest", str(settings.manifest)])
    clients["s3"].create_bucket.assert_not_called()
    clients["iam"].create_role.assert_not_called()


def test_manifest_account_mismatch_blocks_cleanup_before_aws_calls(setup):
    settings, state, clients = setup
    state["account"] = "999999999999"
    state["resources"]["source_bucket"] = "foreign-bucket"
    manage.save(settings, state)
    with pytest.raises(RuntimeError, match="Manifest account"):
        manage.cleanup(clients, settings)
    clients["s3"].head_bucket.assert_not_called()


@pytest.mark.parametrize("bad_case", ["wrong_tags", "unexpected_object", "truncated_listing"])
def test_source_bucket_cleanup_refuses_foreign_changes(setup, bad_case):
    settings, state, clients = setup
    state["resources"]["source_bucket"] = "temporary-fixture"
    manage.save(settings, state)
    s3 = clients["s3"]
    s3.get_bucket_tagging.return_value = {
        "TagSet": [
            {"Key": "RunId", "Value": "foreign-run" if bad_case == "wrong_tags" else "owned-run"}
        ]
    }
    s3.list_objects_v2.return_value = {
        "Contents": [
            {"Key": "foreign-file.txt" if bad_case == "unexpected_object" else "test/sample.txt"}
        ],
        "IsTruncated": bad_case == "truncated_listing",
    }
    with pytest.raises(RuntimeError):
        manage.cleanup(clients, settings)
    s3.delete_object.assert_not_called()
    s3.delete_bucket.assert_not_called()
    assert json.loads(settings.manifest.read_text())["status"] == "CLEANUP_INCOMPLETE"


def test_role_cleanup_refuses_unrelated_managed_policy(setup):
    settings, state, clients = setup
    state["resources"]["role_name"] = "temporary-role"
    manage.save(settings, state)
    iam = clients["iam"]
    iam.list_role_tags.return_value = {"Tags": [{"Key": "RunId", "Value": "owned-run"}]}
    iam.list_attached_role_policies.return_value = {
        "AttachedPolicies": [{"PolicyName": "ForeignPolicy"}]
    }
    with pytest.raises(RuntimeError):
        manage.cleanup(clients, settings)
    iam.delete_role.assert_not_called()
    iam.delete_role_policy.assert_not_called()


def test_vector_index_mismatch_blocks_later_bucket_deletion(setup):
    settings, state, clients = setup
    state["resources"].update({"index_arn": "index", "vector_bucket_arn": "bucket"})
    manage.save(settings, state)
    clients["s3vectors"].list_tags_for_resource.return_value = {"tags": {"RunId": "foreign-run"}}
    with pytest.raises(RuntimeError):
        manage.cleanup(clients, settings)
    clients["s3vectors"].delete_index.assert_not_called()
    clients["s3vectors"].delete_vector_bucket.assert_not_called()


def test_access_denied_is_not_mistaken_for_absence():
    with pytest.raises(ClientError):
        manage.absent(Mock(side_effect=error("AccessDenied")))


def test_already_deleted_resources_make_cleanup_idempotent(setup):
    settings, state, clients = setup
    state["resources"]["role_name"] = "temporary-role"
    manage.save(settings, state)
    clients["iam"].get_role.side_effect = error("NoSuchEntity")
    manage.cleanup(clients, settings)
    manage.cleanup(clients, settings)
    clients["iam"].delete_role.assert_not_called()
    assert json.loads(settings.manifest.read_text())["cleanup"]["service_role"] == "ABSENT"


def test_source_cleanup_verifies_absence_after_delete(setup):
    settings, state, clients = setup
    state["resources"]["source_bucket"] = "temporary-fixture"
    manage.save(settings, state)
    s3 = clients["s3"]
    s3.head_bucket.side_effect = [{}, error("NoSuchBucket")]
    s3.get_bucket_tagging.return_value = {"TagSet": [{"Key": "RunId", "Value": "owned-run"}]}
    s3.list_objects_v2.return_value = {"Contents": [{"Key": "test/sample.txt"}]}
    manage.cleanup(clients, settings)
    s3.delete_bucket.assert_called_once_with(
        Bucket="temporary-fixture", ExpectedBucketOwner=ACCOUNT
    )
    assert (
        json.loads(settings.manifest.read_text())["cleanup"]["source_bucket"]
        == "DELETED_AND_VERIFIED"
    )


def prepare_creation(state, clients):
    state["resources"].update(
        {
            "index_arn": "arn:aws:s3vectors:ap-southeast-1:123456789012:bucket/test/index/test",
            "role_name": "temporary-role",
            "role_arn": "arn:aws:iam::123456789012:role/temporary-role",
        }
    )
    clients["s3vectors"].list_tags_for_resource.return_value = {"tags": state["tags"]}
    clients["iam"].get_role.return_value = {
        "Role": {
            "Tags": [{"Key": "RunId", "Value": state["run_id"]}],
            "AssumeRolePolicyDocument": {
                "Statement": [{"Condition": {"ArnLike": {"aws:SourceArn": "*"}}}]
            },
        }
    }


def test_kb_timeout_preserves_idempotency_token_for_resume(setup):
    settings, state, clients = setup
    prepare_creation(state, clients)
    agent = clients["bedrock-agent"]
    agent.create_knowledge_base.side_effect = ReadTimeoutError(endpoint_url="https://example.test")
    with pytest.raises(ReadTimeoutError):
        manage.finish_create(clients, settings, state)
    first_token = agent.create_knowledge_base.call_args.kwargs["clientToken"]
    recovered = json.loads(settings.manifest.read_text())
    assert recovered["kb_client_token"] == first_token
    with pytest.raises(ReadTimeoutError):
        manage.finish_create(clients, settings, recovered)
    assert agent.create_knowledge_base.call_args.kwargs["clientToken"] == first_token
    request = agent.create_knowledge_base.call_args.kwargs
    assert (
        "embeddingModelConfiguration"
        not in request["knowledgeBaseConfiguration"]["vectorKnowledgeBaseConfiguration"]
    )
    assert request["storageConfiguration"]["s3VectorsConfiguration"].keys() == {"indexArn"}


def test_failed_ingestion_is_never_reported_as_ready(setup):
    settings, state, clients = setup
    prepare_creation(state, clients)
    agent = clients["bedrock-agent"]
    agent.create_knowledge_base.return_value = {
        "knowledgeBase": {
            "knowledgeBaseId": "ABCDEFGHIJ",
            "knowledgeBaseArn": "arn:aws:bedrock:ap-southeast-1:123456789012:knowledge-base/ABCDEFGHIJ",
        }
    }
    agent.get_knowledge_base.return_value = {"knowledgeBase": {"status": "ACTIVE"}}
    agent.create_data_source.return_value = {"dataSource": {"dataSourceId": "SOURCE1234"}}
    agent.start_ingestion_job.return_value = {"ingestionJob": {"ingestionJobId": "JOB1234567"}}
    agent.get_ingestion_job.return_value = {
        "ingestionJob": {
            "status": "COMPLETE",
            "statistics": {"numberOfDocumentsFailed": 1, "numberOfNewDocumentsIndexed": 0},
        }
    }
    with pytest.raises(RuntimeError, match="document failures"):
        manage.finish_create(clients, settings, state)
    assert json.loads(settings.manifest.read_text())["status"] == "CREATE_FAILED"


def test_existing_manifest_prevents_duplicate_resources(setup):
    settings, state, clients = setup
    manage.save(settings, state)
    with pytest.raises(RuntimeError, match="Manifest already exists"):
        manage.create(clients, settings)
    clients["s3"].create_bucket.assert_not_called()


def test_invalid_fixture_is_rejected_before_creating_resources(setup, tmp_path):
    settings, _, clients = setup
    source = tmp_path / "empty.txt"
    source.write_text("")
    settings = manage.Settings(account=ACCOUNT, manifest=settings.manifest, source=source)
    with pytest.raises(RuntimeError, match="nonempty"):
        manage.create(clients, settings)
    clients["s3"].create_bucket.assert_not_called()
    assert not settings.manifest.exists()
