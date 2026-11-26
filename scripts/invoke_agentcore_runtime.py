#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Manually invoke the Bedrock AgentCore Runtime QnA Bot (Strands mode).

This is a command-line helper, not a pytest test. It first calls the deployed
AgentCore Runtime directly and then goes through the proxy Lambda that bridges
the WebSocket API to the runtime.

Usage:
    python scripts/invoke_agentcore_runtime.py
    python scripts/invoke_agentcore_runtime.py "What is supervised learning?"
    python scripts/invoke_agentcore_runtime.py "What is supervised learning?" --region us-east-1

Prerequisites:
    - AWS credentials configured (bedrock-agentcore:InvokeAgentRuntime, lambda:InvokeFunction,
      cloudformation:DescribeStacks and cloudformation:DescribeStackResources)
    - QnAStack deployed with qna_bot_mode="strands"
    - pip install -r requirements.txt (provides boto3)
"""
import argparse
import json
import sys
import uuid
from pathlib import Path

import boto3

# The repository root, so that the solution module is found from whichever directory this file is run
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from educational_course_content_generator_with_qna_bot_using_bedrock.solution import get_client  # noqa: E402

FALLBACK_REGION = "us-east-1"
# CloudFormation logical ID prefix of the CDK construct "qna_bot_lambda" (the proxy Lambda)
PROXY_LAMBDA_LOGICAL_ID_PREFIX = "qnabotlambda"


def get_agent_runtime_arn(region: str, stack_name: str = "QnAStack") -> str:
    """Retrieve the AgentCore Runtime ARN from CloudFormation stack outputs."""
    cf_client = get_client("cloudformation", region_name=region)
    try:
        response = cf_client.describe_stacks(StackName=stack_name)
        for output in response["Stacks"][0].get("Outputs", []):
            if output["OutputKey"] == "AgentCoreRuntimeArn":
                return output["OutputValue"]
        raise ValueError(
            f"Output 'AgentCoreRuntimeArn' not found in stack '{stack_name}'. "
            f"Ensure the stack is deployed with qna_bot_mode='strands'."
        )
    except cf_client.exceptions.ClientError as e:
        raise RuntimeError(
            f"Failed to describe stack '{stack_name}' in region '{region}': {e}"
        ) from e


def invoke_agentcore_runtime(agent_runtime_arn: str, region: str, question: str,
                             course_name: str = "Fundamentals of Machine Learning",
                             course_id: str = "Dummy-c001", week_number: int = 2):
    """Invoke the AgentCore Runtime directly and print the response."""

    print(f"\n{'=' * 60}")
    print("Invoking AgentCore Runtime directly")
    print(f"{'=' * 60}")
    print(f"Agent Runtime ARN: {agent_runtime_arn}")
    print(f"Question: {question}")
    print(f"Course: {course_name}")
    print(f"{'=' * 60}\n")

    payload = {
        "user_question": question,
        "course_name": course_name,
        "course_id": course_id,
        "week_number": week_number,
    }

    # Generate session ID (must be 33+ chars for AgentCore)
    session_id = str(uuid.uuid4()) + "-" + str(uuid.uuid4())[:8]

    try:
        client = get_client("bedrock-agentcore", region_name=region)

        print("Invoking AgentCore Runtime...")
        response = client.invoke_agent_runtime(
            agentRuntimeArn=agent_runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload),
            qualifier="DEFAULT",
        )

        response_data = json.loads(response["response"].read())

        print(f"\n{'=' * 60}")
        print("✅ AgentCore Runtime Response:")
        print(f"{'=' * 60}")

        if "bot_response" in response_data:
            print(f"\n{response_data['bot_response'][:1000]}")
        else:
            print(json.dumps(response_data, indent=2)[:1000])

        print(f"\n{'=' * 60}")
        print(f"Session ID: {session_id}")
        print(f"HTTP Status: {response['ResponseMetadata']['HTTPStatusCode']}")
        print(f"{'=' * 60}")

        return response_data

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print("\nTroubleshooting:")
        print("  1. Verify the AgentCore Runtime ARN is correct")
        print("  2. Check that the runtime status is ACTIVE")
        print("  3. Ensure your AWS credentials have bedrock-agentcore:InvokeAgentRuntime permission")
        print("  4. Check CloudWatch logs for the AgentCore Runtime")
        raise


def invoke_via_proxy_lambda(region: str, question: str, sub: str, stack_name: str = "QnAStack",
                            course_name: str = "Fundamentals of Machine Learning",
                            course_id: str = "Dummy-c001", week_number: int = 2):
    """Invoke the proxy Lambda that bridges the WebSocket API to AgentCore."""

    print(f"\n{'=' * 60}")
    print("Invoking via Proxy Lambda (WebSocket bridge)")
    print(f"{'=' * 60}\n")

    # Find the proxy Lambda through the stack resources
    cf_client = get_client("cloudformation", region_name=region)
    resources = cf_client.describe_stack_resources(StackName=stack_name)
    lambda_name = next(
        (r["PhysicalResourceId"] for r in resources["StackResources"]
         if r["ResourceType"] == "AWS::Lambda::Function"
         and r["LogicalResourceId"].startswith(PROXY_LAMBDA_LOGICAL_ID_PREFIX)),
        None,
    )
    if not lambda_name:
        print(f"❌ Could not find the proxy Lambda in {stack_name}")
        return None

    print(f"Proxy Lambda: {lambda_name}")

    # Behind API Gateway the $connect Lambda authorizer supplies the caller's Cognito
    # subject in requestContext.authorizer.sub; when invoking the proxy directly we
    # have to stub it ourselves (the proxy rejects requests without it).
    event = {
        "requestContext": {
            "routeKey": "qnaBot",
            "connectionId": f"invoke-script-{uuid.uuid4().hex[:12]}",
            "requestId": uuid.uuid4().hex[:16],
            "authorizer": {"sub": sub, "username": "invoke-script"},
        },
        "body": json.dumps({
            "user_question": question,
            "course_name": course_name,
            "course_id": course_id,
            "week_number": week_number,
        }),
    }

    lambda_client = get_client("lambda", region_name=region)
    response = lambda_client.invoke(
        FunctionName=lambda_name,
        Payload=json.dumps(event),
    )
    response_payload = json.loads(response["Payload"].read())

    if response.get("FunctionError"):
        print(f"❌ Lambda Error: {response_payload.get('errorMessage', 'Unknown error')}")
        return response_payload

    body = response_payload.get("body", "{}")
    if isinstance(body, str):
        body = json.loads(body)
    print(f"Status code: {response_payload.get('statusCode')}")
    if "bot_response" in body:
        print("\n✅ Bot Response:")
        print(f"{str(body['bot_response'])[:1000]}")
    else:
        print(f"\n❌ No bot_response in reply: {json.dumps(body)[:1000]}")

    return response_payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Invoke the Bedrock AgentCore Runtime QnA Bot")
    parser.add_argument("question", nargs="?", default="What is machine learning?",
                        help="Question to ask the QnA bot (default: 'What is machine learning?')")
    parser.add_argument("--region",
                        help="AWS region where QnAStack is deployed (default: your AWS profile's region, else us-east-1)")
    parser.add_argument("--stack-name", default="QnAStack",
                        help="CloudFormation stack name (default: QnAStack)")
    parser.add_argument("--sub", default=str(uuid.uuid4()),
                        help="Cognito subject to stub in requestContext.authorizer when calling the proxy "
                             "directly (default: a random UUID; reuse a value to continue a conversation)")
    args = parser.parse_args()
    region = args.region or boto3.session.Session().region_name or FALLBACK_REGION

    print("\n" + "🤖 " * 20)
    print("  AgentCore Runtime QnA Bot Invocation")
    print("🤖 " * 20)

    # Retrieve the AgentCore Runtime ARN from CloudFormation outputs
    print(f"\nFetching AgentCore Runtime ARN from {args.stack_name} stack outputs in {region}...")
    agent_runtime_arn = get_agent_runtime_arn(region, args.stack_name)
    print(f"Found ARN: {agent_runtime_arn}")

    # Step 1: Direct AgentCore invocation
    try:
        invoke_agentcore_runtime(agent_runtime_arn, region, args.question)
    except Exception:
        print("\nDirect invocation failed; continuing with the proxy Lambda...\n")

    # Step 2: Via proxy Lambda
    print("\n")
    invoke_via_proxy_lambda(region, args.question, args.sub, args.stack_name)
