#!/usr/bin/env python3
"""
Create a Cognito user and retrieve a JWT token for testing the WebSocket APIs.

This script reads the Cognito User Pool ID and Client ID from CloudFormation
stack outputs, creates a test user (or authenticates an existing one), and
prints the JWT IdToken for use with wscat or other WebSocket clients.

Usage:
    # Create a new user and get a token
    python scripts/create_cognito_user.py

    # Create with custom credentials
    python scripts/create_cognito_user.py --username myuser --password 'MyPass@2026!'

    # Just get a token for an existing user
    python scripts/create_cognito_user.py --username myuser --password 'MyPass@2026!' --token-only

    # Specify a different region
    python scripts/create_cognito_user.py --region us-west-2

Prerequisites:
    - AWS credentials configured
    - CourseStack deployed (provides Cognito User Pool)
    - pip install boto3
"""
import boto3
import argparse
import sys
import json


def get_stack_outputs(region: str, stack_name: str = "CourseStack") -> dict:
    """Retrieve Cognito details from CloudFormation stack outputs."""
    cf_client = boto3.client("cloudformation", region_name=region)
    try:
        response = cf_client.describe_stacks(StackName=stack_name)
        outputs = {}
        for output in response["Stacks"][0].get("Outputs", []):
            outputs[output["OutputKey"]] = output["OutputValue"]
        return outputs
    except cf_client.exceptions.ClientError as e:
        print(f"❌ Failed to describe stack '{stack_name}': {e}")
        sys.exit(1)


def get_websocket_api_endpoint(region: str, api_name: str) -> str:
    """Get the WebSocket API endpoint by name."""
    apigw_client = boto3.client("apigatewayv2", region_name=region)
    try:
        apis = apigw_client.get_apis()
        for api in apis.get("Items", []):
            if api["Name"] == api_name:
                return f"wss://{api['ApiId']}.execute-api.{region}.amazonaws.com/dev"
    except Exception:
        pass
    return None


def create_user(cognito_client, user_pool_id: str, username: str, password: str, name: str):
    """Create a new Cognito user and set their permanent password."""
    try:
        # Check if user already exists
        try:
            cognito_client.admin_get_user(
                UserPoolId=user_pool_id,
                Username=username,
            )
            print(f"ℹ️  User '{username}' already exists.")
            return True
        except cognito_client.exceptions.UserNotFoundException:
            pass

        # Create the user with a temporary password
        print(f"📝 Creating user '{username}'...")
        cognito_client.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            TemporaryPassword=password,
            UserAttributes=[
                {"Name": "name", "Value": name},
            ],
            MessageAction="SUPPRESS",  # Don't send welcome email
        )
        print(f"✅ User '{username}' created successfully.")
        return True

    except Exception as e:
        print(f"❌ Failed to create user: {e}")
        return False


def authenticate_and_get_token(cognito_client, user_pool_id: str, client_id: str,
                                username: str, password: str, name: str) -> str:
    """Authenticate the user and return the JWT IdToken."""
    try:
        # Initial authentication
        response = cognito_client.initiate_auth(
            ClientId=client_id,
            AuthFlow="USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": username,
                "PASSWORD": password,
            },
        )

        # Handle NEW_PASSWORD_REQUIRED challenge (first login with temp password)
        if response.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
            print("🔑 Responding to password challenge...")
            response = cognito_client.respond_to_auth_challenge(
                ClientId=client_id,
                ChallengeName="NEW_PASSWORD_REQUIRED",
                ChallengeResponses={
                    "USERNAME": username,
                    "NEW_PASSWORD": password,
                    "userAttributes.name": name,
                },
                Session=response["Session"],
            )

        auth_result = response.get("AuthenticationResult", {})
        id_token = auth_result.get("IdToken")

        if not id_token:
            print("❌ Authentication succeeded but no IdToken returned.")
            return None

        return id_token

    except cognito_client.exceptions.NotAuthorizedException as e:
        print(f"❌ Authentication failed: {e}")
        return None
    except Exception as e:
        print(f"❌ Unexpected error during authentication: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Create a Cognito test user and get a JWT token for WebSocket testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/create_cognito_user.py
  python scripts/create_cognito_user.py --username student1 --password 'Student@2026!'
  python scripts/create_cognito_user.py --token-only --username testuser --password 'TestUser@2026!'
        """,
    )
    parser.add_argument("--username", default="testuser",
                        help="Username for the Cognito user (default: testuser)")
    parser.add_argument("--password", default="TestUser@2026!",
                        help="Password for the Cognito user (default: TestUser@2026!)")
    parser.add_argument("--name", default="Test User",
                        help="Display name for the user (default: Test User)")
    parser.add_argument("--region", default="us-east-1",
                        help="AWS region (default: us-east-1)")
    parser.add_argument("--stack-name", default="CourseStack",
                        help="CloudFormation stack name (default: CourseStack)")
    parser.add_argument("--token-only", action="store_true",
                        help="Skip user creation, just authenticate and get token")
    args = parser.parse_args()

    print("\n" + "=" * 60)
    print("  Cognito User & JWT Token Helper")
    print("=" * 60)

    # Step 1: Get Cognito details from CloudFormation
    print(f"\n📦 Reading stack outputs from '{args.stack_name}'...")
    outputs = get_stack_outputs(args.region, args.stack_name)

    user_pool_arn = outputs.get("UserPoolArn")
    client_id = outputs.get("UserPoolClientId")

    if not user_pool_arn or not client_id:
        print("❌ Could not find UserPoolArn or UserPoolClientId in stack outputs.")
        print("   Make sure the CourseStack is deployed.")
        sys.exit(1)

    # Extract User Pool ID from ARN (format: arn:aws:cognito-idp:REGION:ACCOUNT:userpool/POOL_ID)
    user_pool_id = user_pool_arn.split("/")[-1]
    print(f"   User Pool ID: {user_pool_id}")
    print(f"   Client ID:    {client_id}")

    cognito_client = boto3.client("cognito-idp", region_name=args.region)

    # Step 2: Create user (unless --token-only)
    if not args.token_only:
        success = create_user(cognito_client, user_pool_id, args.username, args.password, args.name)
        if not success:
            sys.exit(1)

    # Step 3: Authenticate and get token
    print(f"\n🔐 Authenticating user '{args.username}'...")
    id_token = authenticate_and_get_token(
        cognito_client, user_pool_id, client_id,
        args.username, args.password, args.name,
    )

    if not id_token:
        sys.exit(1)

    print("✅ Authentication successful!")

    # Step 4: Get WebSocket endpoints
    print(f"\n📡 Looking up WebSocket API endpoints...")
    qna_ws_endpoint = get_websocket_api_endpoint(args.region, "QnAWSApi")
    course_ws_endpoint = get_websocket_api_endpoint(args.region, "CourseWSApi")

    # Step 5: Print results
    print("\n" + "=" * 60)
    print("  JWT Token (IdToken)")
    print("=" * 60)
    print(f"\n{id_token}")

    print("\n" + "=" * 60)
    print("  WebSocket Connection Commands")
    print("=" * 60)

    if qna_ws_endpoint:
        print(f"\n🤖 QnA Bot (connect with wscat):")
        print(f'   wscat -c "{qna_ws_endpoint}" -H "Authorization: Bearer {id_token}"')
        print(f"\n   Then send:")
        print(f'   {{"action": "qnaBot", "user_question": "What is machine learning?", "course_name": "Fundamentals of Machine Learning", "course_id": "Dummy-c001", "week_number": 2}}')

    if course_ws_endpoint:
        print(f"\n📚 Course API (connect with wscat):")
        print(f'   wscat -c "{course_ws_endpoint}" -H "Authorization: Bearer {id_token}"')

    print("\n" + "=" * 60)
    print(f"  Token expires in 24 hours. Re-run this script to refresh.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
