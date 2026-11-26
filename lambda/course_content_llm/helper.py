# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import logging
import os
from io import BytesIO
from urllib.parse import urlparse, unquote_plus

import boto3
from botocore.config import Config
import PyPDF2
from langchain_core.prompts import PromptTemplate
from pydantic_utils import convert_pydantic_to_bedrock_converse_function

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# The stack passes the solution user-agent suffix in USER_AGENT_STRING (see "Solution ID and usage
# reporting" in the README). Without the variable the clients are created without a suffix.
SDK_CONFIG = Config(user_agent_extra=os.environ.get("USER_AGENT_STRING", ""))

# increase the standard time out limits in boto3, because Bedrock may take a while to respond to large requests.
my_config = Config(
    connect_timeout=60*5,
    read_timeout=60*5,
    user_agent_extra=os.environ.get("USER_AGENT_STRING", ""),
)

s3_client = boto3.client("s3", config=SDK_CONFIG)
bedrock_runtime_client = boto3.client(service_name='bedrock-runtime', config=my_config)

# Name of the only S3 bucket that requests may read reference files from: every entry of
# s3_input_uri_list must reference this bucket. Fail fast at start-up when it is not configured
# instead of silently accepting files from arbitrary buckets.
INPUT_BUCKET_NAME = os.environ["INPUT_BUCKET_NAME"]

# Request limits
MAX_TEXT_FIELD_CHARS = 500
MAX_USER_PROMPT_CHARS = 8000
MIN_WEEKS = 1
MAX_WEEKS = 52

# Static system prompt. Request fields are never interpolated here; the course title, week, learning
# outcomes and additional context reach the model only through the user message template variables.
SYSTEM_PROMPT = """You are an AI assistant specialized in educational content creation.
Your task is to generate course materials based on given learning outcomes.
Produce concise, accurate, and engaging content suitable for college-level courses.
You may refer to additional context provided within <additional_context> tags if present.
Format your response in valid JSON for easy parsing and integration.
Respond only with the requested content, without any preamble or explanation."""


class RequestValidationError(ValueError):
    """Raised when a course-generation request is invalid. The message is safe to return to the client."""


def _validate_text(body, field, max_chars=MAX_TEXT_FIELD_CHARS):
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RequestValidationError(f"'{field}' is required and must be a non-empty string.")
    if len(value) > max_chars:
        raise RequestValidationError(f"'{field}' must be at most {max_chars} characters.")
    return value


def _validate_text_list(body, field, max_chars=MAX_TEXT_FIELD_CHARS):
    values = body.get(field)
    if not isinstance(values, list) or not values or not all(isinstance(value, str) and value.strip() for value in values):
        raise RequestValidationError(f"'{field}' is required and must be a non-empty list of non-empty strings.")
    if any(len(value) > max_chars for value in values):
        raise RequestValidationError(f"Each entry of '{field}' must be at most {max_chars} characters.")
    return values


def _validate_week_count(body, field):
    value = body.get(field)
    # str.isdecimal() accepts exactly the digit strings int() accepts; isdigit() is also True for
    # characters such as superscripts, which int() rejects with a bare ValueError.
    if isinstance(value, str) and value.strip().isdecimal():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_WEEKS <= value <= MAX_WEEKS:
        raise RequestValidationError(f"'{field}' must be an integer between {MIN_WEEKS} and {MAX_WEEKS}.")
    return value


def _validate_s3_input_uri_list(body):
    uri_list = body.get("s3_input_uri_list") or []
    if not isinstance(uri_list, list) or not all(isinstance(uri, str) for uri in uri_list):
        raise RequestValidationError("'s3_input_uri_list' must be a list of S3 URI strings.")
    for uri in uri_list:
        get_s3_bucket_and_key(uri)  # raises RequestValidationError for unsupported or disallowed URIs
    return uri_list


def validate_content_request(body):
    """Validate and normalise a courseContent request body; raises RequestValidationError."""
    if not isinstance(body, dict):
        raise RequestValidationError("Request body must be a JSON object.")
    return {
        "course_title": _validate_text(body, "course_title"),
        "week_number": _validate_week_count(body, "week_number"),
        "main_learning_outcome": _validate_text(body, "main_learning_outcome"),
        "sub_learning_outcome_list": _validate_text_list(body, "sub_learning_outcome_list"),
        "user_prompt": _validate_text(body, "user_prompt", MAX_USER_PROMPT_CHARS),
        "s3_input_uri_list": _validate_s3_input_uri_list(body),
        "is_streaming": "yes" if str(body.get("is_streaming", "no")).lower() == "yes" else "no",
    }


## write a function to write json file into s3 bucket
def write_json_to_s3(value_dict_, s3_bucket, result_json_folder):
    for key, value in value_dict_.items():
        file_name = f"{key}.json"
        json_data = value
        s3_key = f"{result_json_folder}/{file_name}"
        s3_client.put_object(Body=json.dumps(json_data), Bucket=s3_bucket, Key=s3_key)
    return True


def get_s3_bucket_and_key(s3_input_uri):
    parsed_uri = urlparse(s3_input_uri)

    if parsed_uri.scheme == 's3':
        # s3://bucket-name/key format
        bucket = parsed_uri.netloc
        key = parsed_uri.path.lstrip('/')
    elif parsed_uri.scheme == 'https':
        # https://bucket-name.s3.amazonaws.com/key format
        bucket = (parsed_uri.hostname or '').split('.')[0]
        key = parsed_uri.path.lstrip('/')
    else:
        raise RequestValidationError("Unsupported S3 URI format; use s3://<bucket>/<key>.")

    if not bucket or not key:
        raise RequestValidationError("S3 URIs must include both a bucket name and an object key.")

    if bucket != INPUT_BUCKET_NAME:
        raise RequestValidationError("S3 input files must be located in the solution's input bucket.")

    # decode URL-encoded characters in the key
    key = unquote_plus(key)

    return bucket, key


def extract_text_from_pdf(bucket, key):
    response = s3_client.get_object(Bucket=bucket, Key=key)
    pdf_content = response['Body'].read()

    pdf_file = BytesIO(pdf_content)
    pdf_reader = PyPDF2.PdfReader(pdf_file)
    text = ""
    for page in pdf_reader.pages:
        text += page.extract_text() + "\n"  # Add newline after each page's text
    return text


def save_json_to_s3(bucket, key, llm_json_response):
    # Convert the dictionary to a JSON string
    json_content = json.dumps(llm_json_response)
    # Save the JSON string to S3 (S3 expects bytes, so we encode the string to bytes)
    s3_client.put_object(Bucket=bucket, Key=key, Body=json_content.encode('utf-8'))


def render_user_prompt(user_prompt, **variables):
    """Render the caller-supplied prompt template with the request variables.

    The template is user input, so malformed braces (ValueError), unknown placeholders (KeyError)
    and positional placeholders such as '{}' (IndexError) are reported as a validation error
    instead of surfacing as an unhandled exception.
    """
    try:
        return PromptTemplate.from_template(user_prompt).format(**variables)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("user_prompt template could not be rendered (%s)", type(exc).__name__)
        allowed = ", ".join("{" + name + "}" for name in variables)
        raise RequestValidationError(
            f"'user_prompt' is not a valid template. Use only the placeholders {allowed} "
            "and write literal braces as '{{' and '}}'."
        ) from exc


def invoke_bedrock_converse_api(model_id, course_title, week_number, main_learning_outcome,
                                sub_learning_outcome_list, additional_context,
                                user_prompt, pydantic_classes, is_streaming):
    user_msg = render_user_prompt(user_prompt,
                                  course_title=course_title,
                                  week_number=week_number,
                                  main_learning_outcome=main_learning_outcome,
                                  sub_learning_outcome_list=sub_learning_outcome_list,
                                  additional_context=additional_context)

    messages = [{"role": "user",
                 "content": [{
                     "text": user_msg}
                     ]}
                ]

    # inference_config = {
    #         "temperature": 0.7,
    #         "topP": 0.7
    #     }

    tools = []
    for class_ in pydantic_classes:
        tools.append(convert_pydantic_to_bedrock_converse_function(class_))
    tool_config = {"tools": tools}

    if is_streaming == "yes":
        response = bedrock_runtime_client.converse_stream(
                modelId=model_id,
                messages=messages,
                system=[{"text": SYSTEM_PROMPT}],
                # inferenceConfig=inference_config,
                toolConfig=tool_config,
            )
    else:
        response = bedrock_runtime_client.converse(
            system=[{"text": SYSTEM_PROMPT}],
            modelId=model_id,
            messages=messages,
            # inferenceConfig=inference_config,
            toolConfig=tool_config,
        )
    return response


def parse_llm_response(response):
    ai_message = response["output"]["message"]
    output_text = ai_message["content"][0]["text"]
    try:
        output_json = json.loads(output_text)
    except json.JSONDecodeError:
        output_json = {"error": "LLM Output is not json parsable",
                       "LLM_Output": output_text}
    return output_json


def parse_bedrock_tool_response(response):
    value_dict_ = {}
    tool_name = ""

    stop_reason = response['stopReason']
    tool_requests = response['output']['message']['content']

    if stop_reason == 'tool_use':
        for tool_request in tool_requests:
            if 'toolUse' in tool_request:
                tool_name = tool_request['toolUse']["name"]
                tool_input = tool_request['toolUse']["input"]
                value_dict_[tool_name] = tool_input
                logger.info("Model used tool: tool=%s", tool_name)
        if len(tool_requests) == 1:
            logger.info("Model response contains a single content block")
    return value_dict_


def send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, response):
    apigatewaymanagementapi_client.post_to_connection(ConnectionId=connection_id,
                                                      Data=json.dumps(response).encode('utf-8'))


def process_stream_obj(response, apigatewaymanagementapi_client, connection_id):
    stop_reason = ""
    message = {}
    content = []
    message['content'] = content
    text = ''
    tool_use = {}

    # stream the response into a message; tool-input deltas are forwarded to the client as they arrive.
    for chunk in response['stream']:
        if 'messageStart' in chunk:
            message['role'] = chunk['messageStart']['role']
        elif 'contentBlockStart' in chunk:
            tool = chunk['contentBlockStart']['start']['toolUse']
            tool_use['toolUseId'] = tool['toolUseId']
            tool_use['name'] = tool['name']
        elif 'contentBlockDelta' in chunk:
            delta = chunk['contentBlockDelta']['delta']
            if 'toolUse' in delta:
                if 'input' not in tool_use:
                    tool_use['input'] = ''
                tool_use['input'] += delta['toolUse']['input']
                send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, delta['toolUse']['input'])
            elif 'text' in delta:
                text += delta['text']
        elif 'contentBlockStop' in chunk:
            if 'input' in tool_use:
                tool_use['input'] = json.loads(tool_use['input'])
                content.append({'toolUse': tool_use})
                tool_use = {}
            else:
                content.append({'text': text})
                text = ''

        elif 'messageStop' in chunk:
            stop_reason = chunk['messageStop']['stopReason']

    logger.info("Stream completed: stopReason=%s contentBlocks=%d", stop_reason, len(content))
    return stop_reason, message
