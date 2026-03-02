"""
Streamlit Demo UI for the Educational QnA Bot.

A chat interface that authenticates via Cognito, connects to the QnA Bot
WebSocket API with a JWT Bearer token, and renders Markdown responses.

Usage:
    pip install -r demo/requirements.txt
    streamlit run demo/app.py

    # Or with a specific region:
    streamlit run demo/app.py -- --region us-west-2
"""
import streamlit as st
import boto3
import json
import ssl
import sys
import time
import websocket

# ──────────────────────────────────────────────────────────────────────
# Page config
# ──────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="QnA Bot – Educational Platform",
    page_icon="🎓",
    layout="wide",
)

# ──────────────────────────────────────────────────────────────────────
# Helpers: CloudFormation / Cognito / WebSocket discovery
# ──────────────────────────────────────────────────────────────────────
REGION = sys.argv[-1] if sys.argv[-1].startswith("us-") or sys.argv[-1].startswith("eu-") else "us-east-1"


@st.cache_data(ttl=3600, show_spinner=False)
def get_stack_outputs(stack_name: str) -> dict:
    """Read CloudFormation stack outputs."""
    cf = boto3.client("cloudformation", region_name=REGION)
    resp = cf.describe_stacks(StackName=stack_name)
    return {o["OutputKey"]: o["OutputValue"] for o in resp["Stacks"][0].get("Outputs", [])}


@st.cache_data(ttl=3600, show_spinner=False)
def get_ws_endpoint(api_name: str) -> str | None:
    """Find the WebSocket API endpoint by name."""
    apigw = boto3.client("apigatewayv2", region_name=REGION)
    for api in apigw.get_apis().get("Items", []):
        if api["Name"] == api_name:
            return f"wss://{api['ApiId']}.execute-api.{REGION}.amazonaws.com/dev"
    return None


def cognito_authenticate(user_pool_id: str, client_id: str,
                         username: str, password: str, name: str) -> str | None:
    """Authenticate with Cognito and return the JWT IdToken."""
    cog = boto3.client("cognito-idp", region_name=REGION)

    # Try to create user (idempotent if exists)
    try:
        cog.admin_get_user(UserPoolId=user_pool_id, Username=username)
    except cog.exceptions.UserNotFoundException:
        cog.admin_create_user(
            UserPoolId=user_pool_id, Username=username,
            TemporaryPassword=password,
            UserAttributes=[{"Name": "name", "Value": name}],
            MessageAction="SUPPRESS",
        )

    # Authenticate
    resp = cog.initiate_auth(
        ClientId=client_id, AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )

    # Handle first-login password challenge
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
    """Open a WebSocket connection, send a message, receive the response, and close."""
    url = f"{ws_url}?Authorization=Bearer%20{token}"
    ws = websocket.create_connection(
        url,
        header=[f"Authorization: Bearer {token}"],
        sslopt={"cert_reqs": ssl.CERT_NONE},
        timeout=120,
    )
    try:
        ws.send(json.dumps(payload))
        result = ws.recv()
        return json.loads(result)
    finally:
        ws.close()


# ──────────────────────────────────────────────────────────────────────
# Sidebar: Authentication & Course Settings
# ──────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🎓 QnA Bot Demo")
    st.markdown("---")

    # Load infrastructure details
    try:
        course_outputs = get_stack_outputs("CourseStack")
        qna_outputs = get_stack_outputs("QnAStack")
        ws_endpoint = get_ws_endpoint("QnAWSApi")

        user_pool_arn = course_outputs.get("UserPoolArn", "")
        user_pool_id = user_pool_arn.split("/")[-1] if user_pool_arn else ""
        client_id = course_outputs.get("UserPoolClientId", "")
        qna_mode = qna_outputs.get("QnABotMode", "unknown")

        st.success(f"✅ Infrastructure detected")
        st.caption(f"Mode: **{qna_mode}** | Region: `{REGION}`")
        infra_ready = True
    except Exception as e:
        st.error(f"❌ Could not read stack outputs: {e}")
        st.info("Make sure `CourseStack` and `QnAStack` are deployed and AWS credentials are configured.")
        infra_ready = False

    st.markdown("---")
    st.subheader("🔐 Authentication")

    username = st.text_input("Username", value="testuser")
    password = st.text_input("Password", value="TestUser@2026!", type="password")

    if st.button("🔑 Login", use_container_width=True, disabled=not infra_ready):
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

    # Course catalog derived from kb_dataset metadata
    COURSES = {
        "Fundamentals of Machine Learning": {"course_id": "Dummy-c001", "weeks": 2},
        "Strategic Balance Sheet Analysis for Investment Decision Making": {"course_id": "Dummy-c002", "weeks": 4},
    }

    course_name = st.selectbox("Course Name", options=list(COURSES.keys()))
    course_info = COURSES[course_name]
    course_id = st.text_input("Course ID", value=course_info["course_id"], disabled=True)
    week_number = st.selectbox("Week Number", options=list(range(1, course_info["weeks"] + 1)), index=course_info["weeks"] - 1)

    st.markdown("---")
    if st.button("🗑️ Clear Chat", use_container_width=True):
        st.session_state["messages"] = []
        st.rerun()

# ──────────────────────────────────────────────────────────────────────
# Main: Chat Interface
# ──────────────────────────────────────────────────────────────────────
st.title("🤖 Educational QnA Bot")

if not st.session_state.get("authenticated"):
    st.info("👈 Please log in using the sidebar to start chatting.")
    st.stop()

st.caption(f"📡 Connected via WebSocket to **{ws_endpoint}** | Course: *{course_name}*")

# Initialize chat history
if "messages" not in st.session_state:
    st.session_state["messages"] = []

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

    # Send to WebSocket and stream the response
    with st.chat_message("assistant"):
        # Show spinner while waiting for the agent response
        with st.spinner("Thinking..."):
            try:
                payload = {
                    "action": "qnaBot",
                    "user_question": prompt,
                    "course_name": course_name,
                    "course_id": course_id,
                    "week_number": week_number,
                }
                result = send_ws_message(
                    ws_endpoint,
                    st.session_state["jwt_token"],
                    payload,
                )

                # Extract the bot response
                if isinstance(result, dict):
                    bot_response = result.get("bot_response", json.dumps(result, indent=2))
                else:
                    bot_response = str(result)

            except websocket.WebSocketTimeoutException:
                bot_response = None
                error_msg = "⏱️ The request timed out. The agent may need more time to process complex questions."
                st.error(error_msg)
                st.session_state["messages"].append({"role": "assistant", "content": error_msg})
            except Exception as e:
                bot_response = None
                error_msg = f"❌ Error: {str(e)}"
                st.error(error_msg)
                st.session_state["messages"].append({"role": "assistant", "content": error_msg})

        # Stream the response word-by-word with a typing effect
        if bot_response:
            def stream_response():
                """Generator that yields words with a small delay for streaming effect."""
                for word in bot_response.split(" "):
                    yield word + " "
                    time.sleep(0.02)

            st.write_stream(stream_response)
            st.session_state["messages"].append({"role": "assistant", "content": bot_response})
