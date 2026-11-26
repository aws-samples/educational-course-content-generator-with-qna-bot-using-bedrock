# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the AgentCore Runtime entry point (lambda/qna_bot_agentcore/agentcore_handler.py).

The handler is loaded from its file because ``lambda`` is a reserved word and cannot be imported as a
package. The Strands and AgentCore SDKs are only bundled into the runtime artifact, so they are replaced
by small stand-ins: the tests cover what the handler itself decides (which guardrail options it passes,
what it leaves in the conversation history and how it labels the retrieved passages), not the SDKs.
"""
import importlib.util
import os
import sys
import types
from pathlib import Path

import boto3
import pytest

AGENT_DIR = Path(__file__).resolve().parents[2] / "lambda" / "qna_bot_agentcore"
# Stands for what the stack passes to the runtime in the USER_AGENT_STRING environment variable. The agent
# hands on whatever the variable holds, so these tests do not need the solution block of project_config.json.
USER_AGENT = "example-solution/0000/v0.0.0"
KNOWLEDGE_BASE_QUERY = {"knowledgeBaseId": "KB00000001", "retrievalQuery": {"text": "question"}}
REQUEST = {
    "user_question": "What is machine learning?",
    "course_name": "Fundamentals of Machine Learning",
    "course_id": "ML-001",
    "week_number": 2,
}


class FakeModel:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeAgent:
    """Like the Strands agent: the question is added to the history before the model is called and
    stays there when the call fails."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.messages = []
        self.fail_next_call = False

    def __call__(self, question):
        self.messages.append({"role": "user", "content": [{"text": question}]})
        if self.fail_next_call:
            self.fail_next_call = False
            raise RuntimeError("model call failed")
        self.messages.append({"role": "assistant", "content": [{"text": "answer"}]})
        return "answer"


class FakeApp:
    def entrypoint(self, function):
        return function

    def run(self):
        raise AssertionError("the tests do not start the HTTP service")


def load_handler(patch, guardrail_id="gr-0001", guardrail_version="3", user_agent=USER_AGENT):
    strands = types.ModuleType("strands")
    strands.Agent = FakeAgent
    strands.tool = lambda function: function
    strands_models = types.ModuleType("strands.models")
    strands_models.BedrockModel = FakeModel
    agentcore = types.ModuleType("bedrock_agentcore")
    agentcore_runtime = types.ModuleType("bedrock_agentcore.runtime")
    agentcore_runtime.BedrockAgentCoreApp = FakeApp
    for name, module in (("strands", strands), ("strands.models", strands_models),
                         ("bedrock_agentcore", agentcore), ("bedrock_agentcore.runtime", agentcore_runtime)):
        patch.setitem(sys.modules, name, module)
    # Keep the imports from writing __pycache__ into the folder that CDK packages as the agent code
    patch.setattr(sys, "dont_write_bytecode", True)
    # The handler imports its helper module from its own directory. Other Lambda folders have a helper
    # module of their own, so it is registered through the patch, which removes it again after the test.
    helper_spec = importlib.util.spec_from_file_location("helper", AGENT_DIR / "helper.py")
    helper = importlib.util.module_from_spec(helper_spec)
    helper_spec.loader.exec_module(helper)
    patch.setitem(sys.modules, "helper", helper)

    patch.setenv("KNOWLEDGE_BASE_ID", "KB00000001")
    patch.setenv("QnA_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
    patch.setenv("GUARDRAIL_ID", guardrail_id)
    patch.setenv("GUARDRAIL_VERSION", guardrail_version)
    if user_agent is None:
        patch.delenv("USER_AGENT_STRING", raising=False)
    else:
        patch.setenv("USER_AGENT_STRING", user_agent)
    # boto3 needs a region to create the client; placeholder credentials and no configuration files
    # keep the AWS setup of this machine out of it
    patch.setenv("AWS_REGION", "us-east-1")
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        patch.setenv(name, "testing")
    for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
        patch.delenv(name, raising=False)
    for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
        patch.setenv(name, os.devnull)
    patch.setattr(boto3, "DEFAULT_SESSION", None)
    spec = importlib.util.spec_from_file_location("agentcore_handler_under_test", AGENT_DIR / "agentcore_handler.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def handler():
    with pytest.MonkeyPatch.context() as patch:
        yield load_handler(patch)


def context(session_id="session-1"):
    return types.SimpleNamespace(session_id=session_id)


def test_guardrail_screens_only_the_newest_question(handler):
    """Without guardrail_latest_message the whole conversation is screened again as input on every turn, and an
    earlier answer about investment strategy blocks every follow-up question of a finance conversation."""
    assert handler.MODEL.kwargs["guardrail_id"] == "gr-0001"
    assert handler.MODEL.kwargs["guardrail_version"] == "3"
    assert handler.MODEL.kwargs["guardrail_latest_message"] is True


def test_no_guardrail_options_without_a_configured_guardrail():
    with pytest.MonkeyPatch.context() as patch:
        handler = load_handler(patch, guardrail_id="", guardrail_version="")
        assert not [key for key in handler.MODEL.kwargs if key.startswith("guardrail")]


def test_model_client_configuration_carries_the_user_agent_suffix_and_the_read_timeout(handler):
    """Strands creates the Bedrock Runtime client of the model. Given a client configuration, it appends its
    own user-agent token to the suffix of that configuration, and it no longer sets its read timeout of 120
    seconds, so the configuration states both."""
    configuration = handler.MODEL.kwargs["boto_client_config"]
    assert configuration.user_agent_extra == USER_AGENT
    assert configuration.read_timeout == 120


def test_knowledge_base_client_sends_the_user_agent_suffix_once(handler, sent_user_agent):
    user_agent = sent_user_agent(handler.bedrock_agent_runtime, "retrieve", **KNOWLEDGE_BASE_QUERY)
    assert user_agent.split().count(USER_AGENT) == 1, user_agent


def test_agent_starts_without_the_user_agent_variable(sent_user_agent):
    """Without the solution block the stack does not set the variable. The agent must still start, and its
    requests must then carry no solution token."""
    prefix = USER_AGENT.split("/")[0] + "/"
    with pytest.MonkeyPatch.context() as patch:
        handler = load_handler(patch, user_agent=None)
        configuration = handler.MODEL.kwargs["boto_client_config"]
        assert not configuration.user_agent_extra
        assert configuration.read_timeout == 120
        user_agent = sent_user_agent(handler.bedrock_agent_runtime, "retrieve", **KNOWLEDGE_BASE_QUERY)
    assert not [field for field in user_agent.split() if field.startswith(prefix)], user_agent


def test_answered_turns_stay_in_the_conversation_history(handler):
    assert handler.invoke(dict(REQUEST), context()) == {"bot_response": "answer"}
    assert handler.invoke(dict(REQUEST, user_question="And deep learning?"), context()) == {"bot_response": "answer"}
    (agent, _), _ = handler._get_session("session-1")
    assert [message["role"] for message in agent.messages] == ["user", "assistant", "user", "assistant"]


def test_failed_turn_leaves_no_question_in_the_conversation_history(handler):
    """Only the newest question is screened by the guardrail. A question that a failed model call left in the
    history would reach the model on the next turn without ever having been screened."""
    handler.invoke(dict(REQUEST), context())
    (agent, _), _ = handler._get_session("session-1")
    history_before = list(agent.messages)

    agent.fail_next_call = True
    response = handler.invoke(dict(REQUEST, user_question="A question whose model call fails"), context())

    assert response == {"error": handler.GENERIC_ERROR}
    assert agent.messages == history_before
    # The conversation continues normally afterwards
    assert handler.invoke(dict(REQUEST, user_question="And deep learning?"), context()) == {"bot_response": "answer"}
    questions = [message["content"][0]["text"] for message in agent.messages if message["role"] == "user"]
    assert questions == ["What is machine learning?", "And deep learning?"]


def test_search_results_are_labelled_with_title_and_whole_week_number(handler):
    """The knowledge base returns the week as a float; the model should cite "week 1", not "week 1.0", and the
    passage is labelled with its document title, not with its S3 location."""
    results = [{
        "content": {"text": "Supervised learning uses labeled data."},
        "location": {"s3Location": {"uri": "s3://amzn-s3-demo-bucket/final-course-content/doc.docx"}},
        "metadata": {"learning_objective": "Explore supervised learning", "week": 1.0},
    }]
    formatted = handler._format_results(results)
    assert formatted.startswith("[1] Source: Explore supervised learning (week 1)\n")
    assert "1.0" not in formatted and "s3://" not in formatted
    assert handler._format_results([]) == "No matching course materials were found."


def test_invalid_request_is_rejected_before_a_session_is_created(handler):
    response = handler.invoke({"user_question": "What is machine learning?"}, context("session-2"))
    assert "error" in response and "course" in response["error"]
    assert "session-2" not in handler._SESSIONS
