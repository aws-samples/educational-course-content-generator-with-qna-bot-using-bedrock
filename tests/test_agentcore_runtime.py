#!/usr/bin/env python3
"""
Test script for the Bedrock AgentCore Runtime QnA Bot.

This script directly invokes the AgentCore Runtime to verify the
Strands Agent is working correctly.

Usage:
    python tests/test_agentcore_runtime.py
    python tests/test_agentcore_runtime.py "What is supervised learning?"
    python tests/test_agentcore_runtime.py "What is supervised learning?" --region us-west-2

Prerequisites:
    - AWS credentials configured
    - QnAStack deployed with qna_bot_mode="strands"
    - pip install boto3
"""
import boto3
import json
import uuid
import sys
import argparse


def get_agent_runtime_arn(region: str, stack_name: str = "QnAStack") -> str:
    """Retrieve the AgentCore Runtime ARN from CloudFormation stack outputs."""
    cf_client = boto3.client("cloudformation", region_name=region)
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


def test_agentcore_runtime(agent_runtime_arn: str, region: str, question: str,
                           course_name: str = "Fundamentals of Machine Learning",
                           course_id: str = "Dummy-c001", week_number: int = 2):
    """Invoke the AgentCore Runtime and print the response."""

    print(f"\n{'='*60}")
    print(f"Testing AgentCore Runtime")
    print(f"{'='*60}")
    print(f"Agent Runtime ARN: {agent_runtime_arn}")
    print(f"Question: {question}")
    print(f"Course: {course_name}")
    print(f"{'='*60}\n")

    # Build payload
    payload = {
        "user_question": question,
        "course_name": course_name,
        "course_id": course_id,
        "week_number": week_number,
    }

    # Generate session ID (must be 33+ chars for AgentCore)
    session_id = str(uuid.uuid4()) + "-" + str(uuid.uuid4())[:8]

    try:
        # Initialize the AgentCore client
        client = boto3.client("bedrock-agentcore", region_name=region)

        print("Invoking AgentCore Runtime...")
        response = client.invoke_agent_runtime(
            agentRuntimeArn=agent_runtime_arn,
            runtimeSessionId=session_id,
            payload=json.dumps(payload),
            qualifier="DEFAULT",
        )

        # Read the response
        response_body = response["response"].read()
        response_data = json.loads(response_body)

        print(f"\n{'='*60}")
        print("✅ AgentCore Runtime Response:")
        print(f"{'='*60}")

        if "bot_response" in response_data:
            print(f"\n{response_data['bot_response'][:1000]}")
        else:
            print(json.dumps(response_data, indent=2)[:1000])

        print(f"\n{'='*60}")
        print(f"Session ID: {session_id}")
        print(f"HTTP Status: {response['ResponseMetadata']['HTTPStatusCode']}")
        print(f"{'='*60}")

        return response_data

    except Exception as e:
        print(f"\n❌ Error: {e}")
        print(f"\nTroubleshooting:")
        print(f"  1. Verify the AgentCore Runtime ARN is correct")
        print(f"  2. Check that the runtime status is ACTIVE")
        print(f"  3. Ensure your AWS credentials have bedrock-agentcore:InvokeAgentRuntime permission")
        print(f"  4. Check CloudWatch logs for the AgentCore Runtime")
        raise


def test_via_proxy_lambda(region: str, question: str,
                          course_name: str = "Fundamentals of Machine Learning",
                          course_id: str = "Dummy-c001", week_number: int = 2):
    """Test the proxy Lambda that bridges WebSocket to AgentCore."""

    print(f"\n{'='*60}")
    print(f"Testing via Proxy Lambda (WebSocket bridge)")
    print(f"{'='*60}\n")

    # Get the Lambda function name from CloudFormation
    cf_client = boto3.client("cloudformation", region_name=region)
    resources = cf_client.describe_stack_resources(StackName="QnAStack")
    lambda_name = None
    for r in resources["StackResources"]:
        if r["LogicalResourceId"] == "qnabotlambda2EF6061E":
            lambda_name = r["PhysicalResourceId"]
            break

    if not lambda_name:
        print("❌ Could not find proxy Lambda in QnAStack")
        return

    print(f"Proxy Lambda: {lambda_name}")

    lambda_client = boto3.client("lambda", region_name=region)

    payload = {
        "body": json.dumps({
            "user_question": question,
            "course_name": course_name,
            "course_id": course_id,
            "week_number": week_number,
        })
    }

    response = lambda_client.invoke(
        FunctionName=lambda_name,
        Payload=json.dumps(payload),
    )

    response_payload = json.loads(response["Payload"].read())

    if response.get("FunctionError"):
        print(f"❌ Lambda Error: {response_payload.get('errorMessage', 'Unknown error')}")
    else:
        body = json.loads(response_payload.get("body", "{}"))
        bot_response = body.get('bot_response', str(body))
        print(f"\n✅ Bot Response:")
        print(f"{str(bot_response)[:1000]}")

    return response_payload


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test the Bedrock AgentCore Runtime QnA Bot")
    parser.add_argument("question", nargs="?", default="What is machine learning?",
                        help="Question to ask the QnA bot (default: 'What is machine learning?')")
    parser.add_argument("--region", default="us-east-1",
                        help="AWS region where QnAStack is deployed (default: us-east-1)")
    parser.add_argument("--stack-name", default="QnAStack",
                        help="CloudFormation stack name (default: QnAStack)")
    args = parser.parse_args()

    print("\n" + "🤖 " * 20)
    print("  AgentCore Runtime QnA Bot Test")
    print("🤖 " * 20)

    # Retrieve the AgentCore Runtime ARN from CloudFormation outputs
    print(f"\nFetching AgentCore Runtime ARN from {args.stack_name} stack outputs...")
    agent_runtime_arn = get_agent_runtime_arn(args.region, args.stack_name)
    print(f"Found ARN: {agent_runtime_arn}")

    # Test 1: Direct AgentCore invocation
    try:
        test_agentcore_runtime(agent_runtime_arn, args.region, args.question)
    except Exception:
        print("\nFalling back to proxy Lambda test...\n")

    # Test 2: Via proxy Lambda
    print("\n")
    test_via_proxy_lambda(args.region, args.question)
