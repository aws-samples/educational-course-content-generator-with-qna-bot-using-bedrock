# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
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
    aws_lambda_event_sources as lambda_event_sources,
    aws_sqs as sqs,
    aws_ec2 as ec2,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_kms as kms,
    aws_logs as logs,
)

from constructs import Construct
from cdk_nag import NagSuppressions
import json

from educational_course_content_generator_with_qna_bot_using_bedrock.solution import user_agent_string
from educational_course_content_generator_with_qna_bot_using_bedrock.stack_logging import LOG_RETENTION, own_lambda_log_groups

# Model ids starting with one of these prefixes are cross-region inference profiles, not foundation models.
INFERENCE_PROFILE_PREFIXES = ("us", "eu", "au", "jp", "apac", "global")


def bedrock_model_arn(scope, model_id):
    prefix = model_id.split(".")[0]
    if prefix in INFERENCE_PROFILE_PREFIXES:
        return f"arn:aws:bedrock:{scope.region}:{scope.account}:inference-profile/{model_id}"
    return f"arn:aws:bedrock:{scope.region}::foundation-model/{model_id}"


def bedrock_claude_iam_resources(scope, model_id):
    """IAM resources needed to invoke Anthropic Claude models directly or through the configured inference profile."""
    prefix = model_id.split(".")[0]
    if prefix not in INFERENCE_PROFILE_PREFIXES:
        prefix = "us"
    return [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        f"arn:aws:bedrock:{scope.region}:{scope.account}:inference-profile/{prefix}.anthropic.claude-*",
    ]


class CourseStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        with open("project_config.json", "r") as file:
            variables = json.load(file)

        model_id = variables["model_id"]

        # Create a VPC (if you don"t already have one)
        public_subnet = ec2.SubnetConfiguration(
            name="PublicSubnet", 
            subnet_type=ec2.SubnetType.PUBLIC, 
            cidr_mask=variables["vpc"]["cidr_mask"]
        )
        private_subnet = ec2.SubnetConfiguration(
            name="PrivateSubnet", 
            subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS, 
            cidr_mask=variables["vpc"]["cidr_mask"]
        )
        # The flow log group is owned by the stack: it has a retention period and is deleted by `cdk destroy`
        vpc_flow_log_group = logs.LogGroup(
            self,
            "VpcFlowLogGroup",
            retention=LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )
        vpc = ec2.Vpc(
            scope=self,
            id="DemoVPC",
            ip_addresses=ec2.IpAddresses.cidr(variables["vpc"]["cidr_range"]),
            subnet_configuration=[public_subnet, private_subnet],
            flow_logs={
                "cloudwatch":ec2.FlowLogOptions(
                    destination=ec2.FlowLogDestination.to_cloud_watch_logs(vpc_flow_log_group)
             )
            }
        )

        # Expose VPC for cross-stack references
        self.vpc = vpc

        # Create an S3 VPC Endpoint
        s3_endpoint = ec2.GatewayVpcEndpoint(
            self,
            "S3VpcEndpoint",
            vpc=vpc,
            service=ec2.GatewayVpcEndpointAwsService.S3
        )

        # Create the access logs bucket
        # amazonq-ignore-next-line
        access_logs_bucket = s3.Bucket(
            self,
            "AccessLogsBucket",
            removal_policy=RemovalPolicy.DESTROY,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            auto_delete_objects=True,
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,
        )

        # Grant CloudFront permission to write logs to the bucket
        access_logs_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowCloudFrontLogDelivery",
                actions=["s3:PutObject"],
                principals=[iam.ServicePrincipal("delivery.logs.amazonaws.com")],
                resources=[access_logs_bucket.arn_for_objects("*")],
                # Confused-deputy protection: only log deliveries created in this account may write here
                conditions={
                    "StringEquals": {"aws:SourceAccount": self.account},
                    "ArnLike": {"aws:SourceArn": f"arn:aws:logs:{self.region}:{self.account}:delivery-source:*"},
                },
            )
        )

        # Create the input bucket
        input_bucket_s3 = s3.Bucket(
            self,
            "InputBucket",
            removal_policy=RemovalPolicy.DESTROY,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            server_access_logs_bucket=access_logs_bucket,
            enforce_ssl=True,
            auto_delete_objects=True,
        )
        # Create the output bucket
        output_bucket_s3 = s3.Bucket(
            self,
            "OutputBucket",
            removal_policy=RemovalPolicy.DESTROY,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            server_access_logs_bucket=access_logs_bucket,
            enforce_ssl=True,
            auto_delete_objects=True,
        )
        # Create the KB bucket
        kb_bucket_s3 = s3.Bucket(
            self,
            "KBBucket",
            removal_policy=RemovalPolicy.DESTROY,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            server_access_logs_bucket=access_logs_bucket,
            enforce_ssl=True,
            auto_delete_objects=True,
        )
        # Cfnoutput the bucket
        CfnOutput(self, "AccessLogsBucketName", export_name="AccessLogsBucketName", value=access_logs_bucket.bucket_name)
        CfnOutput(self, "KBBucketName", export_name="KBBucketName", value=kb_bucket_s3.bucket_name)
        CfnOutput(self, "InputBucketName", export_name="InputBucketName", value=input_bucket_s3.bucket_name)
        CfnOutput(self, "OutputBucketName", export_name="OutputBucketName", value=output_bucket_s3.bucket_name)

        # # Add a policy to allow access through the VPC Endpoint for the access logs bucket
        # access_logs_bucket.add_to_resource_policy(
        #     iam.PolicyStatement(
        #         effect=iam.Effect.DENY,
        #         actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
        #         resources=[access_logs_bucket.bucket_arn, f"{access_logs_bucket.bucket_arn}/*"],
        #         principals=[iam.AnyPrincipal()],
        #         conditions={
        #             "StringNotEquals": {
        #                 "aws:SourceVpce": s3_endpoint.vpc_endpoint_id
        #             }
        #         }
        #     )
        # )

        # # Add a policy to input bucket to allow access through the VPC Endpoint
        # input_bucket_s3.add_to_resource_policy(
        #     iam.PolicyStatement(
        #         effect=iam.Effect.DENY,
        #         actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
        #         resources=[input_bucket_s3.bucket_arn, f"{input_bucket_s3.bucket_arn}/*"],
        #         principals=[iam.AnyPrincipal()],
        #         conditions={
        #             "StringNotEquals": {
        #                 "aws:SourceVpce": s3_endpoint.vpc_endpoint_id
        #             }
        #         }
        #     )
        # )

        # # Add a policy to output bucket to allow access through the VPC Endpoint
        # output_bucket_s3.add_to_resource_policy(
        #     iam.PolicyStatement(
        #         effect=iam.Effect.DENY,
        #         actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
        #         resources=[output_bucket_s3.bucket_arn, f"{output_bucket_s3.bucket_arn}/*"],
        #         principals=[iam.AnyPrincipal()],
        #         conditions={
        #             "StringNotEquals": {
        #                 "aws:SourceVpce": s3_endpoint.vpc_endpoint_id
        #             }
        #         }
        #     )
        # )
        
        # # Add a policy to KB bucket to allow access through the VPC Endpoint
        # kb_bucket_s3.add_to_resource_policy(
        #     iam.PolicyStatement(
        #         effect=iam.Effect.DENY,
        #         actions=["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
        #         resources=[kb_bucket_s3.bucket_arn, f"{kb_bucket_s3.bucket_arn}/*"],
        #         principals=[iam.AnyPrincipal()],
        #         conditions={
        #             "StringNotEquals": {
        #                 "aws:SourceVpce": s3_endpoint.vpc_endpoint_id
        #             }
        #         }
        #     )
        # )


        ######################### Connection DDB Table  #########################
        course_connections_ddb_table = dynamodb.Table(self, "CourseConnectionsTable",
                        partition_key=dynamodb.Attribute(name="connectionId", type=dynamodb.AttributeType.STRING),
                        time_to_live_attribute="ttl",
                        billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
                        encryption=dynamodb.TableEncryption.AWS_MANAGED,
                        point_in_time_recovery=True,
                        removal_policy=RemovalPolicy.DESTROY
        )
        ######################### Cognito User Pool & Application Client #########################
        user_pool = cognito.UserPool(
            self, "CourseUserPool",
            user_pool_name="CourseUserPool",
            # Only an administrator can create users (scripts/create_cognito_user.py and the demo call
            # AdminCreateUser). With self sign-up, anyone who knows the app client ID could create accounts
            # in the user pool. The verification message below is not used by the sample: the pool verifies
            # no attribute automatically.
            self_sign_up_enabled=False,
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            user_verification=cognito.UserVerificationConfig(
                email_subject="Verify your email for outline and content generation App",
                email_body="Hello {username}, Thanks for signing up to Course outline and content generation App! Your verification code is {####}",
                email_style=cognito.VerificationEmailStyle.CODE,
            ),
            standard_attributes={"fullname": cognito.StandardAttribute(required=True, mutable=True)},
            # MFA can be enrolled (TOTP) but is not required; set Mfa.REQUIRED before production use
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            password_policy=cognito.PasswordPolicy(
                min_length=8,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Create a new Cognito User Pool Client
        user_pool_client = user_pool.add_client("CourseUserPoolAppClient",
                                                user_pool_client_name="CourseUserPoolAppClient",
                                                id_token_validity=Duration.days(1),
                                                access_token_validity=Duration.days(1),
                                                auth_flows=cognito.AuthFlow(user_password=True)
                                                )
        # CfnOutput userpool arn and user pool client id
        CfnOutput(self, "UserPoolArn", export_name="UserPoolArn", value=user_pool.user_pool_arn)
        CfnOutput(self, "UserPoolClientId", export_name="UserPoolClientId", value=user_pool_client.user_pool_client_id)

        ######################### Lambda Layers  #########################
        langchain_core_layer =_alambda.PythonLayerVersion(self, "langchain-core-layer",
            entry="./lambda/lambda_layer/langchain_core_layer/",
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )

        pypdf2_layer = _alambda.PythonLayerVersion(self, "pypdf2-layer",
            entry="./lambda/lambda_layer/pypdf2_layer/",
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )

        cryptography_layer =_alambda.PythonLayerVersion(self, "cryptography-layer",
            entry="./lambda/lambda_layer/cryptography_layer/",
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )

        pyJWT_layer = _alambda.PythonLayerVersion(self, "pyJWT-layer",
            entry="./lambda/lambda_layer/pyJWT_layer/",
            compatible_runtimes=[_lambda.Runtime.PYTHON_3_12],
            compatible_architectures=[_lambda.Architecture.ARM_64],
        )
        CfnOutput(self, "CryptographyLayerArn", export_name="CryptographyLayerArn", value=cryptography_layer.layer_version_arn)
        CfnOutput(self, "PyJWTLayerArn", export_name="PyJWTLayerArn", value=pyJWT_layer.layer_version_arn)

        ######################### Lambda Functions for WebSocket #########################
        course_ws_connect_lambda = _lambda.Function(
            self, "CourseWSConnect",
            code=_lambda.Code.from_asset("./lambda/connect"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            timeout=Duration.seconds(30),
            handler="index.lambda_handler",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            environment={
                "CONNECTIONS_TABLE": course_connections_ddb_table.table_name
            },
        )
        course_connections_ddb_table.grant_read_write_data(course_ws_connect_lambda)

        course_ws_disconnect_lambda = _lambda.Function(
            self, "CourseWSDisconnect",
            code=_lambda.Code.from_asset("./lambda/disconnect"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            timeout=Duration.seconds(30),
            handler="index.lambda_handler",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            environment={
                "CONNECTIONS_TABLE": course_connections_ddb_table.table_name
            },
        )
        course_connections_ddb_table.grant_read_write_data(course_ws_disconnect_lambda)

        course_ws_default_lambda = _lambda.Function(
            self, "CourseWSDefault",
            code=_lambda.Code.from_asset("./lambda/default"),
            runtime=_lambda.Runtime.PYTHON_3_12,
            architecture=_lambda.Architecture.ARM_64,
            memory_size=512,
            timeout=Duration.seconds(30),
            handler="index.lambda_handler",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            environment={
                "CONNECTIONS_TABLE": course_connections_ddb_table.table_name
            },
        )
        course_connections_ddb_table.grant_read_write_data(course_ws_default_lambda)

        jwt_auth_course_lambda = _lambda.Function(self, 
                                "jwt_auth_course_lambda",
                                code=_lambda.Code.from_asset("./lambda/jwt_auth"),
                                runtime=_lambda.Runtime.PYTHON_3_12,
                                architecture=_lambda.Architecture.ARM_64,
                                memory_size=512,
                                timeout=Duration.seconds(10),
                                handler="index.lambda_handler",
                                layers=[cryptography_layer, pyJWT_layer],
                                vpc=vpc,
                                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                                environment={
                                        "API_REGION": self.region,
                                        "ACCOUNT_ID": self.account,
                                        "COGNITO_USER_POOL_ID": user_pool.user_pool_id,
                                        "COGNITO_APP_CLIENT_ID": user_pool_client.user_pool_client_id,
                                    },
                            )
        
        ######################### SQS and DLQ  #########################
        # Create a KMS key for encryption. `cdk destroy` schedules the key for deletion; it disappears after
        # the waiting period that KMS enforces (7 days is the minimum).
        kms_key = kms.Key(self, "SQSEncryptionKey",
            description="KMS key for SQS queue encryption",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
            pending_window=Duration.days(7),
        )
        
        outline_dlq = sqs.Queue(
            self,
            id="dead_letter_queue_outline_id",
            retention_period=Duration.days(7),
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        outine_dead_letter_queue = sqs.DeadLetterQueue(
            max_receive_count=1,
            queue=outline_dlq,
        )
        # Create outline SQS Queue
        outline_queue = sqs.Queue(
            self,
            "CourseOutlineQueue",
            receive_message_wait_time=Duration.seconds(0), #Time that the poller waits for new messages before returning a response
            visibility_timeout=Duration.minutes(5),  # This should be bingger than Lambda time out
            dead_letter_queue=outine_dead_letter_queue,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=kms_key,        
        )

        content_dlq = sqs.Queue(
            self,
            id="dead_letter_queue_content_id",
            retention_period=Duration.days(7),
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
        )
        content_dead_letter_queue = sqs.DeadLetterQueue(
            max_receive_count=1,
            queue=content_dlq,
        )
        # Create Content SQS Queue
        content_queue = sqs.Queue(
            self,
            "CourseContentQueue",
            receive_message_wait_time=Duration.seconds(0), #Time that the poller waits for new messages before returning a response
            visibility_timeout=Duration.minutes(5),  # This should be bingger than Lambda time out
            dead_letter_queue=content_dead_letter_queue,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY, 
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=kms_key,           
        )
        
        ######################### Course Outline Lambda #########################
        course_outline_ws_lambda = _lambda.Function(self, 
                                "course_outline_ws_lambda",
                                code=_lambda.Code.from_asset("./lambda/course_outline_ws"),
                                runtime=_lambda.Runtime.PYTHON_3_12,
                                architecture=_lambda.Architecture.ARM_64,
                                memory_size=512,
                                timeout=Duration.seconds(30),
                                handler="index.lambda_handler",
                                vpc=vpc,
                                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                                environment={
                                    "OUTLINE_QUEUE_URL":outline_queue.queue_url,
                                }
                            )
        outline_queue.grant_send_messages(course_outline_ws_lambda)
        kms_key.grant_encrypt_decrypt(course_outline_ws_lambda)

        course_outline_llm_lambda = _lambda.Function(self, 
                                "course_outline_llm_lambda",
                                code=_lambda.Code.from_asset("./lambda/course_outline_llm"),
                                runtime=_lambda.Runtime.PYTHON_3_12,
                                architecture=_lambda.Architecture.ARM_64,
                                memory_size=512,
                                timeout=Duration.minutes(3),
                                handler="index.lambda_handler",
                                layers=[langchain_core_layer, pypdf2_layer],
                                vpc=vpc,
                                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                                environment={
                                    "MODEL_ID":model_id,
                                    "INPUT_BUCKET_NAME":input_bucket_s3.bucket_name,
                                    "OUTPUT_BUCKET":output_bucket_s3.bucket_name,
                                }
                            )
        input_bucket_s3.grant_read_write(course_outline_llm_lambda)
        output_bucket_s3.grant_read_write(course_outline_llm_lambda)
        course_connections_ddb_table.grant_read_write_data(course_outline_llm_lambda)
        outline_queue.grant_consume_messages(course_outline_llm_lambda)
        bedrock_model_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=bedrock_claude_iam_resources(self, model_id),
        )
        course_outline_llm_lambda.add_to_role_policy(bedrock_model_policy_statement)

        # This event will be triggered by SQS when a new message is received
        invoke_event_outline = lambda_event_sources.SqsEventSource(outline_queue, 
                                                                   batch_size=1, 
                                                                   max_batching_window=Duration.seconds(0)
                                                                   )
        course_outline_llm_lambda.add_event_source(invoke_event_outline)


        ########################## Course Content Lambda #########################
        course_content_ws_lambda = _lambda.Function(self, 
                                "course_content_ws_lambda",
                                code=_lambda.Code.from_asset("./lambda/course_content_ws"),
                                runtime=_lambda.Runtime.PYTHON_3_12,
                                architecture=_lambda.Architecture.ARM_64,
                                memory_size=512,
                                timeout=Duration.minutes(1),
                                handler="index.lambda_handler",
                                vpc=vpc,
                                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                                environment={
                                     "CONTENT_QUEUE_URL":content_queue.queue_url,
                                }
                            )
        content_queue.grant_send_messages(course_content_ws_lambda)
        kms_key.grant_encrypt_decrypt(course_content_ws_lambda)

        course_content_llm_lambda = _lambda.Function(self, 
                                "course_content_llm_lambda",
                                code=_lambda.Code.from_asset("./lambda/course_content_llm"),
                                runtime=_lambda.Runtime.PYTHON_3_12,
                                architecture=_lambda.Architecture.ARM_64,
                                memory_size=512,
                                timeout=Duration.minutes(3),
                                handler="index.lambda_handler",
                                layers=[langchain_core_layer, pypdf2_layer],
                                vpc=vpc,
                                vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
                                environment={
                                    "MODEL_ID":model_id,
                                    "INPUT_BUCKET_NAME":input_bucket_s3.bucket_name,
                                    "OUTPUT_BUCKET":output_bucket_s3.bucket_name,
                                }
                            )
        input_bucket_s3.grant_read_write(course_content_llm_lambda)
        output_bucket_s3.grant_read_write(course_content_llm_lambda)
        course_connections_ddb_table.grant_read_write_data(course_content_llm_lambda)
        content_queue.grant_consume_messages(course_content_llm_lambda)
        bedrock_model_policy_statement = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            resources=bedrock_claude_iam_resources(self, model_id),
        )
        course_content_llm_lambda.add_to_role_policy(bedrock_model_policy_statement)

        # This event will be triggered by SQS when a new message is received
        invoke_event_content = lambda_event_sources.SqsEventSource(content_queue, 
                                                                   batch_size=1, 
                                                                   max_batching_window=Duration.seconds(0)
                                                                   )
        course_content_llm_lambda.add_event_source(invoke_event_content)

        ######################### COURSE WEB SOCKET #########################
        course_ws_authorizer = authorizersv2.WebSocketLambdaAuthorizer("CourseWSAuthorizer", jwt_auth_course_lambda, identity_source=["route.request.header.Authorization",]) # "route.request.querystring.Authorization", 
        course_ws_connect_integration = integrationsv2.WebSocketLambdaIntegration("CourseWSConnectIntegration", course_ws_connect_lambda)
        course_ws_disconnect_integration = integrationsv2.WebSocketLambdaIntegration("CourseWSDisconnectIntegration", course_ws_disconnect_lambda)
        course_ws_default_integration = integrationsv2.WebSocketLambdaIntegration("CourseWSDefaultIntegration", course_ws_default_lambda)
        course_outline_ws_integration = integrationsv2.WebSocketLambdaIntegration("CourseOutlineIntegration", course_outline_ws_lambda)
        course_content_ws_integration = integrationsv2.WebSocketLambdaIntegration("CourseContentIntegration", course_content_ws_lambda)

        course_ws_api=apigwv2.WebSocketApi(self, "CourseWSApi",
            api_name="CourseWSApi",
            description="WebSocket API for Course Outline and Content Generation",
            connect_route_options=apigwv2.WebSocketRouteOptions(
                integration=course_ws_connect_integration,
                authorizer=course_ws_authorizer
            ),
            disconnect_route_options=apigwv2.WebSocketRouteOptions(
                integration=course_ws_disconnect_integration,
            ),
            default_route_options=apigwv2.WebSocketRouteOptions(
                integration=course_ws_default_integration,
            )
        )

        # Add a custom message route, to generate course outline
        course_ws_api.add_route("courseOutline",
                                integration=course_outline_ws_integration,
                                # return_response=True, # If true this will return lambda response in via websocket
                                )
        
        # Add a custom message route, to generate course content
        course_ws_api.add_route("courseContent",
                                integration=course_content_ws_integration,
                                # return_response=True, # If true this will return lambda response in via websocket
                                )
        
        # Create a WebSocket API stage (usually, "dev" or "prod")
        course_ws_stage = apigwv2.WebSocketStage(
            self, "CourseWSApiStage",
            web_socket_api=course_ws_api,
            stage_name="dev",  # Change this based on the environment (e.g., "prod")
            auto_deploy=True,
        )
        ws_endpoint_url = f"https://{course_ws_api.api_id}.execute-api.{self.region}.amazonaws.com/{course_ws_stage.stage_name}"

        # Add environment variables to manage WebSocket connections
        course_outline_ws_lambda.add_environment("WEBSOCKET_ENDPOINT_URL", ws_endpoint_url)
        course_outline_llm_lambda.add_environment("WEBSOCKET_ENDPOINT_URL", ws_endpoint_url)

        course_content_ws_lambda.add_environment("WEBSOCKET_ENDPOINT_URL", ws_endpoint_url)
        course_content_llm_lambda.add_environment("WEBSOCKET_ENDPOINT_URL", ws_endpoint_url)

        jwt_auth_course_lambda.add_environment("WEBSOCKET_API_ID", course_ws_api.api_id)

        ######################### Permissions #########################
        # Grant permissions for Lambda to manage the WebSocket connection (for sending messages back to clients)
        course_ws_api.grant_manage_connections(course_ws_connect_lambda)
        course_ws_api.grant_manage_connections(course_ws_disconnect_lambda)
        course_ws_api.grant_manage_connections(course_ws_default_lambda)
        course_ws_api.grant_manage_connections(course_outline_ws_lambda)
        course_ws_api.grant_manage_connections(course_outline_llm_lambda)
        course_ws_api.grant_manage_connections(course_content_ws_lambda)
        course_ws_api.grant_manage_connections(course_content_llm_lambda)

        ######################### Outputs #########################
        CfnOutput(self, "CourseWSApiId", export_name="CourseWSApiId",  value=course_ws_api.api_id)
        CfnOutput(self, "CourseWSApiEndpoint", export_name="CourseWSApiEndpoint",  value=course_ws_api.api_endpoint)

        ######################### Cloud Front Distribution #########################
        cloudfront_waf_acl_arn = Fn.import_value("CloudFrontWafAclArn")
        course_ws_domain_name = f"{course_ws_api.api_id}.execute-api.{self.region}.amazonaws.com"

        # Creating http origin for CF behaviour
        http_origin = origins.HttpOrigin(domain_name=course_ws_domain_name, 
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
        course_cloudfront_dist=cloudfront.Distribution(self, 
                                    "courseWSCloudFrontDist",
                                    default_behavior=cloudfront_behavior,
                                    web_acl_id=cloudfront_waf_acl_arn,
                                    minimum_protocol_version=cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
                                    enable_logging=True,
                                    log_bucket=access_logs_bucket,
                                    log_file_prefix="courseWSCloudFrontDist-logs"
                                )

        CfnOutput(self, "CourseCloudFrontDomainName", export_name="CourseCloudFrontDomainName", value=course_cloudfront_dist.domain_name)

        ######################### CDK Nag Suppression #########################
        NagSuppressions.add_resource_suppressions([course_ws_connect_lambda.role, 
                                                   course_ws_disconnect_lambda.role, 
                                                   course_ws_default_lambda.role, 
                                                   course_outline_ws_lambda.role, 
                                                   course_outline_llm_lambda.role, 
                                                   course_content_ws_lambda.role, 
                                                   course_content_llm_lambda.role,
                                                   jwt_auth_course_lambda.role],
                            suppressions=[{
                                                "id": "AwsSolutions-IAM4",
                                                "reason": "AWSLambdaBasicExecutionRole and AWSLambdaVPCAccessExecutionRole are the AWS managed policies that give Lambda its CloudWatch Logs and VPC ENI permissions; they have no resource-level scoping to narrow.",
                                            },
                                            {
                                                "id": "AwsSolutions-IAM5",
                                                "reason": "Wildcards are limited to: s3:*Object*/List*/Abort* on objects of the input and output buckets (<bucket>/*), <table>/index/* from grant_read_write_data on the connections table, kms:GenerateDataKey*/ReEncrypt* on the single SQS key, execute-api @connections/* on this WebSocket API only, and foundation-model/anthropic.claude-* plus the inference-profile prefix derived from model_id so the model version can change without an IAM change.",
                                            }
                                        ],
                            apply_to_children=True)
        
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
        
        # CDK NAG suppression
        NagSuppressions.add_resource_suppressions([user_pool],
                            suppressions=[{
                                                "id": "AwsSolutions-COG2",
                                                "reason": "MFA is left optional (mfa not set to REQUIRED) because sample users are created by scripts/create_cognito_user.py and sign in with username and password from the demo client; set mfa=REQUIRED before any production use.",
                                            },
                                            {
                                                "id": "AwsSolutions-COG8",
                                                "reason": "Sample deployment; the Cognito Plus feature plan (threat protection) adds per-MAU cost and is left to adopters to enable.",
                                            },
                                        ],
                            apply_to_children=True)
        
        # CDK NAG suppression
        NagSuppressions.add_resource_suppressions([course_ws_api],
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
        NagSuppressions.add_resource_suppressions([course_ws_stage],
                            suppressions=[{
                                                "id": "AwsSolutions-APIG1",
                                                "reason": "Access logging on the dev WebSocket stage is left disabled for this sample; request logging is captured by the CloudFront standard logs written to the access-logs bucket.",
                                            },
                                        ],
                            apply_to_children=True)
        
        # CDK NAG suppression
        NagSuppressions.add_resource_suppressions([course_cloudfront_dist],
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

        # CDK NAG suppression for Lambda runtime version
        NagSuppressions.add_stack_suppressions(self,
                                [
                                    {
                                        "id": "AwsSolutions-L1",
                                        "reason": "Python 3.12 is the latest runtime version supported by all Lambda layers and dependencies used in this solution.",
                                    },
                                ])

        ######################### Solution user agent #########################
        # The code of the Lambda functions cannot import solution.py: it reads the solution user-agent
        # suffix for its AWS SDK clients from this variable (see "Solution ID and usage reporting" in the
        # README). Every Lambda function of this stack whose code creates an AWS SDK client gets it, so add
        # a new function to this list when its code calls AWS. The authorizer creates no client.
        solution_user_agent = user_agent_string()
        if solution_user_agent:
            for function in (course_ws_connect_lambda, course_ws_disconnect_lambda, course_ws_default_lambda,
                             course_outline_ws_lambda, course_outline_llm_lambda,
                             course_content_ws_lambda, course_content_llm_lambda):
                function.add_environment("USER_AGENT_STRING", solution_user_agent)

        ######################### Log Groups #########################
        # Keep this as the last statement: only the functions that exist at this point, including the ones
        # CDK creates for custom resources, get a log group
        own_lambda_log_groups(self)
