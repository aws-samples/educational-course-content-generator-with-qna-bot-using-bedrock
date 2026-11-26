# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Validation, filter and Bedrock RetrieveAndGenerate helpers for the classic QnA bot."""
import os

import boto3
from botocore.config import Config

MAX_QUESTION_CHARS = 4000
MAX_FIELD_CHARS = 200
MIN_WEEK_NUMBER = 1
MAX_WEEK_NUMBER = 52

REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
# The stack passes the solution user-agent suffix in USER_AGENT_STRING (see "Solution ID and usage
# reporting" in the README). Without the variable the clients are created without a suffix.
SDK_CONFIG = Config(user_agent_extra=os.environ.get("USER_AGENT_STRING", ""))
bedrock_agent_runtime_client = boto3.client("bedrock-agent-runtime", region_name=REGION, config=SDK_CONFIG)


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


def validate_request(body):
    """Validate the WebSocket message body; raises ValueError with a generic message."""
    if not isinstance(body, dict):
        raise ValueError("Request body must be a JSON object with user_question.")

    user_question = body.get("user_question")
    if not isinstance(user_question, str) or not user_question.strip() or len(user_question) > MAX_QUESTION_CHARS:
        raise ValueError(f"user_question must be a non-empty string of at most {MAX_QUESTION_CHARS} characters.")

    return {
        "user_question": user_question.strip(),
        "course_name": _required_text(body, "course_name"),
        "course_id": _required_text(body, "course_id"),
        "learning_objective": _optional_text(body, "learning_objective"),
        "week_number": _optional_week_number(body.get("week_number")),
    }


def get_filter_condition(course_name, course_id, week_number):
    """Build the metadata filter for the knowledge base query.

    Only values that are present become conditions. ``andAll`` requires at least two
    entries, so a single condition is returned on its own and None is returned when
    there is nothing to filter on.
    """
    conditions = []

    if course_name:
        conditions.append({"equals": {"key": "course_name", "value": course_name}})

    if course_id:
        conditions.append({"equals": {"key": "course_id", "value": course_id}})

    if week_number is not None:
        conditions.append({"lessThanOrEquals": {"key": "week", "value": int(week_number)}})

    if len(conditions) >= 2:
        return {"andAll": conditions}
    if conditions:
        return conditions[0]
    return None


def retrieve_from_kb(user_question, kb_id, model_arn, guardrail_id, guardrail_version,
                     prompt_template, filter_condition, num_of_results):
    """Call Bedrock RetrieveAndGenerate against the knowledge base."""
    vector_search = {
        "numberOfResults": num_of_results,
        "overrideSearchType": "HYBRID",
    }
    if filter_condition:
        vector_search["filter"] = filter_condition

    generation_config = {
        "inferenceConfig": {
            "textInferenceConfig": {
                "temperature": 0,  # not to hallucinate
            }
        },
        "promptTemplate": {"textPromptTemplate": prompt_template},
    }
    if guardrail_id and guardrail_version:
        generation_config["guardrailConfiguration"] = {
            "guardrailId": guardrail_id,
            "guardrailVersion": guardrail_version,
        }

    return bedrock_agent_runtime_client.retrieve_and_generate(
        input={"text": user_question},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": kb_id,
                "modelArn": model_arn,
                "generationConfiguration": generation_config,
                "retrievalConfiguration": {"vectorSearchConfiguration": vector_search},
            },
        },
    )
