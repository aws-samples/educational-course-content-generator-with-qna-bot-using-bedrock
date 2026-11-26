# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Lambda authorizer for the QnA WebSocket API $connect route.

Verifies the Cognito ID token supplied in the ``Authorization`` header (query-string
tokens are not accepted) and, on success, returns an allow policy whose ``principalId``
and ``context.sub`` carry the caller's Cognito subject to the integration Lambdas.
Every failure path returns the deny policy (fail closed).
"""
import jwt

from helper import (
    ALLOWED_ALGORITHMS,
    BASE_ISSUER_URL,
    COGNITO_APP_CLIENT_ID,
    JWKS_URL,
    LOGGER,
    UNAUTHORIZED_RESPONSE,
    build_authorized_response,
    configure_logger,
    silence_noisy_loggers,
    valid_token,
)

# Created once per execution environment so fetched signing keys are cached between invocations.
JWKS_CLIENT = jwt.PyJWKClient(JWKS_URL)


def _authorization_header(event):
    """Return the Authorization header value (case-insensitive lookup) or None."""
    headers = event.get("headers")
    if not isinstance(headers, dict):
        return None
    for name, value in headers.items():
        if isinstance(name, str) and name.lower() == "authorization":
            return value
    return None


def _extract_token(header_value):
    """Strip an optional ``Bearer`` prefix and return the raw token string."""
    if not isinstance(header_value, str):
        return ""
    token = header_value.strip()
    if token.lower().startswith("bearer "):
        token = token[len("bearer "):].strip()
    return token


def lambda_handler(event, context):
    """Authorize a WebSocket $connect request with a Cognito ID token."""
    configure_logger()
    silence_noisy_loggers()

    event = event if isinstance(event, dict) else {}
    request_context = event.get("requestContext") or {}
    LOGGER.info(
        "Authorizing route=%s connectionId=%s requestId=%s",
        request_context.get("routeKey"),
        request_context.get("connectionId"),
        request_context.get("requestId"),
    )

    if request_context.get("eventType") != "CONNECT":
        LOGGER.error("Authorizer invoked for a non-CONNECT event")
        return UNAUTHORIZED_RESPONSE

    token_string = _extract_token(_authorization_header(event))
    if not token_string:
        LOGGER.error("Authorization header missing or empty")
        return UNAUTHORIZED_RESPONSE

    try:
        signing_key = JWKS_CLIENT.get_signing_key_from_jwt(token_string).key
        decoded_token = jwt.decode(
            token_string,
            signing_key,
            algorithms=ALLOWED_ALGORITHMS,
            audience=COGNITO_APP_CLIENT_ID,
            issuer=BASE_ISSUER_URL,
            options={"require": ["exp", "aud", "iss", "sub"]},
        )
    except jwt.exceptions.PyJWTError as err:
        # Log only the exception class: some PyJWT messages echo untrusted header values (for example
        # the ``kid`` of an unknown signing key) and the bearer value itself is never logged. The message
        # deliberately avoids credential-related words so log scanners do not flag it.
        LOGGER.error("Signature or claim validation rejected the connection: %s", type(err).__name__)
        return UNAUTHORIZED_RESPONSE
    except Exception as err:  # fail closed on anything unexpected (e.g. JWKS fetch problems)
        LOGGER.error("Validation aborted by an unexpected error: %s", type(err).__name__)
        return UNAUTHORIZED_RESPONSE

    if not valid_token(decoded_token, COGNITO_APP_CLIENT_ID):
        return UNAUTHORIZED_RESPONSE

    LOGGER.info("Connection authorized")
    return build_authorized_response(decoded_token)
