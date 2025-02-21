## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import logging
import os
import time

LOGGER = logging.getLogger()
BASE_ISSUER_URL = f"https://cognito-idp.{os.environ['API_REGION']}.amazonaws.com/{os.environ['COGNITO_USER_POOL_ID']}"

JWKS_URL = f"{BASE_ISSUER_URL}/.well-known/jwks.json"
VALID_TOKEN_USE = ["id"] 

AUTHORIZED_RESPONSE = {
    "policyDocument": {
        "Version": "2012-10-17",
         "Statement": [
             {
                 "Action": "execute-api:Invoke",
                 "Resource": [f"arn:aws:execute-api:{os.environ['API_REGION']}:{os.environ['ACCOUNT_ID']}:{os.environ['WEBSOCKET_API_ID']}*"],
                 "Effect": "Allow"
        }]
    }
}

UNAUTHORIZED_RESPONSE = {
    "policyDocument": {
        "Version": "2012-10-17",
         "Statement": [
             {
                 "Action": "*",
                 "Resource": ["*"],
                 "Effect": "Deny"
        }]
    }
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
    """Check JWT token parameters' validity

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
        LOGGER.error(f"Audience client {aud} does not match")
        return False
    
    iss = token.get("iss")
    if not iss:
        LOGGER.error("Missing 'iss' key in token")
        return False
    
    if iss != BASE_ISSUER_URL:
        LOGGER.error(f"Issuer URL {iss} did not match")
        return False

    token_use = token.get("token_use")
    if not token_use:
        LOGGER.error("token_use missing from token")
        return False
    
    if token_use not in VALID_TOKEN_USE:
        LOGGER.error(f"token_use {token_use} is not a valid option")
        return False
    
    LOGGER.info("Decoded token is verified to be valid")
    return True
