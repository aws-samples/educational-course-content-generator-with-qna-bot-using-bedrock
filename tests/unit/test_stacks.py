# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Synthesis tests for the CDK stacks.

The stacks read project_config.json and asset paths relative to the current
directory, so they are synthesized from the repository root. Classic mode is
synthesized from a temporary directory that holds a patched copy of
project_config.json and links to the asset directories, so that the
configuration of the repository stays untouched. The feature flags of cdk.json
are applied, as `cdk synth` and `cdk deploy` apply them, so that the tests see
the templates that are deployed. Asset bundling is disabled through the
``aws:cdk:bundling-stacks`` context so that no Docker is needed to synthesize.
"""
import json
import os
import shutil
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk import aws_iam as iam, aws_lambda as _lambda, aws_logs as logs
from aws_cdk.assertions import Annotations, Match, Template

from educational_course_content_generator_with_qna_bot_using_bedrock.cloudfront_waf_stack import CloudFrontWAFStack
from educational_course_content_generator_with_qna_bot_using_bedrock.course_stack import CourseStack
from educational_course_content_generator_with_qna_bot_using_bedrock.qna_stack import QnAStack
from educational_course_content_generator_with_qna_bot_using_bedrock.stack_logging import own_lambda_log_groups

REPO_ROOT = Path(__file__).resolve().parents[2]

# Directories that the stacks package as assets, relative to the directory they are synthesized from
ASSET_DIRECTORIES = ("lambda", "kb_dataset")

# Resource types that hold data, keys or logs
STATEFUL_RESOURCE_TYPES = {
    "AWS::Cognito::UserPool",
    "AWS::DynamoDB::Table",
    "AWS::KMS::Key",
    "AWS::Logs::LogGroup",
    "AWS::OpenSearchServerless::Collection",
    "AWS::S3::Bucket",
    "AWS::SQS::Queue",
}


def cdk_context():
    """The feature flags of cdk.json, which the CDK CLI passes to the app and which change what is synthesized
    (for example the order of policy actions and the custom resource that restricts the default security group
    of the VPC), with asset bundling disabled."""
    with open(REPO_ROOT / "cdk.json", encoding="utf-8") as cdk_json:
        return {**json.load(cdk_json)["context"], "aws:cdk:bundling-stacks": []}


def synthesize(config, working_directory):
    """Synthesize the three stacks from a directory that holds project_config.json and the asset directories."""
    previous_cwd = os.getcwd()
    os.chdir(working_directory)
    try:
        app = cdk.App(context=cdk_context())
        waf_stack = CloudFrontWAFStack(app, config["CloudFrontWAFStackName"])
        # CourseStack must exist before QnAStack, which reuses its VPC
        course_stack = CourseStack(app, config["CourseStackName"])
        qna_stack = QnAStack(app, config["QnAStackName"], vpc=course_stack.vpc)
        return {
            "waf": Template.from_stack(waf_stack),
            "course": Template.from_stack(course_stack),
            "qna": Template.from_stack(qna_stack),
        }
    finally:
        os.chdir(previous_cwd)


def resources_of_type(stack_templates, resource_type):
    """(stack, logical id, resource) of every resource of that type in the synthesized stacks."""
    return [
        (stack, logical_id, resource)
        for stack, template in stack_templates.items()
        for logical_id, resource in template.find_resources(resource_type).items()
    ]


def only(resources):
    """The (logical id, resource) pair of a find_resources() result that must hold exactly one resource."""
    assert len(resources) == 1, f"expected exactly one resource, found {sorted(resources)}"
    return next(iter(resources.items()))


@pytest.fixture(scope="module")
def project_config():
    with open(REPO_ROOT / "project_config.json", encoding="utf-8") as config_file:
        return json.load(config_file)


@pytest.fixture(scope="module")
def templates(project_config):
    """Templates of the mode that project_config.json selects."""
    return synthesize(project_config, REPO_ROOT)


@pytest.fixture(scope="module")
def classic_templates(project_config, tmp_path_factory):
    """Templates of classic mode, whatever mode project_config.json selects."""
    working_directory = tmp_path_factory.mktemp("classic_mode")
    classic_config = {**project_config, "qna_bot_mode": "classic"}
    (working_directory / "project_config.json").write_text(json.dumps(classic_config), encoding="utf-8")
    for name in ASSET_DIRECTORIES:
        try:
            (working_directory / name).symlink_to(REPO_ROOT / name, target_is_directory=True)
        except OSError:  # Creating symbolic links can require extra privileges on Windows
            shutil.copytree(REPO_ROOT / name, working_directory / name)
    return synthesize(classic_config, working_directory)


@pytest.fixture(scope="module", params=["configured", "classic"])
def any_mode_templates(request, templates, classic_templates):
    """Templates of the configured mode and of classic mode, for what must hold in both."""
    return templates if request.param == "configured" else classic_templates


@pytest.fixture(scope="module")
def kb_sync_queue(templates):
    """Logical id of the queue between the Knowledge Base bucket and the sync Lambda: the only queue that has a
    dead-letter queue."""
    queues = templates["qna"].find_resources("AWS::SQS::Queue", {"Properties": {"RedrivePolicy": Match.any_value()}})
    return only(queues)[0]


@pytest.fixture(scope="module")
def kb_sync_function(templates):
    """Logical id of the Lambda function that starts the Knowledge Base ingestion jobs."""
    functions = templates["qna"].find_resources("AWS::Lambda::Function", {
        "Properties": {"Environment": {"Variables": {"DATA_SOURCE_ID": Match.any_value()}}},
    })
    return only(functions)[0]


def test_waf_stack_creates_one_cloudfront_web_acl(templates):
    templates["waf"].resource_count_is("AWS::WAFv2::WebACL", 1)
    templates["waf"].has_resource_properties("AWS::WAFv2::WebACL", {"Scope": "CLOUDFRONT"})


def test_course_stack_creates_one_user_pool(templates):
    templates["course"].resource_count_is("AWS::Cognito::UserPool", 1)


def test_course_stack_user_pool_lets_only_administrators_create_users(templates):
    """With self sign-up, anyone who knows the app client ID could create accounts in the user pool."""
    templates["course"].has_resource_properties(
        "AWS::Cognito::UserPool", {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}}
    )


def test_course_stack_exposes_cloudfront_and_cognito_outputs(templates):
    outputs = templates["course"].to_json()["Outputs"]
    for key in ("UserPoolArn", "UserPoolClientId", "CourseCloudFrontDomainName"):
        assert key in outputs, f"missing output {key}"


def test_qna_stack_creates_one_opensearch_collection(templates):
    templates["qna"].resource_count_is("AWS::OpenSearchServerless::Collection", 1)


def test_qna_stack_creates_one_guardrail(templates):
    templates["qna"].resource_count_is("AWS::Bedrock::Guardrail", 1)


def test_qna_stack_creates_one_agentcore_runtime_in_strands_mode(templates, project_config):
    expected = 1 if project_config.get("qna_bot_mode", "strands") == "strands" else 0
    templates["qna"].resource_count_is("AWS::BedrockAgentCore::Runtime", expected)


def test_qna_stack_exposes_cloudfront_output(templates):
    assert "QnACloudFrontDomainName" in templates["qna"].to_json()["Outputs"]


def test_qna_stack_agentcore_runtime_redacts_trace_content(templates, project_config):
    """The Strands tracer must redact prompt/completion/tool/system-prompt content from traces."""
    if project_config.get("qna_bot_mode", "strands") != "strands":
        pytest.skip("AgentCore Runtime is only created in strands mode")
    templates["qna"].has_resource_properties("AWS::BedrockAgentCore::Runtime", {
        "EnvironmentVariables": Match.object_like({
            "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_unredacted_attributes=",
            "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
            "AWS_REDACT_SPAN_ATTRIBUTES": Match.string_like_regexp("gen_ai.input.messages"),
        }),
    })


def test_qna_stack_agentcore_runtime_role_allows_span_delivery_to_its_own_log_group(templates, project_config):
    """AgentCore delivers the spans of a runtime to the runtime's own log group and needs logs:PutResourcePolicy
    on it for that; without the permission no traces arrive. It is limited to the log groups of this runtime."""
    if project_config.get("qna_bot_mode", "strands") != "strands":
        pytest.skip("AgentCore Runtime is only created in strands mode")
    qna = templates["qna"]
    _, runtime = only(qna.find_resources("AWS::BedrockAgentCore::Runtime"))
    role = runtime["Properties"]["RoleArn"]["Fn::GetAtt"][0]
    statements = [
        statement
        for policy in qna.find_resources("AWS::IAM::Policy", {"Properties": {"Roles": [{"Ref": role}]}}).values()
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
        if "logs:PutResourcePolicy" in statement["Action"]
    ]
    assert len(statements) == 1
    assert statements[0]["Effect"] == "Allow" and statements[0]["Action"] == "logs:PutResourcePolicy"
    assert statements[0]["Resource"] == {"Fn::Join": ["", [
        "arn:", {"Ref": "AWS::Partition"}, ":logs:", {"Ref": "AWS::Region"}, ":", {"Ref": "AWS::AccountId"},
        ":log-group:/aws/bedrock-agentcore/runtimes/qna_strands_agent-*",
    ]]}
    assert runtime["Properties"]["AgentRuntimeName"] == "qna_strands_agent"


def test_qna_stack_proxy_lambda_uses_active_tracing_in_strands_mode(templates, project_config):
    """Lambda marks a request as not sampled unless active tracing is on, and the AgentCore Runtime follows that
    decision: without it, questions asked through the WebSocket API leave no trace."""
    if project_config.get("qna_bot_mode", "strands") != "strands":
        pytest.skip("the proxy Lambda is only created in strands mode")
    qna = templates["qna"]
    proxy_functions = qna.find_resources("AWS::Lambda::Function", {
        "Properties": {"Environment": {"Variables": {"AGENT_RUNTIME_ARN": Match.any_value()}}},
    })
    _, proxy = only(proxy_functions)
    assert proxy["Properties"]["TracingConfig"] == {"Mode": "Active"}
    traced = qna.find_resources("AWS::Lambda::Function", {"Properties": {"TracingConfig": Match.any_value()}})
    assert sorted(traced) == sorted(proxy_functions), "only the proxy Lambda needs active tracing"


def test_qna_stack_index_creator_waits_for_vpc_endpoint_security_group_rule(templates):
    """The index creator reaches the collection only through the VPC endpoint, so it must be ordered
    after the endpoint security group and its 443 ingress rule, not only after the collection."""
    resources = templates["qna"].to_json()["Resources"]
    depends_on = set(resources["CustomCollectionIndexCreator"].get("DependsOn", []))
    ingress_rules = {name for name, res in resources.items() if res["Type"] == "AWS::EC2::SecurityGroupIngress"}
    assert "CourseContentCollection" in depends_on
    assert ingress_rules and ingress_rules <= depends_on, f"missing ingress rule dependency: {ingress_rules - depends_on}"


def test_every_lambda_function_logs_to_a_log_group_of_its_stack(any_mode_templates):
    """Lambda would otherwise create /aws/lambda/<function> on the first invocation, outside CloudFormation and
    without a retention period, and `cdk destroy` would leave that log group behind. This includes the functions
    that CDK adds for custom resources."""
    functions = resources_of_type(any_mode_templates, "AWS::Lambda::Function")
    log_groups = [
        (stack, {"Ref": logical_id})
        for stack, logical_id, _ in resources_of_type(any_mode_templates, "AWS::Logs::LogGroup")
    ]
    assert functions
    for stack, logical_id, function in functions:
        log_group = function["Properties"].get("LoggingConfig", {}).get("LogGroup")
        assert (stack, log_group) in log_groups, f"{stack}/{logical_id} does not log to a log group of its stack"


def test_every_lambda_role_is_deleted_before_the_log_group_of_its_function(any_mode_templates):
    """While a stack is deleted, Lambda delivers the last log lines of a custom resource handler a moment after
    CloudFormation deleted the handler. If the log group were gone by then while the role still exists, Lambda
    would create the log group again, outside the stack and without a retention period. The role depends on the
    log group, so CloudFormation deletes the function, then the role and only then the log group."""
    functions = resources_of_type(any_mode_templates, "AWS::Lambda::Function")
    roles = {(stack, logical_id): role
             for stack, logical_id, role in resources_of_type(any_mode_templates, "AWS::IAM::Role")}
    assert functions
    for stack, logical_id, function in functions:
        role = roles[(stack, function["Properties"]["Role"]["Fn::GetAtt"][0])]
        depends_on = role.get("DependsOn", [])
        depends_on = [depends_on] if isinstance(depends_on, str) else depends_on
        log_group = function["Properties"]["LoggingConfig"]["LogGroup"]["Ref"]
        assert log_group in depends_on, f"the role of {stack}/{logical_id} can outlive the log group {log_group}"


def test_no_lambda_role_can_create_a_log_group(any_mode_templates):
    """Second safeguard against a log group that Lambda creates again outside the stack: the stack creates the
    log groups, so no function role needs logs:CreateLogGroup, and each role denies it inline."""
    functions = resources_of_type(any_mode_templates, "AWS::Lambda::Function")
    roles = {(stack, logical_id): role
             for stack, logical_id, role in resources_of_type(any_mode_templates, "AWS::IAM::Role")}
    assert functions
    for stack, logical_id, function in functions:
        role = roles[(stack, function["Properties"]["Role"]["Fn::GetAtt"][0])]
        denied = [
            statement
            for policy in role["Properties"].get("Policies", [])
            for statement in policy["PolicyDocument"]["Statement"]
            if statement["Effect"] == "Deny" and statement["Action"] == "logs:CreateLogGroup"
            and statement["Resource"] == "*"
        ]
        assert denied, f"the role of {stack}/{logical_id} may create log groups"


def test_every_log_group_expires_after_one_month_and_is_deleted_with_its_stack(any_mode_templates):
    log_groups = resources_of_type(any_mode_templates, "AWS::Logs::LogGroup")
    assert log_groups
    for stack, logical_id, log_group in log_groups:
        assert log_group.get("Properties", {}).get("RetentionInDays") == 30, f"{stack}/{logical_id}"
        assert log_group.get("DeletionPolicy") == "Delete", f"{stack}/{logical_id}"
        assert log_group.get("UpdateReplacePolicy") == "Delete", f"{stack}/{logical_id}"


def test_stacks_own_one_log_group_per_lambda_function_and_the_vpc_flow_log_group(any_mode_templates):
    """The numbers that the README states in its Clean up section. They include the functions that CDK adds for
    custom resources; the one that restricts the default security group of the VPC only exists with the feature
    flags of cdk.json."""
    any_mode_templates["course"].resource_count_is("Custom::VpcRestrictDefaultSG", 1)
    functions = {stack: len(template.find_resources("AWS::Lambda::Function"))
                 for stack, template in any_mode_templates.items()}
    log_groups = {stack: len(template.find_resources("AWS::Logs::LogGroup"))
                  for stack, template in any_mode_templates.items()}
    assert functions == {"waf": 0, "course": 10, "qna": 10}
    # CourseStack also owns the log group of the VPC flow logs
    assert log_groups == {"waf": 0, "course": 11, "qna": 10}


def test_own_lambda_log_groups_skips_only_functions_that_already_name_a_log_group():
    stack = cdk.Stack(cdk.App(), "LogGroups")
    function_props = {
        "code": _lambda.Code.from_inline("def handler(event, context): pass"),
        "handler": "index.handler",
        "runtime": _lambda.Runtime.PYTHON_3_12,
    }
    _lambda.Function(stack, "Plain", **function_props)
    # Like the custom resource provider framework: a logging configuration that names no log group
    _lambda.Function(stack, "JsonLogs", logging_format=_lambda.LoggingFormat.JSON, **function_props)
    _lambda.Function(stack, "OwnLogGroup", log_group=logs.LogGroup(stack, "ExplicitLogGroup"), **function_props)

    assert own_lambda_log_groups(stack) == 2

    template = Template.from_stack(stack)
    template.resource_count_is("AWS::Logs::LogGroup", 3)
    for logging_config in (
        {"LogGroup": {"Ref": Match.string_like_regexp("^PlainStackOwnedLogGroup")}},
        {"LogGroup": {"Ref": Match.string_like_regexp("^JsonLogsStackOwnedLogGroup")}, "LogFormat": "JSON"},
        {"LogGroup": {"Ref": Match.string_like_regexp("^ExplicitLogGroup")}},
    ):
        template.has_resource_properties("AWS::Lambda::Function", {"LoggingConfig": logging_config})


def test_own_lambda_log_groups_warns_about_a_function_role_it_cannot_protect():
    """A role that is passed to the function keeps logs:CreateLogGroup and is not ordered before the log
    group; the synthesis says so instead of leaving it unnoticed."""
    stack = cdk.Stack(cdk.App(), "LogGroupRoles")
    function_props = {
        "code": _lambda.Code.from_inline("def handler(event, context): pass"),
        "handler": "index.handler",
        "runtime": _lambda.Runtime.PYTHON_3_12,
    }
    _lambda.Function(stack, "OwnRole", **function_props)
    shared_role = iam.Role(stack, "SharedRole", assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"))
    _lambda.Function(stack, "PassedRole", role=shared_role, **function_props)

    assert own_lambda_log_groups(stack) == 2

    annotations = Annotations.from_stack(stack)
    annotations.has_warning("/LogGroupRoles/PassedRole/Resource", Match.string_like_regexp("logs:CreateLogGroup"))
    annotations.has_no_warning("/LogGroupRoles/OwnRole/Resource", Match.any_value())
    template = Template.from_stack(stack)
    template.has_resource("AWS::IAM::Role", {
        "DependsOn": [Match.string_like_regexp("^OwnRoleStackOwnedLogGroup")],
        "Properties": {"Policies": [Match.object_like({"PolicyName": "DenyLogGroupCreation"})]},
    })
    template.has_resource("AWS::IAM::Role", {
        "DependsOn": Match.absent(),
        "Properties": {"Policies": Match.absent()},
    })


def test_course_stack_kms_key_is_scheduled_for_deletion_with_the_stack(templates):
    """`cdk destroy` schedules the key for deletion; KMS deletes it after the 7-day waiting period."""
    templates["course"].resource_count_is("AWS::KMS::Key", 1)
    templates["course"].has_resource("AWS::KMS::Key", {
        "Properties": {"PendingWindowInDays": 7},
        "DeletionPolicy": "Delete",
        "UpdateReplacePolicy": "Delete",
    })


def test_no_resource_is_retained_when_its_stack_is_deleted(any_mode_templates):
    """Resources that hold data, keys or logs state the Delete policy explicitly; no other resource overrides
    the CloudFormation default, which is to delete the resource with its stack."""
    for stack, template in any_mode_templates.items():
        for logical_id, resource in template.to_json()["Resources"].items():
            default = None if resource["Type"] in STATEFUL_RESOURCE_TYPES else "Delete"
            for attribute in ("DeletionPolicy", "UpdateReplacePolicy"):
                policy = resource.get(attribute, default)
                assert policy == "Delete", f"{stack}/{logical_id} ({resource['Type']}) has {attribute} {policy}"


def test_qna_stack_kb_sync_queue_is_encrypted_and_has_a_dead_letter_queue(templates, kb_sync_queue):
    qna = templates["qna"]
    qna.resource_count_is("AWS::SQS::Queue", 2)
    dead_letter_queues = qna.find_resources("AWS::SQS::Queue", {"Properties": {"RedrivePolicy": Match.absent()}})
    dead_letter_queue, _ = only(dead_letter_queues)
    qna.has_resource_properties("AWS::SQS::Queue", {
        "SqsManagedSseEnabled": True,
        "RedrivePolicy": {"deadLetterTargetArn": {"Fn::GetAtt": [dead_letter_queue, "Arn"]}, "maxReceiveCount": 5},
    })
    qna.has_resource_properties("AWS::SQS::Queue", {
        "SqsManagedSseEnabled": True,
        "MessageRetentionPeriod": 14 * 24 * 60 * 60,
        "RedrivePolicy": Match.absent(),
    })
    # Both queues only accept TLS connections
    for queue in (kb_sync_queue, dead_letter_queue):
        qna.has_resource_properties("AWS::SQS::QueuePolicy", {
            "Queues": [{"Ref": queue}],
            "PolicyDocument": {"Statement": Match.array_with([Match.object_like({
                "Effect": "Deny",
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            })])},
        })


def test_qna_stack_kb_sync_queue_accepts_s3_notifications_only_for_the_kb_bucket_of_this_account(
        templates, kb_sync_queue):
    """A bucket ARN carries no account id, and the name of a deleted bucket can be taken by a bucket of another
    account. The queue policy therefore limits Amazon S3 by the bucket ARN and by the account of the bucket."""
    qna = templates["qna"]
    queue_arn = {"Fn::GetAtt": [kb_sync_queue, "Arn"]}
    kb_bucket_arn = {"Fn::Join": ["", [
        "arn:", {"Ref": "AWS::Partition"}, ":s3:::", {"Fn::ImportValue": "KBBucketName"},
    ]]}
    _, policy = only(qna.find_resources("AWS::SQS::QueuePolicy", {"Properties": {"Queues": [{"Ref": kb_sync_queue}]}}))
    statements = policy["Properties"]["PolicyDocument"]["Statement"]

    # Amazon S3 is the only principal that the policy allows, and only on behalf of the Knowledge Base bucket
    allowed = [statement for statement in statements if statement["Effect"] == "Allow"]
    assert len(allowed) == 1, allowed
    assert allowed[0]["Principal"] == {"Service": "s3.amazonaws.com"}
    assert "sqs:SendMessage" in allowed[0]["Action"]
    assert allowed[0]["Resource"] == queue_arn
    assert allowed[0]["Condition"] == {"ArnLike": {"aws:SourceArn": kb_bucket_arn}}

    # Whatever the policy allows Amazon S3 to do is denied on behalf of a resource of another account
    assert {
        "Sid": "DenyS3NotificationsFromOtherAccounts",
        "Effect": "Deny",
        "Principal": {"Service": "s3.amazonaws.com"},
        "Action": "sqs:*",
        "Resource": queue_arn,
        "Condition": {"StringNotEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}},
    } in statements


def test_qna_stack_kb_sync_queue_visibility_timeout_covers_six_function_timeouts_and_the_batching_window(
        templates, kb_sync_queue, kb_sync_function):
    """AWS recommends this visibility timeout for an SQS event source with a batching window: six times the
    function timeout plus the batching window."""
    qna = templates["qna"]
    function_timeout = qna.find_resources("AWS::Lambda::Function")[kb_sync_function]["Properties"]["Timeout"]
    _, event_source_mapping = only(qna.find_resources("AWS::Lambda::EventSourceMapping"))
    batching_window = event_source_mapping["Properties"]["MaximumBatchingWindowInSeconds"]
    visibility_timeout = qna.find_resources("AWS::SQS::Queue")[kb_sync_queue]["Properties"]["VisibilityTimeout"]
    assert (function_timeout, batching_window) == (60, 30)
    assert visibility_timeout == 6 * function_timeout + batching_window


def test_qna_stack_sync_lambda_receives_the_bucket_notifications_in_batches(templates, kb_sync_queue, kb_sync_function):
    """Up to 30 seconds of notifications reach the sync Lambda as one batch, so that a bulk upload does not start
    one ingestion job per file."""
    templates["qna"].resource_count_is("AWS::Lambda::EventSourceMapping", 1)
    templates["qna"].has_resource_properties("AWS::Lambda::EventSourceMapping", {
        "EventSourceArn": {"Fn::GetAtt": [kb_sync_queue, "Arn"]},
        "FunctionName": {"Ref": kb_sync_function},
        "BatchSize": 100,
        "MaximumBatchingWindowInSeconds": 30,
        "ScalingConfig": {"MaximumConcurrency": 2},
    })


def test_qna_stack_kb_bucket_notifies_the_queue_instead_of_the_sync_lambda(templates, kb_sync_queue):
    qna = templates["qna"]
    _, notifications = only(qna.find_resources("Custom::S3BucketNotifications"))
    configuration = notifications["Properties"]["NotificationConfiguration"]
    assert list(configuration) == ["QueueConfigurations"]
    queue_configurations = configuration["QueueConfigurations"]
    assert sorted(queue_configuration["Events"] for queue_configuration in queue_configurations) == [
        ["s3:ObjectCreated:*"],
        ["s3:ObjectRemoved:*"],
    ]
    for queue_configuration in queue_configurations:
        assert queue_configuration["QueueArn"] == {"Fn::GetAtt": [kb_sync_queue, "Arn"]}
        filter_rules = {rule["Name"]: rule["Value"] for rule in queue_configuration["Filter"]["Key"]["FilterRules"]}
        assert filter_rules == {"prefix": "final-course-content/", "suffix": ".metadata.json"}
    # S3 no longer invokes the sync Lambda, so no permission for it remains
    assert not qna.find_resources("AWS::Lambda::Permission", {"Properties": {"Principal": "s3.amazonaws.com"}})


def test_qna_stack_sample_dataset_upload_waits_for_the_bucket_notifications(templates, kb_sync_queue, kb_sync_function):
    """The upload of the sample dataset only starts an ingestion job if the bucket already notifies the queue."""
    qna = templates["qna"]
    notifications, _ = only(qna.find_resources("Custom::S3BucketNotifications"))
    _, bucket_deployment = only(qna.find_resources("Custom::CDKBucketDeployment"))
    assert {notifications, kb_sync_queue, kb_sync_function} <= set(bucket_deployment["DependsOn"])


def test_qna_stack_sync_lambda_can_start_and_list_ingestion_jobs_of_the_knowledge_base(templates, kb_sync_function):
    """The sync Lambda lists the ingestion jobs to skip a sync that a job started after the change already covers."""
    qna = templates["qna"]
    knowledge_base, _ = only(qna.find_resources("AWS::Bedrock::KnowledgeBase"))
    role = qna.find_resources("AWS::Lambda::Function")[kb_sync_function]["Properties"]["Role"]["Fn::GetAtt"][0]
    _, policy = only(qna.find_resources("AWS::IAM::Policy", {"Properties": {"Roles": [{"Ref": role}]}}))
    # These are the only Amazon Bedrock permissions of the role; the order of the actions depends on the
    # @aws-cdk/aws-iam:minimizePolicies feature flag
    bedrock_statements = [statement for statement in policy["Properties"]["PolicyDocument"]["Statement"]
                          if "bedrock:" in json.dumps(statement["Action"])]
    assert len(bedrock_statements) == 1, bedrock_statements
    assert sorted(bedrock_statements[0]["Action"]) == ["bedrock:ListIngestionJobs", "bedrock:StartIngestionJob"]
    assert bedrock_statements[0]["Effect"] == "Allow"
    assert bedrock_statements[0]["Resource"] == {"Fn::GetAtt": [knowledge_base, "KnowledgeBaseArn"]}


def test_classic_mode_has_no_tracing(classic_templates):
    """Active tracing belongs to the proxy Lambda of Strands mode; Classic mode has no AgentCore Runtime to trace."""
    for stack, template in classic_templates.items():
        traced = template.find_resources("AWS::Lambda::Function", {"Properties": {"TracingConfig": Match.any_value()}})
        assert not traced, f"{stack}: {sorted(traced)}"
        for logical_id, policy in template.find_resources("AWS::IAM::Policy").items():
            assert "xray:" not in json.dumps(policy["Properties"]["PolicyDocument"]), f"{stack}/{logical_id}"


def test_classic_mode_bot_lambda_can_resolve_the_inference_profile(classic_templates):
    """RetrieveAndGenerate calls GetInferenceProfile on the configured inference profile before it invokes the
    model, so every question fails with an access denied error if the role of the Lambda does not allow it."""
    qna = classic_templates["qna"]
    qna.resource_count_is("AWS::BedrockAgentCore::Runtime", 0)
    _, bot = only(qna.find_resources("AWS::Lambda::Function", {
        "Properties": {"Environment": {"Variables": {"QnA_MODEL_ARN": Match.any_value()}}},
    }))
    qna.has_resource_properties("AWS::IAM::Policy", {
        "Roles": [{"Ref": bot["Properties"]["Role"]["Fn::GetAtt"][0]}],
        "PolicyDocument": {"Statement": Match.array_with([{
            "Action": "bedrock:GetInferenceProfile",
            "Effect": "Allow",
            "Resource": {"Fn::Join": ["", [
                "arn:aws:bedrock:",
                {"Ref": "AWS::Region"},
                ":",
                {"Ref": "AWS::AccountId"},
                Match.string_like_regexp(r"^:inference-profile/[a-z]+\.anthropic\.claude-\*$"),
            ]]},
        }])},
    })


def test_strands_mode_does_not_grant_get_inference_profile(templates, project_config):
    """Only RetrieveAndGenerate, which classic mode calls, resolves the inference profile."""
    if project_config.get("qna_bot_mode", "strands") != "strands":
        pytest.skip("project_config.json selects classic mode")
    assert "bedrock:GetInferenceProfile" not in json.dumps(templates["qna"].to_json())
