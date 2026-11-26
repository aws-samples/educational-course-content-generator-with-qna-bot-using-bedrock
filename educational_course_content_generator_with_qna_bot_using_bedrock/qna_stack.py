# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from aws_cdk import (
    ArnFormat,
    BundlingOptions,
    CfnOutput,
    DockerImage,
    Fn,
    RemovalPolicy,
    Stack,
    Duration,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_s3_assets as s3_assets,
    aws_lambda_python_alpha as _alambda,
    aws_cognito as cognito,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrationsv2,
    aws_apigatewayv2_authorizers as authorizersv2,
    aws_dynamodb as dynamodb,
    aws_lambda_event_sources as lambda_event_sources,
    aws_sqs as sqs,
    CustomResource,
    aws_bedrock as bedrock,
    aws_bedrockagentcore as agentcore,
    aws_opensearchserverless as aoss,
    aws_s3_deployment as s3_deployment,
    aws_s3_notifications as s3n,
    custom_resources,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_ec2 as ec2,
)
from constructs import Construct
from cdk_nag import NagSuppressions
import json

from educational_course_content_generator_with_qna_bot_using_bedrock.solution import user_agent_string
from educational_course_content_generator_with_qna_bot_using_bedrock.stack_logging import own_lambda_log_groups

# Model ids starting with one of these prefixes are cross-region inference profiles, not foundation models.
INFERENCE_PROFILE_PREFIXES = ("us", "eu", "au", "jp", "apac", "global")

# SAM build image used to bundle the AgentCore artifact, pinned by digest for reproducible builds.
AGENTCORE_BUNDLING_IMAGE = (
    "public.ecr.aws/sam/build-python3.12"
    "@sha256:9e3dbdcb3daa882f18b1449e1210c0867efc3349cf3bb419b6dacf49a8d92eed"
)

# Number of Knowledge Base passages retrieved per question.
KB_MAX_RESULTS = "3"

# Name of the AgentCore Runtime; the runtime id and the name of its log group start with it.
AGENTCORE_RUNTIME_NAME = "qna_strands_agent"

# Span attributes that carry conversation content under the OpenTelemetry GenAI conventions. ADOT
# redacts them before export, as a second layer behind the Strands tracer's own redaction.
GENAI_CONTENT_SPAN_ATTRIBUTES = ",".join([
    "gen_ai.input.messages",
    "gen_ai.output.messages",
    "gen_ai.system_instructions",
    "gen_ai.tool.call.arguments",
    "gen_ai.tool.call.result",
])


def bedrock_model_arn(scope, model_id):
    prefix = model_id.split(".")[0]
    if prefix in INFERENCE_PROFILE_PREFIXES:
        return f"arn:aws:bedrock:{scope.region}:{scope.account}:inference-profile/{model_id}"
    return f"arn:aws:bedrock:{scope.region}::foundation-model/{model_id}"


def bedrock_inference_profile_iam_resource(scope, model_id):
    """IAM resource for the Anthropic Claude inference profiles of the geography that the configured model id uses."""
    prefix = model_id.split(".")[0]
    if prefix not in INFERENCE_PROFILE_PREFIXES:
        prefix = "us"
    return f"arn:aws:bedrock:{scope.region}:{scope.account}:inference-profile/{prefix}.anthropic.claude-*"


def bedrock_claude_iam_resources(scope, model_id):
    """IAM resources needed to invoke Anthropic Claude models directly or through the configured inference profile."""
    return [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        bedrock_inference_profile_iam_resource(scope, model_id),
    ]


# Bump this value whenever the Bedrock Guardrail policies in this stack change. AWS::Bedrock::GuardrailVersion
# snapshots the guardrail configuration when the version resource is created, so without a property change
# CloudFormation keeps the old version and the GUARDRAIL_VERSION environment variable of the QnA bot keeps pointing
# at stale policies. A new description replaces the version resource and publishes the current configuration.
GUARDRAIL_CONFIG_REVISION = "2"


class QnAStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, vpc=None, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if vpc is None:
            raise ValueError(
                "QnAStack requires the VPC created by CourseStack: the OpenSearch Serverless collection is only "
                "reachable through a VPC endpoint in that VPC, and the QnA Lambda functions run in its private subnets."
            )

        with open("project_config.json", "r") as file:
            variables = json.load(file)
        
        qna_model_id = variables["qna_model_id"]
        qna_bot_mode = variables.get("qna_bot_mode", "strands")  # "strands" (default) or "classic"

        ######################### Imports  #########################
        # Import the existing user pool
        user_pool_arn = Fn.import_value("UserPoolArn")
        user_pool = cognito.UserPool.from_user_pool_arn(self, "ImportedUserPool", user_pool_arn)

        # Import the existing buckets
        access_logs_bucket_name = Fn.import_value("AccessLogsBucketName")
        access_logs_bucket = s3.Bucket.from_bucket_name(self, "ImportedAccessLogsBucket", access_logs_bucket_name)

        kb_bucket_name = Fn.import_value("KBBucketName")
        kb_bucket = s3.Bucket.from_bucket_name(self, "ImportedKBBucket", kb_bucket_name)

        # Import the existing user pool client
        user_pool_client_id = Fn.import_value("UserPoolClientId")
        user_pool_client = cognito.UserPoolClient.from_user_pool_client_id(self, "ImportedUserPoolClient", user_pool_client_id)

        # Import the existing cryptography layer
        cryptography_layer_arn = Fn.import_value("CryptographyLayerArn")
        cryptography_layer = _lambda.LayerVersion.from_layer_version_arn(
            self, "ImportedCryptographyLayer", cryptography_layer_arn
        )

        # Import the existing pyJWT layer
        pyJWT_layer_arn = Fn.import_value("PyJWTLayerArn")
        pyJWT_layer = _lambda.LayerVersion.from_layer_version_arn(
            self, "ImportedPyJWTLayer", pyJWT_layer_arn
        )
        
        ######################### QnA Connection DDB Table  #########################
        qna_connections_ddb_table = dynamodb.Table(self, "QnAConnectionsTable",
                        partition_key=dynamodb.Attribute(name="connectionId", type=dynamodb.AttributeType.STRING),
                        time_to_live_attribute="ttl",
                        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                        encryption=dynamodb.TableEncryption.AWS_MANAGED,
                        point_in_time_recovery=True,
                        removal_policy= RemovalPolicy.DESTROY
        )

        ######################### Lambda Functions for WebSocket #########################
        ######## VPC subnet selection for Lambda functions
        vpc_config = {
            "vpc": vpc,
            "vpc_subnets": ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
        }

        ######## Connect WebSocket Lambda
        qna_ws_connect_lambda = _lambda.Function(
            self, "QnAWSConnect",
            code=_lambda.Code.from_asset("./lambda/connect"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            handler="index.lambda_handler",
            timeout=Duration.seconds(30),
            **vpc_config,
            environment={
                "CONNECTIONS_TABLE": qna_connections_ddb_table.table_name
            },
        )
        qna_connections_ddb_table.grant_read_write_data(qna_ws_connect_lambda)

        ######## Disconnect WebSocket Lambda
        qna_ws_disconnect_lambda = _lambda.Function(
            self, "QnAWSDisconnect",
            code=_lambda.Code.from_asset("./lambda/disconnect"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            handler="index.lambda_handler",
            timeout=Duration.seconds(30),
            **vpc_config,
            environment={
                "CONNECTIONS_TABLE": qna_connections_ddb_table.table_name
            },
        )
        qna_connections_ddb_table.grant_read_write_data(qna_ws_disconnect_lambda)

        ######## Default WebSocket Lambda
        qna_ws_default_lambda = _lambda.Function(
            self, "QnAWSDefault",
            code=_lambda.Code.from_asset("./lambda/default"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            handler="index.lambda_handler",
            timeout=Duration.seconds(30),
            **vpc_config,
            environment={
                "CONNECTIONS_TABLE": qna_connections_ddb_table.table_name
            },
        )
        qna_connections_ddb_table.grant_read_write_data(qna_ws_default_lambda)

        ######## Cognito Authorization Lambda
        jwt_auth_qna_lambda = _lambda.Function(self, 
                                "jwt_auth_funtion_lambda",
                                code=_lambda.Code.from_asset("./lambda/jwt_auth"),
                                runtime=_lambda.Runtime.PYTHON_3_12,
                                architecture=_lambda.Architecture.ARM_64,
                                memory_size=512,
                                timeout=Duration.seconds(10),
                                handler="index.lambda_handler",
                                layers=[cryptography_layer, pyJWT_layer],
                                **vpc_config,
                               environment={
                                        "API_REGION": self.region,
                                        "ACCOUNT_ID": self.account,
                                        "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                                        "COGNITO_APP_CLIENT_ID": user_pool_client.user_pool_client_id,
                                    },
                            )

        ######################### Knowledge Base Part1: Role & Bucket creation #########################
        embeddings_model_id = variables["embeddings_model_id"]
        embeddings_vector_size = variables["embeddings_vector_size"]
        vector_index_name = variables["vector_index_name"]
        metadata_field = variables["metadata_field"]
        text_field = variables["text_field"]
        vector_field = variables["vector_field"]
        # Role that will be used by the KB
        kb_role = iam.Role(scope=self,
                           id="CourseKBRole",
                           assumed_by=iam.ServicePrincipal("bedrock.amazonaws.com"))
        
        # # S3 bucket that will be used for our storage needs
        kb_bucket.grant_read(kb_role)
        
        bucket_deployment = s3_deployment.BucketDeployment(self, 
                                "copy_files_to_s3_bucket",
                                sources=[s3_deployment.Source.asset("./kb_dataset",
                                                                    exclude=["**/~$*", "~$*", "**/.DS_Store", "Readme.txt"])],
                                destination_bucket=kb_bucket,
                                destination_key_prefix="final-course-content/",
                                )
        
        
        ######################### OPEN SEARCH SERVERLESS  #########################
        ######## Collection
        course_collection = aoss.CfnCollection(scope=self,
                                                      id="CourseContentCollection",
                                                      name="course-content-collection",
                                                      # the properties below are optional
                                                      description="Course content embeddings Store",
                                                      standby_replicas="DISABLED",
                                                      type="VECTORSEARCH",
                                                      )
        # The collection holds the vector index: state explicitly that `cdk destroy` deletes it
        course_collection.apply_removal_policy(RemovalPolicy.DESTROY)

        ######## Encryption Policy
        encryption_policy_document = json.dumps({"Rules": [{"ResourceType": "collection",
                                                            "Resource": [f"collection/{course_collection.name}"]}],
                                                 "AWSOwnedKey": True},
                                                separators=(",", ":"))
        encryption_policy = aoss.CfnSecurityPolicy(scope=self,
                                                            id="CollectionEncryptionPolicy",
                                                            name="content-coll-encryption-policy",
                                                            description="Encryption policy for course content collection",
                                                            type="encryption",
                                                            policy=encryption_policy_document)
        course_collection.add_dependency(encryption_policy)

        ######## Network Policy
        # The collection is reachable only through an OpenSearch Serverless VPC endpoint (used by the
        # index creator Lambda) and from the Bedrock Knowledge Base service; there is no public fallback.
        vpce_security_group = ec2.SecurityGroup(self, "AossVpceSecurityGroup",
                                                vpc=vpc,
                                                description="OpenSearch Serverless VPC endpoint for the course content collection")
        index_lambda_security_group = ec2.SecurityGroup(self, "IndexCreatorLambdaSecurityGroup",
                                                        vpc=vpc,
                                                        description="Index creator Lambda for the course content collection")
        vpce_security_group.add_ingress_rule(index_lambda_security_group, ec2.Port.tcp(443),
                                             "HTTPS from the index creator Lambda")
        collection_vpce = aoss.CfnVpcEndpoint(self, "CourseCollectionVpcEndpoint",
                                              name="course-content-coll-vpce",
                                              vpc_id=vpc.vpc_id,
                                              subnet_ids=[subnet.subnet_id for subnet in vpc.private_subnets],
                                              security_group_ids=[vpce_security_group.security_group_id])
        network_policy_document = json.dumps([{"Rules": [{"Resource": [f"collection/{course_collection.name}"],
                                                          "ResourceType": "collection"}],
                                               "AllowFromPublic": False,
                                               "SourceVPCEs": [collection_vpce.attr_id],
                                               "SourceServices": ["bedrock.amazonaws.com"]}], separators=(",", ":"))
        index_lambda_network = {
            "vpc": vpc,
            "vpc_subnets": ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            "security_groups": [index_lambda_security_group],
        }
        network_policy = aoss.CfnSecurityPolicy(scope=self,
                                                         id="CollectionNetworkPolicy",
                                                         name="content-coll-network-policy",
                                                         description="Network policy for course content collection",
                                                         type="network",
                                                         policy=network_policy_document)
        if collection_vpce:
            network_policy.add_dependency(collection_vpce)
        course_collection.add_dependency(network_policy)

        ######## Indexing Lambda
        opensearch_py_layer =_alambda.PythonLayerVersion(self, "opensearch-py-layer",
            entry = "./lambda/lambda_layer/opensearch_py_layer/",
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )

        requests_aws4auth_layer =_alambda.PythonLayerVersion(self, "requests-aws4auth-layer",
            entry = "./lambda/lambda_layer/requests_aws4auth_layer/",
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )

        opensearch_index_cust_res_lambda = _lambda.Function(
                                                    self, "opensearch_index_cust_res_lambda",
                                                    code=_lambda.Code.from_asset("./lambda/opensearch_index_cust_res"),
                                                    runtime=_lambda.Runtime.PYTHON_3_12,
                                                    handler="index.lambda_handler",
                                                    timeout=Duration.minutes(2),
                                                    memory_size=512,
                                                    layers=[opensearch_py_layer, requests_aws4auth_layer],
                                                    **index_lambda_network,
                                                )
        
        res_provider = custom_resources.Provider(scope=self,
                                                 id="CustomResourceIndexCreator",
                                                 on_event_handler=opensearch_index_cust_res_lambda)
        
        index_creator = CustomResource(scope=self,
                                       id="CustomCollectionIndexCreator",
                                       service_token=res_provider.service_token,
                                       properties={"collection": course_collection.name,
                                                   "endpoint": course_collection.attr_collection_endpoint,
                                                   "vector_index_name": vector_index_name,
                                                   "vector_size": embeddings_vector_size,  # Depends on embeddings model
                                                   "metadata_field": metadata_field,
                                                   "text_field": text_field,
                                                   "vector_field": vector_field})
        index_creator.node.add_dependency(course_collection)
        # The index creator reaches the collection only through the VPC endpoint, so also wait for the
        # endpoint security group and its 443 ingress rule (a child of the security group construct).
        index_creator.node.add_dependency(vpce_security_group)
        
        ######## Data Access Policy
        data_policy_json = json.dumps([{"Rules":
                                  [{"Resource": [f"collection/{course_collection.name}"],
                                    "Permission": ["aoss:CreateCollectionItems",
                                                   "aoss:DeleteCollectionItems",
                                                   "aoss:UpdateCollectionItems",
                                                   "aoss:DescribeCollectionItems"],
                                    "ResourceType": "collection"},
                                   {"Resource": [f"index/{course_collection.name}/*"],
                                    "Permission": ["aoss:CreateIndex",
                                                   "aoss:DeleteIndex",
                                                   "aoss:UpdateIndex",
                                                   "aoss:DescribeIndex",
                                                   "aoss:ReadDocument",
                                                   "aoss:WriteDocument"],
                                    "ResourceType": "index"}],
                              "Principal": [kb_role.role_arn, 
                                            opensearch_index_cust_res_lambda.role.role_arn, 
                                            # "arn:aws:iam::123456789012:role/CrossAccountRole"
                                            ],
                              "Description": "Data access rule"}], 
                              separators=(",", ":")
                              )
        data_access_policy = aoss.CfnAccessPolicy(scope=self,
                                                           id="DataAccessPolicy",
                                                           name="content-coll-data-access-policy",
                                                           description="Data access policy for course content collection",
                                                           type="data",
                                                           policy=data_policy_json)
        course_collection.add_dependency(data_access_policy)

        opensearch_index_cust_res_lambda.role.add_to_policy(iam.PolicyStatement(
                                                    effect=iam.Effect.ALLOW,
                                                    resources=[course_collection.attr_arn],
                                                    actions=["aoss:APIAccessAll"]))


        ######################### Knowledge Base Part2: KB and Sych lambda #########################
        ######## Embedding model 
        embeddings_model_arn = bedrock.FoundationModel.from_foundation_model_id(self, 
                                                    _id="EmbeddingsModel", 
                                                    foundation_model_id=bedrock.FoundationModelIdentifier(embeddings_model_id)
                                                    ).model_arn

        ######## Policy to access AOSS and Embedding Model
        kb_role.add_to_policy(iam.PolicyStatement(sid="OpenSearchServerlessAPIAccessAllStatement",
                                                  effect=iam.Effect.ALLOW,
                                                  resources=[course_collection.attr_arn],
                                                  actions=["aoss:APIAccessAll"]))
        kb_role.add_to_policy(iam.PolicyStatement(sid="BedrockInvokeModelStatement",
                                                  effect=iam.Effect.ALLOW,
                                                  resources=[embeddings_model_arn],
                                                  actions=["bedrock:InvokeModel"]))

        ######## KB Configuration
        knowledge_base = bedrock.CfnKnowledgeBase(scope=self,
                                                    id="CourseKB",
                                                    name="CourseKB",
                                                    role_arn=kb_role.role_arn,
                                                    knowledge_base_configuration={"type": "VECTOR",
                                                                                    "vectorKnowledgeBaseConfiguration": {
                                                                                        "embeddingModelArn": embeddings_model_arn,
                                                                                        # "embeddingModelConfiguration": {
                                                                                        #     "bedrockEmbeddingModelConfiguration": {
                                                                                        #         "dimensions": embeddings_vector_size
                                                                                        #     }
                                                                                        # }
                                                                                    }
                                                                                },
                                                    storage_configuration={"type": "OPENSEARCH_SERVERLESS",
                                                                            "opensearchServerlessConfiguration": {
                                                                                "collectionArn": course_collection.attr_arn,
                                                                                "vectorIndexName": vector_index_name,
                                                                                "fieldMapping": {
                                                                                    "metadataField": metadata_field,
                                                                                    "textField": text_field,
                                                                                    "vectorField": vector_field,
                                                                                }
                                                                            }
                                                                        }
                                                    )
        knowledge_base.node.add_dependency(index_creator)


        ######## KB Data Source
        # Create the data source; we could also define the chunking strategy, but let"s leave its default values
        kb_data_source = bedrock.CfnDataSource(scope=self,
                                                 id="KBDataSource",
                                                 name="KBDataSource",
                                                 knowledge_base_id= knowledge_base.attr_knowledge_base_id,
                                                 data_source_configuration={"s3Configuration":
                                                                            {"bucketArn": kb_bucket.bucket_arn},
                                                                            "type": "S3"},
                                                 data_deletion_policy="RETAIN")
        
        ######## KB Sync lambda
        kb_sync_lambda = _lambda.Function(self, 
                        "kb_sync_lambda",
                        code=_lambda.Code.from_asset("./lambda/kb_sync"),
                        runtime=_lambda.Runtime.PYTHON_3_12,
                        architecture=_lambda.Architecture.ARM_64,
                        memory_size=512,
                        timeout=Duration.minutes(1),
                        handler="index.lambda_handler",
                        environment={"KNOWLEDGE_BASE_ID": knowledge_base.attr_knowledge_base_id,
                                     "DATA_SOURCE_ID" :kb_data_source.attr_data_source_id
                                     }
                    )
        # ListIngestionJobs lets the function skip a sync that a job started after the change already covers
        kb_synch_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:StartIngestionJob", "bedrock:ListIngestionJobs"],
            resources=[knowledge_base.attr_knowledge_base_arn],
        )
        kb_sync_lambda.add_to_role_policy(kb_synch_policy_statement)

        ######## KB Sync queue
        # S3 sends one notification per object. The notifications are buffered in a queue and reach the sync
        # Lambda in batches, so that a bulk upload does not start one ingestion job per file.
        kb_sync_dlq = sqs.Queue(
            self,
            "KBSyncDeadLetterQueue",
            retention_period=Duration.days(14),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
        )
        kb_sync_batching_window = Duration.seconds(30)
        kb_sync_queue = sqs.Queue(
            self,
            "KBSyncQueue",
            # Recommended for an SQS event source: six times the function timeout plus the batching window
            visibility_timeout=Duration.seconds(
                6 * kb_sync_lambda.timeout.to_seconds() + kb_sync_batching_window.to_seconds()
            ),
            enforce_ssl=True,
            encryption=sqs.QueueEncryption.SQS_MANAGED,
            removal_policy=RemovalPolicy.DESTROY,
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=5, queue=kb_sync_dlq),
        )
        kb_sync_lambda.add_event_source(lambda_event_sources.SqsEventSource(
            kb_sync_queue,
            batch_size=100,
            max_batching_window=kb_sync_batching_window,
            max_concurrency=2,
        ))

        ######## S3 Create/Remove event
        # S3 event notifications to the queue when a metadata file is added to or removed from the S3 bucket
        s3_notification_key_filter = s3.NotificationKeyFilter(
                                    prefix="final-course-content/",
                                    suffix=".metadata.json",
                                )
        kb_bucket.add_event_notification(s3.EventType.OBJECT_CREATED, s3n.SqsDestination(kb_sync_queue), s3_notification_key_filter)
        kb_bucket.add_event_notification(s3.EventType.OBJECT_REMOVED, s3n.SqsDestination(kb_sync_queue), s3_notification_key_filter)

        # Confused-deputy protection: the statement that SqsDestination adds allows Amazon S3 by the ARN of the
        # bucket, which carries no account id, and the name of a deleted bucket can be taken by a bucket of
        # another account. Amazon S3 is therefore denied the queue unless it acts for a resource of this account.
        kb_sync_queue.add_to_resource_policy(iam.PolicyStatement(
            sid="DenyS3NotificationsFromOtherAccounts",
            effect=iam.Effect.DENY,
            principals=[iam.ServicePrincipal("s3.amazonaws.com")],
            actions=["sqs:*"],
            resources=[kb_sync_queue.queue_arn],
            conditions={"StringNotEquals": {"aws:SourceAccount": self.account}},
        ))

        # The sample dataset is uploaded only once the sync Lambda, the queue and the bucket notification
        # configuration (the "Notifications" construct that add_event_notification creates under the bucket)
        # exist, so that the upload can produce the events that synchronize the Knowledge Base. The events
        # are not guaranteed: Amazon S3 can take about five minutes to apply a new notification configuration
        # (the README describes how to start the first ingestion job by hand).
        bucket_deployment.node.add_dependency(kb_sync_lambda)
        bucket_deployment.node.add_dependency(kb_bucket.node.try_find_child("Notifications"))
        bucket_deployment.node.add_dependency(kb_sync_queue)

        ######################### Bedrock Guardrail #########################
        # Create a guardrail configuration for the bedrock agent
        qna_bot_guardrail = bedrock.CfnGuardrail(self, "QnABotGuardrail",
            name="qna-bot-guardrail",
            description="Guardrail configuration for the QnA Bot",
            blocked_input_messaging = "I apologize, but I'm not able to address that question within the scope of this application. Is there something else I can assist you with ?",
            blocked_outputs_messaging = "I apologize, but the answer to that question falls outside the scope of this application. Is there another way I can help you today?",
            # Filter strength for incoming user prompts and outgoing agent responses
            content_policy_config=bedrock.CfnGuardrail.ContentPolicyConfigProperty(
                filters_config=[
                    # Prompt-attack detection applies to user input only; the service requires NONE on output.
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH",
                        output_strength="NONE",
                        type="PROMPT_ATTACK"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH",
                        output_strength="HIGH",
                        type="MISCONDUCT"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH",
                        output_strength="HIGH",
                        type="INSULTS"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH",
                        output_strength="HIGH",
                        type="HATE"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH",
                        output_strength="HIGH",
                        type="SEXUAL"
                    ),
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="HIGH",
                        output_strength="HIGH",
                        type="VIOLENCE"
                    )                    
                ]
            ),
            
            sensitive_information_policy_config=bedrock.CfnGuardrail.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[
                        bedrock.CfnGuardrail.PiiEntityConfigProperty(
                            action="ANONYMIZE",
                            type="EMAIL"
                        ),
                        bedrock.CfnGuardrail.PiiEntityConfigProperty(
                            action="ANONYMIZE",
                            type="PHONE"
                        ),
                        bedrock.CfnGuardrail.PiiEntityConfigProperty(
                            action="BLOCK",
                            type="CREDIT_DEBIT_CARD_NUMBER"
                        )
                ],
                regexes_config=[
                    bedrock.CfnGuardrail.RegexConfigProperty(
                        name="Account Number",
                        action="ANONYMIZE",
                        pattern=r"\b\d{6}\d{4}\b",
                        description="Matches account numbers in the format XXXXXX1234"
                    )
                ]
            ),

            topic_policy_config=bedrock.CfnGuardrail.TopicPolicyConfigProperty(
                topics_config=[
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        definition="Any discussion related to illegal activities",
                        name="IllegalActivities",
                        type="DENY",
                        examples=["How to make illegal substances", "Planning a robbery"]
                    ),
                    bedrock.CfnGuardrail.TopicConfigProperty(
                        definition="Requests for personalised recommendations about which specific securities to buy or sell, "
                                   "or how an individual should allocate their own money or retirement savings",
                        name="PersonalizedFinancialAdvice",
                        type="DENY",
                        examples=["Which stock should I buy with my savings?",
                                  "How should I allocate my 401k?",
                                  "Should I sell my shares now?"],
                        # Evaluate this topic on student input only. When it was also applied to model
                        # output, Bedrock replaced the tail of legitimate course answers about investment
                        # strategy with the blocked-output message (observed during end-to-end testing).
                        input_enabled=True,
                        output_enabled=False,
                    )
                ]
            ),

            word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
                managed_word_lists_config=[
                    bedrock.CfnGuardrail.ManagedWordsConfigProperty(
                        type="PROFANITY"
                    )
                ],
            ),
        )

        # Create a Guardrail version
        qna_bot_guardrail_version = bedrock.CfnGuardrailVersion(self, "QnABotGuardrailVersion",
            guardrail_identifier=qna_bot_guardrail.attr_guardrail_id,
            description=f"QnA Bot guardrail configuration revision {GUARDRAIL_CONFIG_REVISION}",
        )

        ######################### QnA Bot: Strands Agent on AgentCore or Classic on Lambda #########################
        if qna_bot_mode == "strands":
            # Strands Agent mode: deploys the Strands Agent to Bedrock AgentCore Runtime.
            # AgentCore provides session isolation, observability and managed scaling; the conversation
            # history is kept in memory for the lifetime of a session and is not persisted.
            # A proxy Lambda bridges the WebSocket API Gateway to the AgentCore Runtime.

            # Package the agent code together with its dependencies as a zip asset. AgentCore Runtime
            # runs the artifact as-is (there is no dependency install step at start-up), so the
            # dependencies are bundled here for the arm64 Python 3.12 runtime. The artifact is kept
            # deterministic and free of bytecode (no pip-time .pyc files, no local __pycache__), so its
            # hash, and with it the Runtime resource, only changes when the code or the pinned
            # dependencies change.
            agentcore_code_asset = s3_assets.Asset(self, "AgentCoreCodeAsset",
                path="./lambda/qna_bot_agentcore",
                exclude=["__pycache__", "*.pyc"],
                bundling=BundlingOptions(
                    image=DockerImage.from_registry(AGENTCORE_BUNDLING_IMAGE),
                    command=[
                        "bash", "-c",
                        "pip install --no-compile -r requirements.txt -t /asset-output/ "
                        "--platform manylinux2014_aarch64 --python-version 3.12 --only-binary=:all: "
                        "&& cp -r . /asset-output/ "
                        "&& find /asset-output -name __pycache__ -type d -prune -exec rm -rf {} + "
                        "&& find /asset-output -name '*.pyc' -delete",
                    ],
                ),
            )

            # The entrypoint starts the handler under the bundled ADOT auto-instrumentation so traces are emitted.
            agentcore_artifact = agentcore.AgentRuntimeArtifact.from_s3(
                s3.Location(
                    bucket_name=agentcore_code_asset.s3_bucket_name,
                    object_key=agentcore_code_asset.s3_object_key,
                ),
                agentcore.AgentCoreRuntime.PYTHON_3_12,
                ["opentelemetry-instrument", "agentcore_handler.py"],
            )

            # Create the AgentCore Runtime hosting the Strands Agent
            qna_agent_runtime = agentcore.Runtime(self, "QnAAgentCoreRuntime",
                runtime_name=AGENTCORE_RUNTIME_NAME,
                agent_runtime_artifact=agentcore_artifact,
                description="QnA Bot powered by Strands Agent on Bedrock AgentCore Runtime",
                environment_variables={
                    "QnA_MODEL_ID": qna_model_id,
                    "KNOWLEDGE_BASE_ID": knowledge_base.attr_knowledge_base_id,
                    "GUARDRAIL_ID": qna_bot_guardrail.attr_guardrail_id,
                    "GUARDRAIL_VERSION": qna_bot_guardrail_version.attr_version,
                    "KB_MAX_RESULTS": KB_MAX_RESULTS,
                    # Keep conversation content out of traces and logs (see README, Observability):
                    # - the Strands tracer records prompts, completions, tool arguments/results and the
                    #   system prompt on its spans unless a redaction allowlist is set; an empty allowlist
                    #   redacts all of them,
                    # - ADOT turns on botocore's Bedrock message capture by default on AgentCore, which would
                    #   record the Converse request/response messages on the Bedrock Runtime spans,
                    # - ADOT redacts the listed gen_ai.* content attributes before export as a safety net.
                    "OTEL_SEMCONV_STABILITY_OPT_IN": "gen_ai_unredacted_attributes=",
                    "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "false",
                    "AWS_REDACT_SPAN_ATTRIBUTES": GENAI_CONTENT_SPAN_ATTRIBUTES,
                    # Solution user-agent suffix for the AWS SDK clients of the agent (see "Solution ID and
                    # usage reporting" in the README); not set when there is no solution block
                    **({"USER_AGENT_STRING": user_agent_string()} if user_agent_string() else {}),
                },
            )

            # The agent invokes the configured Claude model, retrieves from the KB and applies the guardrail
            qna_agent_runtime.add_to_role_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=bedrock_claude_iam_resources(self, qna_model_id),
            ))
            qna_agent_runtime.add_to_role_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:Retrieve"],
                resources=[knowledge_base.attr_knowledge_base_arn],
            ))
            qna_agent_runtime.add_to_role_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:ApplyGuardrail"],
                resources=[qna_bot_guardrail.attr_guardrail_arn],
            ))
            # Traces: once CloudWatch Transaction Search is enabled, AgentCore delivers the spans of the
            # agent to the `spans` log stream of the runtime's own log group (the default span destination
            # of new runtimes in Regions that support it). AgentCore uses this permission of the execution role to allow AWS X-Ray to
            # write to that log group; without it no spans are delivered.
            qna_agent_runtime.add_to_role_policy(iam.PolicyStatement(
                sid="SpanDeliveryToRuntimeLogGroup",
                effect=iam.Effect.ALLOW,
                actions=["logs:PutResourcePolicy"],
                resources=[self.format_arn(
                    service="logs",
                    resource="log-group",
                    resource_name=f"/aws/bedrock-agentcore/runtimes/{AGENTCORE_RUNTIME_NAME}-*",
                    arn_format=ArnFormat.COLON_RESOURCE_NAME,
                )],
            ))

            # Proxy Lambda to bridge WebSocket API Gateway to AgentCore Runtime
            qna_bot_lambda = _lambda.Function(self,
                            "qna_bot_lambda",
                            code=_lambda.Code.from_asset("./lambda/qna_bot_agentcore_proxy"),
                            runtime=_lambda.Runtime.PYTHON_3_12,
                            architecture=_lambda.Architecture.ARM_64,
                            memory_size=512,
                            timeout=Duration.minutes(3),
                            handler="index.lambda_handler",
                            # Without active tracing Lambda marks each request as not sampled, and the AWS SDK
                            # passes that decision on to the AgentCore Runtime, which then records no trace for
                            # the question. With active tracing Lambda makes the sampling decision itself: the
                            # first request each second and 5% of the others, a rate that cannot be configured.
                            tracing=_lambda.Tracing.ACTIVE,
                            **vpc_config,
                            environment={
                                "AGENT_RUNTIME_ARN": qna_agent_runtime.agent_runtime_arn,
                            },
                        )

            # Besides the X-Ray write permissions that active tracing adds, the proxy only needs to invoke the
            # AgentCore Runtime (the WebSocket grant is added with the API below)
            qna_bot_lambda.add_to_role_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock-agentcore:InvokeAgentRuntime"],
                resources=[
                    qna_agent_runtime.agent_runtime_arn,
                    f"{qna_agent_runtime.agent_runtime_arn}/*",
                ],
            ))

            CfnOutput(self, "AgentCoreRuntimeArn", value=qna_agent_runtime.agent_runtime_arn,
                       description="Bedrock AgentCore Runtime ARN for the QnA Strands Agent")

        else:
            # Classic mode: the Lambda calls the Bedrock RetrieveAndGenerate API directly
            qna_bot_lambda = _lambda.Function(self,
                            "qna_bot_lambda",
                            code=_lambda.Code.from_asset("./lambda/qna_bot_classic"),
                            runtime=_lambda.Runtime.PYTHON_3_12,
                            architecture=_lambda.Architecture.ARM_64,
                            memory_size=512,
                            timeout=Duration.minutes(1),
                            handler="index.lambda_handler",
                            **vpc_config,
                            environment={
                                "KB_ID": knowledge_base.attr_knowledge_base_id,
                                "QnA_MODEL_ARN": bedrock_model_arn(self, qna_model_id),
                                "GUARDRAIL_ID": qna_bot_guardrail.attr_guardrail_id,
                                "GUARDRAIL_VERSION": qna_bot_guardrail_version.attr_version,
                                "KB_MAX_RESULTS": KB_MAX_RESULTS,
                            },
                        )
            qna_bot_lambda.add_to_role_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
                resources=bedrock_claude_iam_resources(self, qna_model_id),
            ))
            # RetrieveAndGenerate resolves the inference profile before it invokes the model
            qna_bot_lambda.add_to_role_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:GetInferenceProfile"],
                resources=[bedrock_inference_profile_iam_resource(self, qna_model_id)],
            ))
            qna_bot_lambda.add_to_role_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
                resources=[knowledge_base.attr_knowledge_base_arn],
            ))
            qna_bot_lambda.add_to_role_policy(iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["bedrock:ApplyGuardrail"],
                resources=[qna_bot_guardrail.attr_guardrail_arn],
            ))

        CfnOutput(self, "QnABotMode", value=qna_bot_mode, description="QnA Bot deployment mode (strands or classic)")
        
        ######################### QnA WEB SOCKET #########################
        ######## Integration
        qna_ws_authorizer = authorizersv2.WebSocketLambdaAuthorizer("QnAWSAuthorizer", jwt_auth_qna_lambda, identity_source=[ "route.request.header.Authorization" ])
        qna_ws_connect_integration = integrationsv2.WebSocketLambdaIntegration("QnAWSConnectIntegration", qna_ws_connect_lambda)
        qna_ws_disconnect_integration = integrationsv2.WebSocketLambdaIntegration("QnAWSDisconnectIntegration", qna_ws_disconnect_lambda)
        qna_ws_default_integration = integrationsv2.WebSocketLambdaIntegration("QnAWSDefaultIntegration", qna_ws_default_lambda)
        qna_bot_integration = integrationsv2.WebSocketLambdaIntegration("QnABotIntegration", qna_bot_lambda)

        qna_ws_api=apigwv2.WebSocketApi(self, "QnAWSApi",
            api_name="QnAWSApi",
            description="WebSocket API for QnA Bot",
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=qna_ws_connect_integration,
                authorizer=qna_ws_authorizer
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=qna_ws_disconnect_integration,
            ),
            default_route_options=apigwv2.WebSocketRouteOptions(
                integration=qna_ws_default_integration,
            )
        )

        ######## Message Route
        qna_ws_api.add_route("qnaBot",
                            integration=qna_bot_integration,
                            return_response=True, # If true this will return lambda response in via websocket
                            )

        ######## API Stage
        qna_ws_stage = apigwv2.WebSocketStage(
            self, "QnAWSApiStage",
            web_socket_api=qna_ws_api,
            stage_name="dev",  # Change this based on the environment (e.g., "prod")
            auto_deploy=True,
        )

        # Add environment variables to manage WebSocket connections
        jwt_auth_qna_lambda.add_environment("WEBSOCKET_API_ID", qna_ws_api.api_id)

        ######## Permissions
        # Grant permissions for Lambda to manage the WebSocket connection (for sending messages back to clients)
        qna_ws_api.grant_manage_connections(qna_ws_connect_lambda)
        qna_ws_api.grant_manage_connections(qna_ws_disconnect_lambda)
        qna_ws_api.grant_manage_connections(qna_ws_default_lambda)
        qna_ws_api.grant_manage_connections(qna_bot_lambda)

        ######################### Cloud Front Distribution #########################
        cloudfront_waf_acl_arn = Fn.import_value("CloudFrontWafAclArn")
        qna_ws_domain_name = f"{qna_ws_api.api_id}.execute-api.{self.region}.amazonaws.com"

        # Creating http origin for CF behaviour
        http_origin = origins.HttpOrigin(domain_name=qna_ws_domain_name, 
                                        origin_path="/dev",
                                        protocol_policy=cloudfront.OriginProtocolPolicy.HTTPS_ONLY,
                                        https_port=443,
                                        origin_ssl_protocols=[cloudfront.OriginSslPolicy.TLS_V1_2],
                                        )
        
        # Creating CF behviour
        cloudfront_behavior = cloudfront.BehaviorOptions(origin=http_origin,
                                                        viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.HTTPS_ONLY, #this is must for WebSocket
                                                        cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                                                        smooth_streaming=True,
                                                        origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER, #recommended for API
                                                        allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                                            )
        
        # Creating the CloudFront distribution
        # minimum_protocol_version only takes effect once a custom domain with an ACM certificate is configured.
        # With the default *.cloudfront.net certificate used here, CloudFront applies the TLSv1 security policy,
        # so viewers can still negotiate TLS 1.0/1.1.
        qna_cloudfront_dist=cloudfront.Distribution(self, 
                                    "qnaWSCloudFrontDist",
                                    default_behavior=cloudfront_behavior,
                                    web_acl_id=cloudfront_waf_acl_arn,
                                    minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
                                    enable_logging=True,
                                    log_bucket=access_logs_bucket,
                                    log_file_prefix="qnaWSCloudFrontDist-logs"
                                )

        CfnOutput(self, "QnACloudFrontDomainName", export_name="QnACloudFrontDomainName", value=qna_cloudfront_dist.domain_name)

        ######################### CDK Nag Suppression #########################
        NagSuppressions.add_resource_suppressions([qna_ws_connect_lambda.role, qna_ws_disconnect_lambda.role, qna_ws_default_lambda.role ,qna_bot_lambda.role, 
                                                   opensearch_index_cust_res_lambda.role, kb_sync_lambda.role, kb_role,
                                                   jwt_auth_qna_lambda.role],
                            suppressions=[{
                                                "id": "AwsSolutions-IAM4",
                                                "reason": "AWSLambdaBasicExecutionRole and AWSLambdaVPCAccessExecutionRole are the AWS managed policies that give Lambda its CloudWatch Logs and VPC ENI permissions; they have no resource-level scoping to narrow.",
                                            },
                                            {
                                                "id": "AwsSolutions-IAM5",
                                                "reason": "Wildcards are limited to: s3:Get*/List* on objects of the single KB bucket (<bucket>/*) for the Knowledge Base role, <table>/index/* from grant_read_write_data on the connections table, execute-api @connections/* on this WebSocket API only, foundation-model/anthropic.claude-* in any Region (a cross-region inference profile invokes the model in its destination Regions) plus the inference-profile prefix derived from qna_model_id so the model version can change without an IAM change, the AgentCore Runtime ARN/* endpoint qualifier, and Resource * for xray:PutTraceSegments and xray:PutTelemetryRecords of the proxy Lambda in Strands mode (active tracing; these AWS X-Ray actions do not support resource-level permissions).",
                                            }
                                        ],
                            apply_to_children=True)
        
        ## CDK NAG suppression
        NagSuppressions.add_resource_suppressions_by_path(            
            self,
            path=["/QnAStack/Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource",
                  "/QnAStack/Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/Resource"],
            suppressions = [
                            { "id": "AwsSolutions-L1", "reason": "The BucketDeployment singleton Lambda runtime is fixed by the aws-cdk-lib BucketDeployment construct and cannot be set from this stack." },
                            {"id": "AwsSolutions-IAM5","reason": "The BucketDeployment service role needs s3:GetObject*/List* on the CDK asset bucket and s3:*Object on final-course-content/* of the KB bucket to copy the sample dataset.",}
                        ],
            apply_to_children=True
        )

        # CDK NAG suppression
        NagSuppressions.add_stack_suppressions(self, 
                                        [
                                            {
                                                "id": "AwsSolutions-IAM4",
                                                "reason": "Lambda execution policy for custom resources created by higher level CDK constructs",
                                                "appliesTo": [
                                                        "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                                                    ],
                                            },
                                        ])
        
        ## CDK NAG suppression
        NagSuppressions.add_resource_suppressions_by_path(self,
                                path="/QnAStack/CustomResourceIndexCreator/framework-onEvent/ServiceRole/DefaultPolicy/Resource",
                                suppressions=[{
                                                                "id": "AwsSolutions-IAM5",
                                                                "reason": "The Provider framework Lambda invokes the index creator function by ARN and ARN:* (version qualifier), as generated by the custom_resources.Provider construct.",
                                                            }
                                                        ]
                                                    )
        
        # CDK NAG suppression
        NagSuppressions.add_resource_suppressions([qna_ws_api],
                            suppressions=[{
                                                "id": "AwsSolutions-APIG1",
                                                "reason": "Access logging on the dev WebSocket stage is left disabled for this sample; request logging is captured by the CloudFront standard logs written to the access-logs bucket.",
                                            },
                                            {
                                                "id": "AwsSolutions-APIG4",
                                                "reason": "Lambda Authorizer with Cognito is on $connect route and it's not applicable for other routes.",
                                            },
                                        ],
                            apply_to_children=True)
        
        # CDK NAG suppression
        NagSuppressions.add_resource_suppressions([qna_ws_stage],
                            suppressions=[{
                                                "id": "AwsSolutions-APIG1",
                                                "reason": "Access logging on the dev WebSocket stage is left disabled for this sample; request logging is captured by the CloudFront standard logs written to the access-logs bucket.",
                                            },
                                        ],
                            apply_to_children=True)
        
        # CDK NAG suppression
        NagSuppressions.add_resource_suppressions([qna_cloudfront_dist],
                            suppressions=[{
                                                "id": "AwsSolutions-CFR1",
                                                "reason": "Geo restriction is enforced by the GeoMatch rule of the CloudFront web ACL from CloudFrontWAFStack instead of the distribution's own geo restriction setting.",
                                            },
                                            {
                                                "id": "AwsSolutions-CFR4",
                                                "reason": "The sample has no custom domain, so the distribution uses the default *.cloudfront.net certificate. With that certificate CloudFront applies the TLSv1 security policy, so viewers can still negotiate TLS 1.0/1.1; the TLSv1.2_2021 minimum protocol version only takes effect once a custom domain with an ACM certificate is configured.",
                                            },
                                        ],
                            apply_to_children=True)

        # CDK NAG suppression for custom resource Lambda runtime
        NagSuppressions.add_resource_suppressions_by_path(self,
                                path="/QnAStack/CustomResourceIndexCreator/framework-onEvent/Resource",
                                suppressions=[{
                                                "id": "AwsSolutions-L1",
                                                "reason": "The Provider framework Lambda runtime is set by the aws-cdk-lib custom_resources.Provider construct and is not configurable from this stack.",
                                            }])

        # CDK NAG suppression for Lambda runtime version (Python 3.12 is latest stable supported)
        NagSuppressions.add_stack_suppressions(self,
                                [
                                    {
                                        "id": "AwsSolutions-L1",
                                        "reason": "Python 3.12 is the latest runtime version supported by all Lambda layers and dependencies used in this solution.",
                                    },
                                ])

        # CDK NAG suppressions for AgentCore Runtime resources (only when strands mode is active)
        if qna_bot_mode == "strands":
            NagSuppressions.add_resource_suppressions([qna_agent_runtime.role],
                                suppressions=[{
                                    "id": "AwsSolutions-IAM5",
                                    "reason": "The Runtime construct generates log-group:* for logs:DescribeLogGroups (a list action), log-group:/aws/bedrock-agentcore/runtimes/* and log-stream:* for the other CloudWatch Logs actions, Resource * for X-Ray and cloudwatch:PutMetricData (namespace-conditioned), workload-identity/* for workload identity tokens, and s3:GetBucket*/GetObject*/List* on the CDK asset bucket and <asset bucket>/* to read the code artifact; the model statement uses foundation-model/anthropic.claude-* in any Region (arn:aws:bedrock:*::, because a cross-region inference profile invokes the model in its destination Regions) plus the inference-profile prefix derived from qna_model_id; logs:PutResourcePolicy for span delivery is limited to log-group:/aws/bedrock-agentcore/runtimes/qna_strands_agent-*, because the runtime id that completes the log group name is generated.",
                                }],
                                apply_to_children=True)

        ######################### Solution user agent #########################
        # The code of the Lambda functions cannot import solution.py: it reads the solution user-agent
        # suffix for its AWS SDK clients from this variable (see "Solution ID and usage reporting" in the
        # README). Every Lambda function of this stack whose code creates an AWS SDK client gets it, so add
        # a new function to this list when its code calls AWS. The authorizer and the index creator create
        # no client.
        solution_user_agent = user_agent_string()
        if solution_user_agent:
            for function in (qna_ws_connect_lambda, qna_ws_disconnect_lambda, qna_ws_default_lambda,
                             kb_sync_lambda, qna_bot_lambda):
                function.add_environment("USER_AGENT_STRING", solution_user_agent)

        ######################### Log Groups #########################
        # Keep this as the last statement: only the functions that exist at this point, including the ones
        # CDK creates for custom resources, get a log group
        own_lambda_log_groups(self)
