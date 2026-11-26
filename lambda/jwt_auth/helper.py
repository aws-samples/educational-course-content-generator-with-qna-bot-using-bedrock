# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Helpers for the Cognito JWT Lambda authorizer on the QnA WebSocket $connect route."""
import logging
import os
import time

LOGGER = logging.getLogger()

API_REGION = os.environ["API_REGION"]
ACCOUNT_ID = os.environ["ACCOUNT_ID"]
COGNITO_USER_POOL_ID = os.environ["COGNITO_USER_POOL_ID"]
COGNITO_APP_CLIENT_ID = os.environ["COGNITO_APP_CLIENT_ID"]

BASE_ISSUER_URL = f"https://cognito-idp.{API_REGION}.amazonaws.com/{COGNITO_USER_POOL_ID}"
JWKS_URL = f"{BASE_ISSUER_URL}/.well-known/jwks.json"
VALID_TOKEN_USE = ["id"]
# Cognito signs tokens with RS256 only; the "alg" header of an untrusted token is never used.
ALLOWED_ALGORITHMS = ["RS256"]

UNAUTHORIZED_RESPONSE = {
    "principalId": "anonymous",
    "policyDocument": {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Action": "*",
                "Resource": ["*"],
                "Effect": "Deny",
            }
        ],
    },
}


def build_authorized_response(decoded_token):
    """Build the allow response for a verified Cognito ID token.

    API Gateway exposes ``principalId`` and every ``context`` key to the integration
    Lambdas of later routes as ``event["requestContext"]["authorizer"][<key>]``; the
    proxy Lambda uses ``sub`` to derive a per-user AgentCore session.

    :param decoded_token: Dictionary of verified token claims
    :rtype: Dictionary
    """
    sub = decoded_token["sub"]
    return {
        "principalId": sub,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Action": "execute-api:Invoke",
                    "Resource": [
                        f"arn:aws:execute-api:{API_REGION}:{ACCOUNT_ID}:{os.environ['WEBSOCKET_API_ID']}*"
                    ],
                    "Effect": "Allow",
                }
            ],
        },
        "context": {
            "sub": sub,
            "username": str(decoded_token.get("cognito:username") or ""),
        },
    }


def silence_noisy_loggers():
    """Silence chatty libraries for better logging"""
    for logger in ['boto3', 'botocore',
                   'botocore.vendored.requests.packages.urllib3']:
        logging.getLogger(logger).setLevel(logging.WARNING)


def configure_logger():
    """Configure python logger for lambda function"""
    default_log_args = {
        "level": logging.DEBUG if os.environ.get("VERBOSE", False) else logging.INFO,
        "format": "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        "datefmt": "%d-%b-%y %H:%M",
        "force": True,
    }
    logging.basicConfig(**default_log_args)


def valid_token(token, audience):
    """Check JWT token claims' validity (claim values are never logged)

    :param token: Dictionary
    :param audience: String

    :rtype: Boolean
    """
    expiry_time = token.get("exp")
    if not expiry_time:
        LOGGER.error("Token does not contain 'exp' key")
        return False

    if int(time.time()) > expiry_time:
        LOGGER.error("Token has expired")
        return False

    aud = token.get("aud")
    if not aud:
        LOGGER.error("Missing 'aud' key in token")
        return False

    if aud != audience:
        LOGGER.error("Audience claim does not match the app client")
        return False

    iss = token.get("iss")
    if not iss:
        LOGGER.error("Missing 'iss' key in token")
        return False

    if iss != BASE_ISSUER_URL:
        LOGGER.error("Issuer claim does not match the user pool")
        return False

    token_use = token.get("token_use")
    if not token_use:
        LOGGER.error("token_use missing from token")
        return False

    if token_use not in VALID_TOKEN_USE:
        LOGGER.error("token_use is not a valid option")
        return False

    sub = token.get("sub")
    if not isinstance(sub, str) or not sub:
        LOGGER.error("Missing 'sub' key in token")
        return False

    LOGGER.info("Decoded token is verified to be valid")
    return True
