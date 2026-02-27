## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
"""
Bedrock AgentCore Runtime entry point for the Strands Agent QnA Bot.

This module provides the entrypoint handler for deploying the QnA bot
as a Bedrock AgentCore Runtime service. It uses the bedrock-agentcore SDK
to expose the agent as an HTTP service with /invocations and /ping endpoints.

Usage:
    Deploy to AgentCore Runtime using the CDK construct or starter toolkit.
    The agent is invoked via the /invocations POST endpoint.

    Example invocation payload:
    {
        "prompt": "What is machine learning?",
        "course_name": "Fundamentals of Machine Learning",
        "course_id": "ML-001",
        "week_number": 2
    }
"""
import json
import os
import logging

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent
from strands.models import BedrockModel
from strands_tools import retrieve

from helper import build_retrieve_filter, build_system_prompt

logger = logging.getLogger()
logger.setLevel(logging.INFO)

app = BedrockAgentCoreApp()


def _create_agent():
    """Create and configure the Strands Agent with Bedrock model and retrieve tool."""
    model_id = os.getenv("QnA_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    guardrail_id = os.getenv("GUARDRAIL_ID", "")
    guardrail_version = os.getenv("GUARDRAIL_VERSION", "")

    bedrock_model_kwargs = {
        "model_id": model_id,
        "temperature": 0,
    }
    if guardrail_id and guardrail_version:
        bedrock_model_kwargs["guardrail_id"] = guardrail_id
        bedrock_model_kwargs["guardrail_version"] = guardrail_version

    bedrock_model = BedrockModel(**bedrock_model_kwargs)

    return bedrock_model


@app.entrypoint
def invoke(payload):
    """Process student questions using the Strands Agent with KB retrieval.

    Args:
        payload: Dictionary containing:
            - prompt (str): The student's question (alternative to user_question)
            - user_question (str): The student's question
            - course_name (str): Name of the course
            - course_id (str): Unique course identifier
            - week_number (int): Week number for content filtering
            - learning_objective (str): Current learning objective
            - session_id (str): Optional session identifier

    Returns:
        Dictionary with bot_response containing the agent's answer.
    """
    # Extract parameters from payload
    user_question = payload.get("user_question") or payload.get("prompt", "")
    course_name = payload.get("course_name", None)
    learning_objective = payload.get("learning_objective", None)
    course_id = payload.get("course_id", None)
    week_number = payload.get("week_number", None)

    # KB_ID is set as an environment variable on the AgentCore Runtime
    kb_id = os.getenv("KB_ID", "")

    logger.info("Processing question: %s for course: %s", user_question, course_name)

    # Build the agent
    bedrock_model = _create_agent()
    system_prompt = build_system_prompt(course_name, learning_objective)

    # Set KB ID as environment variable for the retrieve tool
    os.environ["KNOWLEDGE_BASE_ID"] = kb_id

    agent = Agent(
        model=bedrock_model,
        system_prompt=system_prompt,
        tools=[retrieve],
    )

    # Build retrieval filter and prompt
    retrieve_filter = build_retrieve_filter(course_name, course_id, week_number)

    prompt_parts = [
        f'A student has asked the following question: "{user_question}"',
        "",
        "Please use the retrieve tool to search the knowledge base for relevant information.",
        f"Use knowledge base ID: {kb_id}",
        "Set numberOfResults to 3 for focused results.",
    ]

    if retrieve_filter:
        prompt_parts.append(f"Apply this filter: {json.dumps(retrieve_filter)}")

    prompt_parts.extend([
        "",
        "After retrieving, provide a comprehensive answer based solely on the retrieved content.",
        "If results don't contain relevant information, state that clearly.",
    ])

    agent_prompt = "\n".join(prompt_parts)

    # Invoke the agent
    response = agent(agent_prompt)
    output_text = str(response)

    logger.info("Agent response generated successfully")

    return {"bot_response": output_text}


if __name__ == "__main__":
    app.run()
