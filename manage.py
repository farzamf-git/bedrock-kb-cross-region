#!/usr/bin/env python3
"""Create or remove this experiment's Singapore KB and its dedicated resources."""

import argparse
import json
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parent


@dataclass(frozen=True)
class Settings:
    account: str
    profile: str | None = None
    region: str = "ap-southeast-1"
    manifest: Path = ROOT / "artifacts" / "resources.json"
    source: Path = ROOT / "sample.txt"


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


EMBEDDING = "cohere.embed-english-v3"
POLICY_NAME = "ThrowawayKnowledgeBase"
CONFIG = Config(connect_timeout=10, read_timeout=120, retries={"total_max_attempts": 1})


def save(settings, state):
    settings.manifest.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings.manifest.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, default=str) + "\n")
    temporary.replace(settings.manifest)


def log(message):
    print(message, flush=True)


def absent(call):
    try:
        call()
        return False
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {
            "ResourceNotFoundException",
            "NotFoundException",
            "NoSuchEntity",
            "NoSuchBucket",
            "404",
        }:
            return True
        raise


def wait_for(get, ready, failed, timeout=300):
    deadline = time.monotonic() + timeout
    while True:
        value = get()
        status = value["status"]
        log(f"  status={status}")
        if status in ready:
            return value
        if status in failed:
            raise RuntimeError(json.dumps(value, default=str))
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out at status {status}")
        time.sleep(10)


def create(clients, settings):
    if settings.manifest.exists():
        raise RuntimeError("Manifest already exists; inspect it before creating another test.")
    content = settings.source.read_bytes()
    require(content and len(content) < 1024, "Use a nonempty synthetic text fixture under 1 KB")
    suffix = uuid.uuid4().hex[:12]
    name = f"kb-cris-{settings.account}-{suffix}"
    tags = {"Purpose": "throwaway-kb-cross-region-test", "RunId": suffix}
    state = {
        "account": settings.account,
        "region": settings.region,
        "profile": settings.profile,
        "run_id": suffix,
        "tags": tags,
        "name": name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "resources": {},
        "cleanup": {},
    }
    save(settings, state)
    s3, vectors, iam, agent = [clients[x] for x in ("s3", "s3vectors", "iam", "bedrock-agent")]
    resources = state["resources"]
    try:
        log("Creating the private S3 source bucket and uploading sample.txt.")
        s3.create_bucket(
            Bucket=name, CreateBucketConfiguration={"LocationConstraint": settings.region}
        )
        resources["source_bucket"] = name
        save(settings, state)
        s3.put_bucket_tagging(
            Bucket=name,
            ExpectedBucketOwner=settings.account,
            Tagging={"TagSet": [{"Key": k, "Value": v} for k, v in tags.items()]},
        )
        s3.put_public_access_block(
            Bucket=name,
            ExpectedBucketOwner=settings.account,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        s3.put_bucket_encryption(
            Bucket=name,
            ExpectedBucketOwner=settings.account,
            ServerSideEncryptionConfiguration={
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
            },
        )
        s3.put_object(
            Bucket=name,
            Key="test/sample.txt",
            Body=content,
            ContentType="text/plain",
            ServerSideEncryption="AES256",
            ExpectedBucketOwner=settings.account,
        )
        s3.head_object(Bucket=name, Key="test/sample.txt", ExpectedBucketOwner=settings.account)

        log("Creating a 1024-dimensional S3 vector index in Singapore.")
        vector_bucket = vectors.create_vector_bucket(
            vectorBucketName=name, encryptionConfiguration={"sseType": "AES256"}, tags=tags
        )
        resources["vector_bucket_arn"] = vector_bucket["vectorBucketArn"]
        save(settings, state)
        index = vectors.create_index(
            vectorBucketArn=resources["vector_bucket_arn"],
            indexName="test-index",
            dataType="float32",
            dimension=1024,
            distanceMetric="cosine",
            metadataConfiguration={
                "nonFilterableMetadataKeys": ["AMAZON_BEDROCK_TEXT", "AMAZON_BEDROCK_METADATA"]
            },
            tags=tags,
        )
        resources["index_arn"] = index["indexArn"]
        save(settings, state)
        vectors.get_index(indexArn=resources["index_arn"])

        log("Creating a scoped Bedrock service role.")
        role_name = f"AmazonBedrockExecutionRoleForKB-cris-{suffix}"
        trust = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": "bedrock.amazonaws.com"},
                    "Action": "sts:AssumeRole",
                    "Condition": {
                        "StringEquals": {"aws:SourceAccount": settings.account},
                        "ArnLike": {
                            "aws:SourceArn": f"arn:aws:bedrock:{settings.region}:{settings.account}:knowledge-base/*"
                        },
                    },
                }
            ],
        }
        role = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Temporary role for cross-Region KB experiment",
            Tags=[{"Key": k, "Value": v} for k, v in tags.items()],
        )
        resources["role_name"] = role_name
        resources["role_arn"] = role["Role"]["Arn"]
        save(settings, state)
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["bedrock:InvokeModel"],
                    "Resource": f"arn:aws:bedrock:{settings.region}::foundation-model/{EMBEDDING}",
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": f"arn:aws:s3:::{name}",
                    "Condition": {
                        "StringLike": {"s3:prefix": ["test/*"]},
                        "StringEquals": {"aws:ResourceAccount": settings.account},
                    },
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": f"arn:aws:s3:::{name}/test/*",
                    "Condition": {"StringEquals": {"aws:ResourceAccount": settings.account}},
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3vectors:PutVectors",
                        "s3vectors:GetVectors",
                        "s3vectors:DeleteVectors",
                        "s3vectors:QueryVectors",
                        "s3vectors:GetIndex",
                    ],
                    "Resource": resources["index_arn"],
                },
            ],
        }
        iam.put_role_policy(
            RoleName=role_name, PolicyName=POLICY_NAME, PolicyDocument=json.dumps(policy)
        )
        time.sleep(15)
        vectors.put_vector_bucket_policy(
            vectorBucketArn=resources["vector_bucket_arn"],
            policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {"AWS": resources["role_arn"]},
                            "Action": [
                                "s3vectors:PutVectors",
                                "s3vectors:GetVectors",
                                "s3vectors:DeleteVectors",
                                "s3vectors:QueryVectors",
                                "s3vectors:GetIndex",
                            ],
                            "Resource": resources["index_arn"],
                        }
                    ],
                }
            ),
        )
        finish_create(clients, settings, state)
    except Exception as exc:
        state["status"] = "CREATE_FAILED"
        state["error"] = str(exc)
        save(settings, state)
        raise


def finish_create(clients, settings, state):
    require(
        state["account"] == settings.account and state["region"] == settings.region,
        "Manifest account or Region does not match",
    )
    resources = state["resources"]
    require("kb_id" not in resources, "KB creation already completed; inspect the manifest")
    name, tags = state["name"], state["tags"]
    role_name = resources["role_name"]
    agent, iam, vectors = [clients[x] for x in ("bedrock-agent", "iam", "s3vectors")]
    require(
        vectors.list_tags_for_resource(resourceArn=resources["index_arn"])["tags"].get("RunId")
        == state["run_id"],
        "Index ownership mismatch",
    )
    role = iam.get_role(RoleName=role_name)["Role"]
    require(
        {tag["Key"]: tag["Value"] for tag in role["Tags"]}.get("RunId") == state["run_id"],
        "Role ownership mismatch",
    )
    trust = role["AssumeRolePolicyDocument"]
    try:
        log("Creating the Knowledge Base with regional Cohere embeddings.")
        # Persist before the request, so a timeout can be resumed with the same token.
        token = state.setdefault("kb_client_token", str(uuid.uuid4()))
        save(settings, state)
        for attempt in range(3):
            try:
                kb = agent.create_knowledge_base(
                    clientToken=token,
                    name=name,
                    roleArn=resources["role_arn"],
                    tags=tags,
                    description="Throwaway Singapore KB cross-Region generation experiment",
                    knowledgeBaseConfiguration={
                        "type": "VECTOR",
                        "vectorKnowledgeBaseConfiguration": {
                            "embeddingModelArn": f"arn:aws:bedrock:{settings.region}::foundation-model/{EMBEDDING}",
                        },
                    },
                    storageConfiguration={
                        "type": "S3_VECTORS",
                        "s3VectorsConfiguration": {"indexArn": resources["index_arn"]},
                    },
                )
                break
            except ClientError as exc:
                message = exc.response["Error"]["Message"].lower()
                if attempt == 2 or not any(
                    part in message for part in ("assume", "role", "permission", "access denied")
                ):
                    raise
                log(f"IAM propagation wait: attempt {attempt + 1}.")
                time.sleep(15 * (attempt + 1))
        resources["kb_id"] = kb["knowledgeBase"]["knowledgeBaseId"]
        resources["kb_arn"] = kb["knowledgeBase"]["knowledgeBaseArn"]
        save(settings, state)
        wait_for(
            lambda: agent.get_knowledge_base(knowledgeBaseId=resources["kb_id"])["knowledgeBase"],
            {"ACTIVE"},
            {"FAILED", "DELETING"},
        )
        trust["Statement"][0]["Condition"]["ArnLike"]["aws:SourceArn"] = resources["kb_arn"]
        iam.update_assume_role_policy(RoleName=role_name, PolicyDocument=json.dumps(trust))

        log("Creating the S3 data source with NONE chunking for the tiny text file.")
        data_source = agent.create_data_source(
            knowledgeBaseId=resources["kb_id"],
            name="synthetic-test-document",
            dataDeletionPolicy="DELETE",
            dataSourceConfiguration={
                "type": "S3",
                "s3Configuration": {
                    "bucketArn": f"arn:aws:s3:::{name}",
                    "inclusionPrefixes": ["test/"],
                },
            },
            vectorIngestionConfiguration={"chunkingConfiguration": {"chunkingStrategy": "NONE"}},
        )
        resources["data_source_id"] = data_source["dataSource"]["dataSourceId"]
        save(settings, state)
        log("Starting ingestion and waiting for its completion.")
        ingestion = agent.start_ingestion_job(
            knowledgeBaseId=resources["kb_id"], dataSourceId=resources["data_source_id"]
        )
        resources["ingestion_job_id"] = ingestion["ingestionJob"]["ingestionJobId"]
        save(settings, state)
        completed = wait_for(
            lambda: agent.get_ingestion_job(
                knowledgeBaseId=resources["kb_id"],
                dataSourceId=resources["data_source_id"],
                ingestionJobId=resources["ingestion_job_id"],
            )["ingestionJob"],
            {"COMPLETE"},
            {"FAILED", "STOPPED"},
            timeout=600,
        )
        state["ingestion"] = completed
        require(
            completed["statistics"]["numberOfDocumentsFailed"] == 0,
            "Ingestion had document failures",
        )
        require(
            completed["statistics"]["numberOfNewDocumentsIndexed"] == 1,
            "Expected one indexed document",
        )
        state["status"] = "INGESTED"
        state.pop("error", None)
        save(settings, state)
        log(f"KB ready: {resources['kb_id']}; ingestion: {completed['statistics']}")
    except Exception as exc:
        state["status"] = "CREATE_FAILED"
        state["error"] = str(exc)
        save(settings, state)
        raise


def cleanup(clients, settings):
    state = json.loads(settings.manifest.read_text())
    require(
        state["account"] == settings.account and state["region"] == settings.region,
        "Manifest account or Region does not match",
    )
    resources = state["resources"]
    s3, vectors, iam, agent = [clients[x] for x in ("s3", "s3vectors", "iam", "bedrock-agent")]
    failures = {}

    def remove(label, verify_owned, delete, verify_absent):
        try:
            if verify_absent():
                state["cleanup"][label] = "ABSENT"
            else:
                verify_owned()
                delete()
                deadline = time.monotonic() + 180
                while not verify_absent():
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"{label} deletion not yet confirmed")
                    time.sleep(5)
                state["cleanup"][label] = "DELETED_AND_VERIFIED"
            log(f"{label}: {state['cleanup'][label]}")
        except Exception as exc:
            failures[label] = str(exc)
            log(f"{label}: cleanup failed: {exc}")
        save(settings, state)

    def check_tags(actual):
        require(actual.get("RunId") == state["run_id"], "Resource ownership tag mismatch")

    if "kb_id" in resources:

        def own_kb():
            check_tags(agent.list_tags_for_resource(resourceArn=resources["kb_arn"])["tags"])

        remove(
            "knowledge_base",
            own_kb,
            lambda: agent.delete_knowledge_base(knowledgeBaseId=resources["kb_id"]),
            lambda: absent(lambda: agent.get_knowledge_base(knowledgeBaseId=resources["kb_id"])),
        )
        if "data_source_id" in resources and not failures:
            if absent(
                lambda: agent.get_data_source(
                    knowledgeBaseId=resources["kb_id"], dataSourceId=resources["data_source_id"]
                )
            ):
                state["cleanup"]["data_source"] = "ABSENT_VERIFIED"
                save(settings, state)
                log("data_source: ABSENT_VERIFIED")
            else:
                failures["data_source"] = "Data source is still present after KB deletion"
    if "index_arn" in resources and not failures:
        remove(
            "vector_index",
            lambda: check_tags(
                vectors.list_tags_for_resource(resourceArn=resources["index_arn"])["tags"]
            ),
            lambda: vectors.delete_index(indexArn=resources["index_arn"]),
            lambda: absent(lambda: vectors.get_index(indexArn=resources["index_arn"])),
        )
    if "vector_bucket_arn" in resources and not failures:
        remove(
            "vector_bucket",
            lambda: check_tags(
                vectors.list_tags_for_resource(resourceArn=resources["vector_bucket_arn"])["tags"]
            ),
            lambda: vectors.delete_vector_bucket(vectorBucketArn=resources["vector_bucket_arn"]),
            lambda: absent(
                lambda: vectors.get_vector_bucket(vectorBucketArn=resources["vector_bucket_arn"])
            ),
        )
    if "source_bucket" in resources and not failures:
        bucket = resources["source_bucket"]

        def own_source():
            check_tags(
                {
                    tag["Key"]: tag["Value"]
                    for tag in s3.get_bucket_tagging(
                        Bucket=bucket, ExpectedBucketOwner=settings.account
                    )["TagSet"]
                }
            )
            objects = s3.list_objects_v2(Bucket=bucket, ExpectedBucketOwner=settings.account)
            require(
                {obj["Key"] for obj in objects.get("Contents", [])} <= {"test/sample.txt"},
                "Unexpected object in source bucket",
            )
            require(not objects.get("IsTruncated"), "Source bucket listing was truncated")

        def delete_source():
            s3.delete_object(
                Bucket=bucket, Key="test/sample.txt", ExpectedBucketOwner=settings.account
            )
            s3.delete_bucket(Bucket=bucket, ExpectedBucketOwner=settings.account)

        remove(
            "source_bucket",
            own_source,
            delete_source,
            lambda: absent(
                lambda: s3.head_bucket(Bucket=bucket, ExpectedBucketOwner=settings.account)
            ),
        )
    if "role_name" in resources and not failures:
        role = resources["role_name"]

        def own_role():
            check_tags(
                {tag["Key"]: tag["Value"] for tag in iam.list_role_tags(RoleName=role)["Tags"]}
            )
            require(
                iam.list_attached_role_policies(RoleName=role)["AttachedPolicies"] == [],
                "Unexpected managed role policy",
            )
            require(
                set(iam.list_role_policies(RoleName=role)["PolicyNames"]) <= {POLICY_NAME},
                "Unexpected inline role policy",
            )

        def delete_role():
            if POLICY_NAME in iam.list_role_policies(RoleName=role)["PolicyNames"]:
                iam.delete_role_policy(RoleName=role, PolicyName=POLICY_NAME)
            iam.delete_role(RoleName=role)

        remove(
            "service_role",
            own_role,
            delete_role,
            lambda: absent(lambda: iam.get_role(RoleName=role)),
        )
    state["cleanup_errors"] = failures
    state["status"] = "CLEANUP_INCOMPLETE" if failures else "DELETED_AND_VERIFIED"
    save(settings, state)
    if failures:
        raise RuntimeError(json.dumps(failures))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "resume-kb-creation", "cleanup"])
    parser.add_argument(
        "--account", required=True, help="Expected 12-digit AWS account; checked with STS"
    )
    parser.add_argument("--profile", help="Optional local AWS credential profile")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--manifest", type=Path, default=Settings.manifest)
    parser.add_argument("--source", type=Path, default=Settings.source)
    args = parser.parse_args(argv)
    if len(args.account) != 12 or not args.account.isdecimal():
        parser.error("--account must contain 12 digits")
    settings = Settings(
        account=args.account,
        profile=args.profile,
        region=args.region,
        manifest=args.manifest,
        source=args.source,
    )
    session = boto3.Session(profile_name=settings.profile)
    clients = {
        service: session.client(service, region_name=settings.region, config=CONFIG)
        for service in ("sts", "s3", "s3vectors", "iam", "bedrock-agent")
    }
    identity = clients["sts"].get_caller_identity()
    require(identity["Account"] == settings.account, "Unexpected account; stopping before mutation")
    if args.action == "resume-kb-creation":
        finish_create(clients, settings, json.loads(settings.manifest.read_text()))
    else:
        (create if args.action == "create" else cleanup)(clients, settings)
    return 0


if __name__ == "__main__":
    main()
