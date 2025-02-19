## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
from aws_cdk import (
    CfnOutput,
    Fn,
    RemovalPolicy,
    Stack,
    Duration,
    aws_iam as iam,
    aws_lambda as _lambda,
    aws_s3 as s3,
    aws_lambda_python_alpha as _alambda,
    aws_cognito as cognito,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as integrationsv2,
    aws_apigatewayv2_authorizers as authorizersv2,
    aws_dynamodb as dynamodb,
    CustomResource,
    aws_bedrock as bedrock,
    aws_opensearchserverless as aoss,
    aws_s3_deployment as s3_deployment,
    aws_s3_notifications as s3n,
    custom_resources,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)
from constructs import Construct
from cdk_nag import NagSuppressions
import json

class QnAStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        with open("project_config.json", "r") as file:
            variables = json.load(file)
        
        qna_model_id = variables["qna_model_id"]

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
        ######## Connect WebSocket Lambda
        qna_ws_connect_lambda = _lambda.Function(
            self, "QnAWSConnect",
            code=_lambda.Code.from_asset("./lambda/connect"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            handler="index.lambda_handler",
            timeout=Duration.seconds(30),
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
            handler="index.lambda_handler",
            timeout=Duration.seconds(30),
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
            handler="index.lambda_handler",
            timeout=Duration.seconds(30),
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
                               environment={
                                        "API_REGION": self.region,
                                        "ACCOUNT_ID": self.account,
                                        "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                                        "COGNITO_APP_CLIENT_ID": user_pool_client.user_pool_client_id,
                                    },
                            )

        ######## QnA Bot Lambda
        qna_bot_lambda = _lambda.Function(self, 
                        "qna_bot_lambda",
                        code=_lambda.Code.from_asset("./lambda/qna_bot"),
                        runtime=_lambda.Runtime.PYTHON_3_12,
                        architecture=_lambda.Architecture.ARM_64,
                        memory_size=512,
                        timeout=Duration.minutes(1),
                        handler="index.lambda_handler",
                    )
        
        ######################### QnA WEB SOCKET #########################
        ######## Integration
        qna_ws_authorizer = authorizersv2.WebSocketLambdaAuthorizer("QnAWSAuthorizer", jwt_auth_qna_lambda, identity_source=[ "route.request.header.Authorization" ]) # "route.request.querystring.Authorization",
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
        ws_endpoint_url = f"https://{qna_ws_api.api_id}.execute-api.{self.region}.amazonaws.com/{qna_ws_stage.stage_name}"

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
                                sources=[s3_deployment.Source.asset("./kb_dataset")],
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
        network_policy_document = json.dumps([{"Rules": [{"Resource": [f"collection/{course_collection.name}"],
                                                          "ResourceType": "dashboard"},
                                                         {"Resource": [f"collection/{course_collection.name}"],
                                                          "ResourceType": "collection"}],
                                               "AllowFromPublic": True}], separators=(",", ":"))
        network_policy = aoss.CfnSecurityPolicy(scope=self,
                                                         id="CollectionNetworkPolicy",
                                                         name="content-coll-network-policy",
                                                         description="Network policy for course content collection",
                                                         type="network",
                                                         policy=network_policy_document)
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
        kb_synch_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:StartIngestionJob"],
            resources=[knowledge_base.attr_knowledge_base_arn],
        )
        kb_sync_lambda.add_to_role_policy(kb_synch_policy_statement)

        ######## S3 Create/Remove event
        # EventBridge rule when a file is added or removed from the S3 bucket
        s3_notification_key_filter = s3.NotificationKeyFilter(
                                    prefix="final-course-content/",
                                    suffix=".metadata.json",
                                )
        kb_bucket.add_event_notification(s3.EventType.OBJECT_CREATED, s3n.LambdaDestination(kb_sync_lambda), s3_notification_key_filter)
        kb_bucket.add_event_notification(s3.EventType.OBJECT_REMOVED, s3n.LambdaDestination(kb_sync_lambda), s3_notification_key_filter)
        
        # Sync will only happen after deployment complete 
        bucket_deployment.node.add_dependency(kb_sync_lambda)

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
                    bedrock.CfnGuardrail.ContentFilterConfigProperty(
                        input_strength="NONE",
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
                    )
                ]
            ),

            word_policy_config=bedrock.CfnGuardrail.WordPolicyConfigProperty(
                managed_word_lists_config=[
                    bedrock.CfnGuardrail.ManagedWordsConfigProperty(
                        type="PROFANITY"
                    )
                ],
                words_config=[
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="fiduciary advice"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="investment recommendations"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="stock picks"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="financial planning guidance"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="portfolio allocation advice"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="retirement fund suggestions"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="wealth management tips"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="trust fund setup"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="investment strategy"
                    ),
                    bedrock.CfnGuardrail.WordConfigProperty(
                        text="financial advisor recommendations"
                    )
                ]
            ),
        )

        # Create a Guardrail version
        qna_bot_guardrail_version = bedrock.CfnGuardrailVersion(self, "QnABotGuardrailVersion",
            guardrail_identifier=qna_bot_guardrail.attr_guardrail_id,
            description="QnA Bot guardrail version",
        )

        ######################### QnA Bot Lambda KB configuration #########################
        qna_bot_lambda.add_environment("KB_ID", knowledge_base.attr_knowledge_base_id)
        qna_bot_lambda.add_environment("QnA_MODEL_ID", qna_model_id)
        qna_bot_lambda.add_environment("GUARDRAIL_ID", qna_bot_guardrail.attr_guardrail_id)
        qna_bot_lambda.add_environment("GUARDRAIL_VERSION", qna_bot_guardrail_version.attr_version)

        haiku_sonnet_bedrock_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=[f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-3-haiku*:0",
                       f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-3-sonnet*:0",
                       f"arn:aws:bedrock:{self.region}::foundation-model/anthropic.claude-3-5-sonnet*:0",
                       ]
        )
        kb_retrive_generate_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:Retrieve", "bedrock:RetrieveAndGenerate"],
            resources=[knowledge_base.attr_knowledge_base_arn],
        )
        guardrail_policy_statement = iam.PolicyStatement(
            actions=["bedrock:ApplyGuardrail"],
            resources=[qna_bot_guardrail.attr_guardrail_arn],
        )
        qna_bot_lambda.add_to_role_policy(haiku_sonnet_bedrock_policy_statement)
        qna_bot_lambda.add_to_role_policy(kb_retrive_generate_policy_statement)
        qna_bot_lambda.add_to_role_policy(guardrail_policy_statement)


        ######################### CDK Nag Suppression #########################
        NagSuppressions.add_resource_suppressions([qna_ws_connect_lambda.role, qna_ws_disconnect_lambda.role, qna_ws_default_lambda.role ,qna_bot_lambda.role, 
                                                   opensearch_index_cust_res_lambda.role, kb_sync_lambda.role, kb_role],
                            suppressions=[{
                                                "id": "AwsSolutions-IAM4",
                                                "reason": "This code is for demo purposes. So granted full access Claude Model from Bedrock service.",
                                            },
                                            {
                                                "id": "AwsSolutions-IAM5",
                                                "reason": "This code is for demo purposes. So granted access to all indices of S3 bucket.",
                                            }
                                        ],
                            apply_to_children=True)
        
        ## CDK NAG suppression
        NagSuppressions.add_resource_suppressions_by_path(            
            self,
            path=["/QnAStack/Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/ServiceRole/DefaultPolicy/Resource",
                  "/QnAStack/BucketNotificationsHandler050a0587b7544547bf325f094a3db834/Role/DefaultPolicy/Resource",
                  "/QnAStack/Custom::CDKBucketDeployment8693BB64968944B69AAFB0CC9EB8756C/Resource"],
            suppressions = [
                            { "id": "AwsSolutions-L1", "reason": "CDK BucketDeployment L1 Construct" },
                            {"id": "AwsSolutions-IAM5","reason": "This code is for demo purposes. So granted access to all indices of S3 bucket.",}
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
                                                                "reason": "Custom resource needs wildcard permissions to manage OpenSearch index.",
                                                            }
                                                        ]
                                                    )
        
        # CDK NAG suppression
        NagSuppressions.add_resource_suppressions([qna_ws_api],
                            suppressions=[{
                                                "id": "AwsSolutions-APIG1",
                                                "reason": "This code is for demo purposes. Suppressing access logging enabled.",
                                            },
                                            {
                                                "id": "AwsSolutions-APIG4",
                                                "reason": "Lambda Authorizer with Cognito is on $connect route and it's not applicable for other routes.",
                                            },
                                            {
                                                "id": "AwsSolutions-APIG6",
                                                "reason": "This code is for demo purposes. Suppressing CloudWatch logging enabled for all methods.",
                                            },
                                        ],
                            apply_to_children=True)
        
        # CDK NAG suppression
        NagSuppressions.add_resource_suppressions([qna_ws_stage],
                            suppressions=[{
                                                "id": "AwsSolutions-APIG1",
                                                "reason": "This code is for demo purposes. Suppressing access logging enabled.",
                                            },
                                        ],
                            apply_to_children=True)
        
        # CDK NAG suppression
        NagSuppressions.add_resource_suppressions([qna_cloudfront_dist],
                            suppressions=[{
                                                "id": "AwsSolutions-CFR4",
                                                "reason": "This code is for demo purposes. Certificate is not mandatory therefore the Cloudfront certificate will be used.",
                                            },
                                        ],
                            apply_to_children=True)