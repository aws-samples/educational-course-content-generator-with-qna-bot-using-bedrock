## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
"""
Proxy Lambda for Bedrock AgentCore Runtime invocation.

This Lambda function bridges the WebSocket API Gateway to the Bedrock AgentCore
Runtime hosting the Strands Agent QnA bot. It receives student questions via
WebSocket, forwards them to AgentCore, and returns the response.
"""
import json
import os
import uuid
import logging
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    logger.info("Received event: %s", json.dumps(event))

    try:
        body = json.loads(event["body"])
    except (KeyError, json.JSONDecodeError, TypeError) as e:
        logger.error("Invalid request body: %s", str(e))
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "Invalid request. Please provide a JSON body with user_question."}),
        }

    user_question = body.get("user_question", "")
    if not user_question:
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "user_question is required."}),
        }

    agent_runtime_arn = os.getenv("AGENT_RUNTIME_ARN", "")
    region = os.getenv("AWS_REGION", "us-east-1")

    # Build the payload for AgentCore Runtime
    # KB_ID and guardrail config are set as env vars on the AgentCore Runtime itself
    payload = {
        "user_question": user_question,
        "course_name": body.get("course_name"),
        "learning_objective": body.get("learning_objective"),
        "course_id": body.get("course_id"),
        "week_number": body.get("week_number"),
    }

    # Generate a session ID if not provided (must be 33+ chars for AgentCore)
    session_id = body.get("session_id") or (str(uuid.uuid4()) + "-" + str(uuid.uuid4())[:8])

    try:
        agentcore_client = boto3.client("bedrock-agentcore", region_name=region)

        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=agent_runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload),
            qualifier="DEFAULT",
        )

        response_body = response["response"].read()
        response_data = json.loads(response_body)
        output_text = response_data.get("bot_response", str(response_data))

        logger.info("AgentCore response received successfully")

        return {
            "statusCode": 200,
            "body": json.dumps({"bot_response": output_text}),
        }

    except Exception as e:
        logger.error("Error invoking AgentCore Runtime: %s", str(e))
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"Failed to invoke AgentCore Runtime: {str(e)}"}),
        }
