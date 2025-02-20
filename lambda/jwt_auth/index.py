## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import os
import jwt
from helper import *

def lambda_handler(event, context):
    """What executes when the program is run"""
    print(event)

    # configure python logger for Lambda
    configure_logger()
    # silence chatty libraries for better logging
    silence_noisy_loggers()

    # Check if the event is a WebSocket connection request
    if event['requestContext']['eventType'] == 'CONNECT':
        auth_token = event['headers'].get("Authorization", None)
        query_param_auth_token = event["queryStringParameters"].get("Authorization", None)
        if query_param_auth_token:
            auth_token = query_param_auth_token

        if not auth_token:
            LOGGER.error("No Authorization header found")
            return UNAUTHORIZED_RESPONSE

        token_string = auth_token.replace("Bearer ", "")
        if not token_string:
            LOGGER.error("empty token provided")
            return UNAUTHORIZED_RESPONSE
        
        LOGGER.info("Attempting to extract headers from the token string")
        try:
            token_header = jwt.get_unverified_header(token_string)
        except jwt.exceptions.DecodeError as err:
            LOGGER.error(
                f"Unable to extract headers from the token string: {err}")
            return UNAUTHORIZED_RESPONSE

        LOGGER.info(f"Initializing jwks client for: {JWKS_URL}")
        jwks_client = jwt.PyJWKClient(JWKS_URL)

        LOGGER.info("Trying to get the signing key from the token header")
        try:
            key = jwks_client.get_signing_key(token_header["kid"]).key
        except jwt.exceptions.PyJWKSetError as err:
            LOGGER.error(f"Unable to fetch keys: {err}")
            return UNAUTHORIZED_RESPONSE
        except jwt.exceptions.PyJWKClientError as err:
            LOGGER.error(f"No matching key found: {err}")
            return UNAUTHORIZED_RESPONSE
        
        algorithm = token_header.get("alg")
        if not algorithm:
            LOGGER.error("Token header did not contain the alg key")
            return UNAUTHORIZED_RESPONSE
        
        audience_client = os.environ["COGNITO_APP_CLIENT_ID"]
        LOGGER.info(f"Trying to decode the token string for client: {audience_client}")
        try:
            decoded_token = jwt.decode(
                token_string, 
                key, 
                [algorithm], 
                audience=audience_client
            )
        except jwt.exceptions.DecodeError as err:
            LOGGER.error(f"Unable to decode token string: {err}")
            return UNAUTHORIZED_RESPONSE
        except jwt.exceptions.MissingRequiredClaimError as err:
            LOGGER.error(f"Unable to decode token: {err}")
            return UNAUTHORIZED_RESPONSE
        except jwt.exceptions.ExpiredSignatureError as err:
            LOGGER.error(f"Signature has expired: {err}")
            return UNAUTHORIZED_RESPONSE
        
        if not valid_token(decoded_token, audience_client):
            return UNAUTHORIZED_RESPONSE
        
        return AUTHORIZED_RESPONSE

if __name__ == "__main__":
    event = None
    lambda_handler(event, None)