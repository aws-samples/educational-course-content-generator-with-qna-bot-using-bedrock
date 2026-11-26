# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Proxy Lambda bridging the QnA WebSocket API to the Bedrock AgentCore Runtime.

The $connect Lambda authorizer places the caller's Cognito subject in
``requestContext.authorizer.sub``; this function derives a deterministic AgentCore
session id from that subject, the course id and an optional ``conversation_id``, so
conversation context is kept per authenticated user and course. Requests without an
authorizer subject are rejected, and any ``session_id`` in the request body is ignored.
"""
import hashlib
import json
import logging
import os
import re

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

AGENT_RUNTIME_ARN = os.environ["AGENT_RUNTIME_ARN"]
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")

MAX_QUESTION_CHARS = 4000
MAX_FIELD_CHARS = 200
MIN_WEEK_NUMBER = 1
MAX_WEEK_NUMBER = 52
CONVERSATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
GENERIC_ERROR = "The assistant is temporarily unavailable. Please try again."

# Standard retry mode honours the service model's retryable trait, so the brief HTTP 409
# RetryableConflictException that AgentCore returns while a session is being provisioned or torn
# down is retried with exponential backoff instead of surfacing as an error (botocore's default
# legacy mode has no retry policy for this service). Three attempts in total keep the worst case
# well below the 29-second WebSocket integration cap of API Gateway.
# The stack passes the solution user-agent suffix in USER_AGENT_STRING (see "Solution ID and usage
# reporting" in the README). Without the variable the client is created without a suffix.
agentcore_client = boto3.client(
    "bedrock-agentcore",
    region_name=REGION,
    config=Config(
        retries={"mode": "standard", "total_max_attempts": 3},
        user_agent_extra=os.environ.get("USER_AGENT_STRING", ""),
    ),
)


def _response(status_code, body):
    return {"statusCode": status_code, "body": json.dumps(body)}


def caller_sub(event):
    """Return the authenticated subject set by the $connect authorizer, or None."""
    authorizer = (event.get("requestContext") or {}).get("authorizer")
    if not isinstance(authorizer, dict):
        return None
    sub = authorizer.get("sub")
    return sub if isinstance(sub, str) and sub else None


def derive_session_id(sub, course_id, conversation_id):
    """Deterministic AgentCore session id per user, course and conversation (64 hex chars)."""
    return hashlib.sha256(f"{sub}:{course_id}:{conversation_id}".encode()).hexdigest()


def _optional_text(body, key):
    value = body.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > MAX_FIELD_CHARS:
        raise ValueError(f"{key} must be a string of at most {MAX_FIELD_CHARS} characters.")
    return value


def _required_text(body, key):
    """Return a mandatory string field; questions must always be scoped to one course."""
    value = _optional_text(body, key)
    if value is None or not value.strip():
        raise ValueError(f"{key} is required so that the answer is scoped to a single course.")
    return value


def _optional_week_number(value):
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            value = None
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_WEEK_NUMBER <= value <= MAX_WEEK_NUMBER:
        raise ValueError(f"week_number must be an integer between {MIN_WEEK_NUMBER} and {MAX_WEEK_NUMBER}.")
    return value


def parse_request(event):
    """Parse and validate the WebSocket message body; raises ValueError with a generic message."""
    try:
        body = json.loads(event.get("body") or "")
    except (TypeError, ValueError):
        body = None
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object with user_question.")

    user_question = body.get("user_question")
    if not isinstance(user_question, str) or not user_question.strip() or len(user_question) > MAX_QUESTION_CHARS:
        raise ValueError(f"user_question must be a non-empty string of at most {MAX_QUESTION_CHARS} characters.")

    conversation_id = body.get("conversation_id") or "default"
    if not isinstance(conversation_id, str) or not CONVERSATION_ID_PATTERN.fullmatch(conversation_id):
        raise ValueError("conversation_id must match ^[A-Za-z0-9_-]{1,64}$.")

    return {
        "user_question": user_question.strip(),
        "course_name": _required_text(body, "course_name"),
        "course_id": _required_text(body, "course_id"),
        "learning_objective": _optional_text(body, "learning_objective"),
        "week_number": _optional_week_number(body.get("week_number")),
        "conversation_id": conversation_id,
    }


def lambda_handler(event, context):
    event = event if isinstance(event, dict) else {}
    request_context = event.get("requestContext") or {}
    logger.info(
        "route=%s connectionId=%s requestId=%s",
        request_context.get("routeKey"),
        request_context.get("connectionId"),
        request_context.get("requestId"),
    )

    sub = caller_sub(event)
    if not sub:
        logger.warning("No authorizer subject on the request; rejecting")
        return _response(401, {"error": "Unauthorized"})

    try:
        request = parse_request(event)
    except ValueError as err:
        logger.warning("Rejected invalid request: %s", err)
        return _response(400, {"error": str(err)})

    course_id = request["course_id"] or ""
    session_id = derive_session_id(sub, course_id, request["conversation_id"])

    payload = {
        "user_question": request["user_question"],
        "course_name": request["course_name"],
        "learning_objective": request["learning_objective"],
        "course_id": request["course_id"],
        "week_number": request["week_number"],
    }

    logger.info(
        "Invoking AgentCore course_id=%s week_number=%s question_chars=%d",
        course_id,
        request["week_number"],
        len(request["user_question"]),
    )

    try:
        response = agentcore_client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            runtimeSessionId=session_id,
            payload=json.dumps(payload),
            qualifier="DEFAULT",
        )
        response_data = json.loads(response["response"].read())
    except (BotoCoreError, ClientError, KeyError, ValueError) as err:
        # Details stay in the logs; the client only receives a generic message.
        logger.error("AgentCore invocation failed: %s: %s", type(err).__name__, err)
        return _response(500, {"error": GENERIC_ERROR})

    if not isinstance(response_data, dict) or "bot_response" not in response_data:
        logger.error("AgentCore returned an error or unexpected response")
        message = response_data.get("error") if isinstance(response_data, dict) else None
        return _response(502, {"error": message or GENERIC_ERROR})

    bot_response = response_data["bot_response"]
    logger.info("AgentCore response received answer_chars=%d", len(str(bot_response)))
    return _response(200, {"bot_response": bot_response})
