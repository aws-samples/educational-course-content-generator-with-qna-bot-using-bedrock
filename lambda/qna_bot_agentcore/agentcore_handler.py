# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bedrock AgentCore Runtime entry point for the Strands Agent QnA bot.

The bedrock-agentcore SDK exposes this module as an HTTP service (/invocations and
/ping). The CDK stack starts it with ``opentelemetry-instrument agentcore_handler.py``.

One Strands agent is cached per AgentCore session (``context.session_id``), so the
conversation history is kept across turns for the lifetime of the session. The agent
answers only from a custom ``search_course_materials`` tool that queries the knowledge
base with the course/week filter applied server-side; the model never sees the
knowledge base id, result count or filter, and the user turn is only the question. Retrieved
passages are labelled with their document title rather than their S3 location, so answers do
not disclose bucket names or object keys.

Example invocation payload (forwarded by the WebSocket proxy Lambda):
    {
        "user_question": "What is machine learning?",
        "course_name": "Fundamentals of Machine Learning",
        "course_id": "ML-001",
        "week_number": 2
    }

Configuration (environment variables set by the CDK stack):
    KNOWLEDGE_BASE_ID and QnA_MODEL_ID are required; GUARDRAIL_ID, GUARDRAIL_VERSION,
    KB_MAX_RESULTS (default 3) and USER_AGENT_STRING (the solution user-agent suffix of
    the AWS SDK clients) are optional; the region comes from AWS_REGION or
    AWS_DEFAULT_REGION.
"""
import logging
import os
import posixpath
import threading
from urllib.parse import unquote, urlparse

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from strands import Agent, tool
from strands.models import BedrockModel

from helper import SYSTEM_PROMPT, build_retrieve_filter, validate_payload

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Fail fast on missing configuration instead of discovering it on the first request.
KB_ID = os.environ["KNOWLEDGE_BASE_ID"]
MODEL_ID = os.environ["QnA_MODEL_ID"]
KB_MAX_RESULTS = int(os.getenv("KB_MAX_RESULTS", "3"))
REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
GUARDRAIL_ID = os.getenv("GUARDRAIL_ID", "")
GUARDRAIL_VERSION = os.getenv("GUARDRAIL_VERSION", "")

MAX_RETRIEVAL_QUERY_CHARS = 1000
MAX_CACHED_SESSIONS = 100
GENERIC_ERROR = "The assistant could not answer right now. Please try again."

app = BedrockAgentCoreApp()

# The stack passes the solution user-agent suffix in USER_AGENT_STRING (see "Solution ID and usage
# reporting" in the README). Without the variable the clients are created without a suffix.
SOLUTION_USER_AGENT = os.environ.get("USER_AGENT_STRING", "")
# Read timeout that Strands sets on its Bedrock Runtime client. Strands sets it only when it is given
# no client configuration, so the configuration below states it.
MODEL_READ_TIMEOUT_SECONDS = 120

bedrock_agent_runtime = boto3.client(
    "bedrock-agent-runtime", region_name=REGION, config=Config(user_agent_extra=SOLUTION_USER_AGENT)
)

_model_kwargs = {"model_id": MODEL_ID, "temperature": 0, "region_name": REGION}
# Strands appends its own user-agent token to the suffix of this client configuration.
_model_kwargs["boto_client_config"] = Config(
    user_agent_extra=SOLUTION_USER_AGENT, read_timeout=MODEL_READ_TIMEOUT_SECONDS
)
if GUARDRAIL_ID and GUARDRAIL_VERSION:
    # guardrail_latest_message: as input, the guardrail screens only the newest question, on every
    # model call of its turn (so again after the search tool result) but not on later turns; it also
    # screens every answer. Otherwise the whole conversation is screened again as input on every
    # turn, and an earlier answer about investment strategy trips the denied topic for personalised
    # financial advice, which blocks every follow-up question of that conversation. A blocked
    # question is replaced in the history by a placeholder (Strands default), so it does not reach
    # the model later.
    _model_kwargs.update(
        guardrail_id=GUARDRAIL_ID,
        guardrail_version=GUARDRAIL_VERSION,
        guardrail_latest_message=True,
    )
MODEL = BedrockModel(**_model_kwargs)

# Per-session cache: session id -> (agent, scope). ``scope`` is mutable and refreshed on
# every request so that week/course changes within a session take effect immediately.
_SESSIONS: dict[str, tuple[Agent, dict]] = {}
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCK = threading.Lock()


def _source_title(result):
    """Return a citation title for a retrieved passage without exposing storage identifiers.

    The knowledge base metadata carries the document's learning objective, which doubles as its
    title. Otherwise fall back to the object's file name (no bucket, no prefix), so the model can
    cite the material while answers never reveal S3 bucket names or object keys.
    """
    metadata = result.get("metadata") or {}
    title = metadata.get("learning_objective")
    if isinstance(title, str) and title.strip():
        return title.strip()
    for value in (result.get("location") or {}).values():
        if isinstance(value, dict):
            uri = value.get("uri") or value.get("url")
            if isinstance(uri, str) and uri:
                file_name = posixpath.basename(unquote(urlparse(uri).path))
                if file_name:
                    return posixpath.splitext(file_name)[0] or file_name
    return "course material"


def _format_results(results):
    """Concatenate retrieved passages with their document titles for the model."""
    if not results:
        return "No matching course materials were found."
    parts = []
    for index, result in enumerate(results, start=1):
        text = (result.get("content") or {}).get("text", "")
        metadata = result.get("metadata") or {}
        header = f"[{index}] Source: {_source_title(result)}"
        week = metadata.get("week")
        if week is not None:
            # The knowledge base returns numeric metadata as floats; show "week 1", not "week 1.0"
            if isinstance(week, float) and week.is_integer():
                week = int(week)
            header += f" (week {week})"
        parts.append(f"{header}\n{text}")
    return "\n\n".join(parts)


def _make_search_tool(scope):
    """Create the knowledge base search tool bound to one session's mutable scope."""

    @tool
    def search_course_materials(query: str) -> str:
        """Search the course materials knowledge base for passages relevant to the question.

        The search is automatically restricted to the student's current course and week.

        Args:
            query: A focused search query describing the information to look up.
        """
        if not isinstance(query, str) or not query.strip():
            return "The search query must be a non-empty string."

        vector_search = {"numberOfResults": KB_MAX_RESULTS}
        if scope["filter"]:
            vector_search["filter"] = scope["filter"]

        try:
            response = bedrock_agent_runtime.retrieve(
                knowledgeBaseId=KB_ID,
                retrievalQuery={"text": query.strip()[:MAX_RETRIEVAL_QUERY_CHARS]},
                retrievalConfiguration={"vectorSearchConfiguration": vector_search},
            )
        except (BotoCoreError, ClientError) as err:
            logger.error("Knowledge base retrieval failed: %s", type(err).__name__)
            return "The course materials could not be searched right now."

        results = response.get("retrievalResults", [])
        logger.info("Knowledge base returned %d results", len(results))
        return _format_results(results)

    return search_course_materials


def _get_session(session_id):
    """Return the cached (agent, scope) pair and lock for a session, creating them on first use."""
    with _CACHE_LOCK:
        entry = _SESSIONS.get(session_id)
        if entry is None:
            if len(_SESSIONS) >= MAX_CACHED_SESSIONS:
                oldest = next(iter(_SESSIONS))
                _SESSIONS.pop(oldest, None)
                _SESSION_LOCKS.pop(oldest, None)
            scope = {"filter": None, "course_name": None, "learning_objective": None}
            agent = Agent(
                model=MODEL,
                system_prompt=SYSTEM_PROMPT,
                tools=[_make_search_tool(scope)],
                callback_handler=None,
            )
            entry = (agent, scope)
            _SESSIONS[session_id] = entry
            _SESSION_LOCKS[session_id] = threading.Lock()
            logger.info("Created agent for a new session")
        return entry, _SESSION_LOCKS[session_id]


@app.entrypoint
def invoke(payload, context):
    """Answer a student's question with the per-session Strands agent.

    Returns ``{"bot_response": <markdown>}`` or ``{"error": <generic message>}``.
    """
    try:
        request = validate_payload(payload)
    except ValueError as err:
        logger.warning("Rejected invalid payload: %s", err)
        return {"error": str(err)}

    session_id = getattr(context, "session_id", None) or "default"
    (agent, scope), lock = _get_session(session_id)

    logger.info(
        "Invoking agent course_id=%s week_number=%s question_chars=%d",
        request["course_id"],
        request["week_number"],
        len(request["user_question"]),
    )

    try:
        with lock:
            scope["filter"] = build_retrieve_filter(
                request["course_name"], request["course_id"], request["week_number"]
            )
            scope["course_name"] = request["course_name"]
            scope["learning_objective"] = request["learning_objective"]
            # The user turn is only the student's question; course/week scoping is applied in the tool.
            # Strands adds the question to the history before the first model call and keeps it when
            # that call fails. Only the newest question is screened, so a question left behind by a
            # failed turn would reach the model on the next turn without a guardrail verdict: put the
            # history back.
            history = list(agent.messages)
            try:
                result = agent(request["user_question"])
            except Exception:
                agent.messages[:] = history
                raise
    except Exception:
        logger.exception("Agent invocation failed")
        return {"error": GENERIC_ERROR}

    answer = str(result)
    logger.info("Agent response generated answer_chars=%d", len(answer))
    return {"bot_response": answer}


if __name__ == "__main__":
    app.run()
