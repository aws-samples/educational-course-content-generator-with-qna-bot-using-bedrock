# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Streamlit Demo UI for the Educational QnA Bot.

A chat interface that authenticates via Amazon Cognito, connects to the QnA Bot
WebSocket API through its CloudFront distribution with a JWT Bearer token, and
renders the Markdown answers with a typing effect.

Usage (run from the repository root so that .streamlit/config.toml applies):
    pip install -r demo/requirements.txt
    streamlit run demo/app.py

    # Or with a specific region (defaults to the region of your AWS profile):
    streamlit run demo/app.py -- --region us-east-1
"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import boto3
import streamlit as st
import websocket

# The repository root, so that the solution module is found from whichever directory this file is run
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from educational_course_content_generator_with_qna_bot_using_bedrock.solution import get_client  # noqa: E402

# API Gateway WebSocket integrations time out after 29 seconds. Wait a little longer
# so that the gateway's own error frame can still be received and explained.
WS_TIMEOUT_SECONDS = 35
FALLBACK_REGION = "us-east-1"

# Course catalog matching the sample kb_dataset metadata
COURSES = {
    "Fundamentals of Machine Learning": {"course_id": "Dummy-c001", "weeks": 2},
    "Strategic Balance Sheet Analysis for Investment Decision Making": {"course_id": "Dummy-c002", "weeks": 4},
}

# ──────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="QnA Bot – Educational Platform",
    page_icon="🎓",
    layout="wide",
)


# ──────────────────────────────────────────────────────────────────────
# Helpers: region, CloudFormation / Cognito / WebSocket
# ──────────────────────────────────────────────────────────────────────
def resolve_region() -> str:
    """Use `-- --region <name>` when given, else the boto3 session region, else us-east-1."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--region")
    args, _unknown = parser.parse_known_args(sys.argv[1:])
    return args.region or boto3.session.Session().region_name or FALLBACK_REGION


REGION = resolve_region()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stack_outputs(stack_name: str, region: str) -> dict:
    """Read CloudFormation stack outputs."""
    cf = get_client("cloudformation", region_name=region)
    resp = cf.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


def cognito_authenticate(user_pool_id: str, client_id: str,
                         username: str, password: str, name: str) -> str | None:
    """Authenticate with Cognito and return the JWT IdToken (creates the user on first login)."""
    cog = get_client("cognito-idp", region_name=REGION)

    # Create the user if it does not exist yet (idempotent)
    try:
        cog.admin_get_user(UserPoolId=user_pool_id, Username=username)
    except cog.exceptions.UserNotFoundException:
        cog.admin_create_user(
            UserPoolId=user_pool_id, Username=username,
            TemporaryPassword=password,
            UserAttributes=[{"Name": "name", "Value": name}],
            MessageAction="SUPPRESS",
        )

    resp = cog.initiate_auth(
        ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )

    # First login with a temporary password: make it the permanent password
    if resp.get("ChallengeName") == "NEW_PASSWORD_REQUIRED":
        resp = cog.respond_to_auth_challenge(
            ClientId=client_id, ChallengeName="NEW_PASSWORD_REQUIRED",
            ChallengeResponses={
                "USERNAME": username, "NEW_PASSWORD": password,
                "userAttributes.name": name,
            },
            Session=resp["Session"],
        )

    return resp.get("AuthenticationResult", {}).get("IdToken")


def send_ws_message(ws_url: str, token: str, payload: dict) -> dict:
    """Open a WebSocket connection, send one message, return the parsed reply and close."""
    # TLS certificate verification stays at the library default (enabled) and the
    # token travels in the Authorization header only, never in the URL.
    ws = websocket.create_connection(
        ws_url,
        header=[f"Authorization: Bearer {token}"],
        timeout=WS_TIMEOUT_SECONDS,
    )
    try:
        ws.send(json.dumps(payload))
        return json.loads(ws.recv())
    finally:
        ws.close()


def interpret_reply(result) -> tuple[str | None, str | None]:
    """Split a WebSocket reply into (bot_response, friendly error message)."""
    if isinstance(result, dict):
        if result.get("bot_response"):
            return str(result["bot_response"]), None
        if result.get("error"):
            return None, f"The QnA bot could not answer: {result['error']}"
        if "timed out" in str(result.get("message", "")).lower():
            # API Gateway sends this frame when the integration exceeds its 29-second limit
            return None, (
                "The WebSocket API closed the request after its 29-second limit before the "
                "agent finished. Please try again with a shorter or more specific question."
            )
    return None, "The QnA bot did not return an answer. Please try again."


# ──────────────────────────────────────────────────────────────────────
# Sidebar: Authentication & Course Settings
# ──────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🎓 QnA Bot Demo")
    st.markdown("---")

    # Load infrastructure details from the CloudFormation outputs
    ws_endpoint = user_pool_id = client_id = None
    try:
        course_outputs = get_stack_outputs("CourseStack", REGION)
        qna_outputs = get_stack_outputs("QnAStack", REGION)

        user_pool_arn = course_outputs.get("UserPoolArn", "")
        user_pool_id = user_pool_arn.split("/")[-1] if user_pool_arn else ""
        client_id = course_outputs.get("UserPoolClientId", "")
        qna_mode = qna_outputs.get("QnABotMode", "unknown")
        # Connect through CloudFront (WAF-protected); its origin path maps to the API's /dev stage
        cloudfront_domain = qna_outputs.get("QnACloudFrontDomainName", "")
        if not (user_pool_id and client_id and cloudfront_domain):
            raise KeyError("UserPoolArn, UserPoolClientId or QnACloudFrontDomainName output is missing")
        ws_endpoint = f"wss://{cloudfront_domain}"

        st.success("✅ Infrastructure detected")
        st.caption(f"Mode: **{qna_mode}** | Region: `{REGION}`")
        infra_ready = True
    except Exception as e:
        st.error(f"❌ Could not read stack outputs: {e}")
        st.info("Make sure `CourseStack` and `QnAStack` are deployed and AWS credentials are configured.")
        infra_ready = False

    st.markdown("---")
    st.subheader("🔐 Authentication")

    username = st.text_input("Username", value="testuser")
    password = st.text_input("Password", type="password",
                             help="Password of your Cognito user (nothing is pre-filled).")

    login_disabled = not (infra_ready and username and password)
    if st.button("🔑 Login", width="stretch", disabled=login_disabled):
        with st.spinner("Authenticating..."):
            try:
                token = cognito_authenticate(user_pool_id, client_id, username, password, username)
                if token:
                    st.session_state["jwt_token"] = token
                    st.session_state["authenticated"] = True
                    st.success(f"✅ Logged in as **{username}**")
                else:
                    st.error("Authentication failed — no token returned.")
            except Exception as e:
                st.error(f"Login failed: {e}")

    st.markdown("---")
    st.subheader("📚 Course Settings")

    course_name = st.selectbox("Course Name", options=list(COURSES.keys()))
    course_info = COURSES[course_name]
    course_id = st.text_input("Course ID", value=course_info["course_id"], disabled=True)
    week_number = st.selectbox("Week Number", options=list(range(1, course_info["weeks"] + 1)),
                               index=course_info["weeks"] - 1)

    st.markdown("---")
    if st.button("🗑️ Clear Chat", width="stretch"):
        st.session_state["messages"] = []
        # A new conversation id gives the agent a fresh session for this user and course
        st.session_state["conversation_id"] = uuid.uuid4().hex
        st.rerun()

# ──────────────────────────────────────────────────────────────────────
# Main: Chat Interface
# ──────────────────────────────────────────────────────────────────────
st.title("🤖 Educational QnA Bot")

if not st.session_state.get("authenticated"):
    st.info("👈 Please log in using the sidebar to start chatting.")
    st.stop()

st.caption(f"📡 Connected via WebSocket to **{ws_endpoint}** | Course: *{course_name}*")

# Initialize chat history and the conversation id sent with every question
if "messages" not in st.session_state:
    st.session_state["messages"] = []
if "conversation_id" not in st.session_state:
    st.session_state["conversation_id"] = uuid.uuid4().hex

# Display chat history
for msg in st.session_state["messages"]:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# Chat input
if prompt := st.chat_input("Ask a question about the course..."):
    # Show user message
    st.session_state["messages"].append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Send the question over the WebSocket and wait for the single reply frame
    with st.chat_message("assistant"):
        bot_response = error_msg = None
        with st.spinner("Thinking..."):
            try:
                payload = {
                    "action": "qnaBot",
                    "user_question": prompt,
                    "course_name": course_name,
                    "course_id": course_id,
                    "week_number": week_number,
                    "conversation_id": st.session_state["conversation_id"],
                }
                result = send_ws_message(ws_endpoint, st.session_state["jwt_token"], payload)
                bot_response, error_msg = interpret_reply(result)
            except websocket.WebSocketTimeoutException:
                error_msg = (
                    f"⏱️ No reply within {WS_TIMEOUT_SECONDS} seconds. The WebSocket API caps each "
                    "request at 29 seconds; try a shorter or more specific question."
                )
            except Exception as e:
                error_msg = f"❌ Error: {e}"

        if bot_response:
            # The API is request/response, so the complete answer is replayed word by word
            # as a typing effect (this is not token streaming from the model).
            def typing_effect():
                for word in bot_response.split(" "):
                    yield word + " "
                    time.sleep(0.02)

            st.write_stream(typing_effect)
            st.session_state["messages"].append({"role": "assistant", "content": bot_response})
        else:
            st.error(error_msg)
            st.session_state["messages"].append({"role": "assistant", "content": error_msg})
