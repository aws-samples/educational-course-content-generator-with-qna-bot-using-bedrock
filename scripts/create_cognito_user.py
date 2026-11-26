#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Create a Cognito user and retrieve a JWT token for testing the WebSocket APIs.

This script reads the Cognito User Pool ID and Client ID from CloudFormation
stack outputs, creates a test user (or authenticates an existing one), and
prints the JWT IdToken together with ready-to-use wscat commands. The commands
connect through the CloudFront distributions, which are the WAF-protected entry
points of the WebSocket APIs.

Usage:
    # Create a user (you are prompted for the password) and get a token
    python scripts/create_cognito_user.py

    # Choose the username; the password is prompted for unless --password is given
    python scripts/create_cognito_user.py --username myuser

    # Just get a token for an existing user
    python scripts/create_cognito_user.py --username myuser --token-only

    # Specify a region (defaults to your AWS profile's region, then us-east-1)
    python scripts/create_cognito_user.py --region us-east-1

Prerequisites:
    - AWS credentials configured
    - CourseStack deployed (provides the Cognito User Pool); QnAStack for the QnA endpoint
    - pip install -r requirements.txt (provides boto3)
"""
import argparse
import getpass
import logging
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# The repository root, so that the solution module is found from whichever directory this file is run
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from educational_course_content_generator_with_qna_bot_using_bedrock.solution import get_client  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

FALLBACK_REGION = "us-east-1"


def get_stack_outputs(region: str, stack_name: str, required: bool = True) -> dict:
    """Return the outputs of a CloudFormation stack; an optional stack that cannot be read yields {}."""
    cf_client = get_client("cloudformation", region_name=region)
    try:
        response = cf_client.describe_stacks(StackName=stack_name)
    except (ClientError, BotoCoreError) as e:
        if required:
            print(f"❌ Failed to describe stack '{stack_name}': {e}")
            sys.exit(1)
        logger.warning("Could not read outputs of stack '%s' (%s); its endpoint will be skipped", stack_name, e)
        return {}
    return {output["OutputKey"]: output["OutputValue"]
            for output in response["Stacks"][0].get("Outputs", [])}


def create_user(cognito_client, user_pool_id: str, username: str, password: str, name: str) -> bool:
    """Create a new Cognito user (with a temporary password) unless it already exists."""
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

    except (ClientError, BotoCoreError) as e:
        print(f"❌ Failed to create user: {e}")
        return False


def authenticate_and_get_token(cognito_client, user_pool_id: str, client_id: str,
                               username: str, password: str, name: str) -> str | None:
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

        id_token = response.get("AuthenticationResult", {}).get("IdToken")
        if not id_token:
            print("❌ Authentication succeeded but no IdToken returned.")
            return None
        return id_token

    except cognito_client.exceptions.NotAuthorizedException as e:
        print(f"❌ Authentication failed: {e}")
        return None
    except (ClientError, BotoCoreError) as e:
        print(f"❌ Unexpected error during authentication: {e}")
        return None


def prompt_for_password(confirm: bool) -> str:
    """Read the password from the terminal without echoing it."""
    password = getpass.getpass("Password (input hidden): ")
    if not password:
        print("❌ A password is required.")
        sys.exit(1)
    if confirm and getpass.getpass("Confirm password: ") != password:
        print("❌ Passwords do not match.")
        sys.exit(1)
    return password


def main():
    parser = argparse.ArgumentParser(
        description="Create a Cognito test user and get a JWT token for WebSocket testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/create_cognito_user.py
  python scripts/create_cognito_user.py --username student1
  python scripts/create_cognito_user.py --token-only --username testuser
        """,
    )
    parser.add_argument("--username", default="testuser",
                        help="Username for the Cognito user (default: testuser)")
    parser.add_argument("--password",
                        help="Password for the Cognito user; prompted for securely when omitted")
    parser.add_argument("--name", default="Test User",
                        help="Display name for the user (default: Test User)")
    parser.add_argument("--region",
                        help="AWS region (default: the region of your AWS profile, else us-east-1)")
    parser.add_argument("--stack-name", default="CourseStack",
                        help="Stack that provides the Cognito User Pool and Course API (default: CourseStack)")
    parser.add_argument("--qna-stack-name", default="QnAStack",
                        help="Stack that provides the QnA Bot API (default: QnAStack)")
    parser.add_argument("--token-only", action="store_true",
                        help="Skip user creation, just authenticate and get token")
    args = parser.parse_args()

    region = args.region or boto3.session.Session().region_name or FALLBACK_REGION
    password = args.password or prompt_for_password(confirm=not args.token_only)

    print("\n" + "=" * 60)
    print("  Cognito User & JWT Token Helper")
    print("=" * 60)

    # Step 1: Get Cognito details from CloudFormation
    print(f"\n📦 Reading stack outputs from '{args.stack_name}' in {region}...")
    outputs = get_stack_outputs(region, args.stack_name)

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

    cognito_client = get_client("cognito-idp", region_name=region)

    # Step 2: Create user (unless --token-only)
    if not args.token_only:
        if not create_user(cognito_client, user_pool_id, args.username, password, args.name):
            sys.exit(1)

    # Step 3: Authenticate and get token
    print(f"\n🔐 Authenticating user '{args.username}'...")
    id_token = authenticate_and_get_token(
        cognito_client, user_pool_id, client_id,
        args.username, password, args.name,
    )
    if not id_token:
        sys.exit(1)
    print("✅ Authentication successful!")

    # Step 4: WebSocket endpoints through CloudFront (the origin path maps to the API's /dev stage)
    print("\n📡 Looking up the CloudFront WebSocket endpoints...")
    course_domain = outputs.get("CourseCloudFrontDomainName")
    qna_domain = get_stack_outputs(region, args.qna_stack_name, required=False).get("QnACloudFrontDomainName")
    course_ws_endpoint = f"wss://{course_domain}" if course_domain else None
    qna_ws_endpoint = f"wss://{qna_domain}" if qna_domain else None

    # Step 5: Print results
    print("\n" + "=" * 60)
    print("  JWT Token (IdToken)")
    print("=" * 60)
    print(f"\n{id_token}")

    print("\n" + "=" * 60)
    print("  WebSocket Connection Commands (via CloudFront)")
    print("=" * 60)

    if qna_ws_endpoint:
        print("\n🤖 QnA Bot (connect with wscat):")
        print(f'   wscat -c "{qna_ws_endpoint}" -H "Authorization: Bearer {id_token}"')
        print("\n   Then send:")
        print('   {"action": "qnaBot", "user_question": "What is machine learning?", '
              '"course_name": "Fundamentals of Machine Learning", "course_id": "Dummy-c001", "week_number": 2}')
    else:
        print("\n🤖 QnA Bot endpoint not available (deploy QnAStack to get the QnACloudFrontDomainName output).")

    if course_ws_endpoint:
        print("\n📚 Course API (connect with wscat):")
        print(f'   wscat -c "{course_ws_endpoint}" -H "Authorization: Bearer {id_token}"')

    print("\n   Connect through CloudFront as shown; the raw execute-api endpoints bypass AWS WAF.")

    print("\n" + "=" * 60)
    print("  Token expires in 24 hours. Re-run this script to refresh.")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
