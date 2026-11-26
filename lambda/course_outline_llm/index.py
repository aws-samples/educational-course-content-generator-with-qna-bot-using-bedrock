# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import logging
import os

import boto3

from CourseOutlinePydantic import CourseOutline
from helper import (
    SDK_CONFIG,
    RequestValidationError,
    extract_text_from_pdf,
    get_s3_bucket_and_key,
    invoke_bedrock_converse_api,
    parse_bedrock_tool_response,
    process_stream_obj,
    save_json_to_s3,
    send_message_to_ws_client,
    validate_outline_request,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

MAX_RETRIES = 2


def _parse_request(record):
    try:
        body = json.loads(record.get("body") or "{}")
    except json.JSONDecodeError as exc:
        raise RequestValidationError("Request body must be valid JSON.") from exc
    return validate_outline_request(body)


def _send_error(apigatewaymanagementapi_client, connection_id, request_id, message):
    try:
        send_message_to_ws_client(apigatewaymanagementapi_client, connection_id,
                                  response={"error": message, "request_id": request_id})
    except Exception:
        logger.exception("Failed to send error message to client: connectionId=%s requestId=%s",
                         connection_id, request_id)


def lambda_handler(event, context):
    request_id = getattr(context, "aws_request_id", "")
    # The WebSocket event is forwarded by the course_outline_ws Lambda through SQS
    record = json.loads(event['Records'][0]['body'])
    request_context = record.get('requestContext', {})
    connection_id = request_context.get('connectionId', '')
    logger.info("route=%s connectionId=%s requestId=%s", request_context.get('routeKey'), connection_id, request_id)

    model_id = os.getenv("MODEL_ID", "")
    websocket_endpoint_url = os.getenv("WEBSOCKET_ENDPOINT_URL", "")
    output_bucket = os.getenv("OUTPUT_BUCKET", "")
    apigatewaymanagementapi_client = boto3.client('apigatewaymanagementapi', endpoint_url=websocket_endpoint_url,
                                                  config=SDK_CONFIG)

    try:
        request = _parse_request(record)
    except RequestValidationError as exc:
        logger.warning("Rejected courseOutline request: connectionId=%s requestId=%s reason=%s",
                       connection_id, request_id, exc)
        _send_error(apigatewaymanagementapi_client, connection_id, request_id, str(exc))
        return {'statusCode': 400, 'body': json.dumps({'error': str(exc)})}

    s3_input_uri_list = request["s3_input_uri_list"]
    user_prompt = request["user_prompt"]
    course_title = request["course_title"]
    course_duration = request["course_duration"]
    is_streaming = request["is_streaming"]
    logger.info("courseOutline request accepted: connectionId=%s requestId=%s courseDuration=%d inputFiles=%d "
                "promptLength=%d titleLength=%d streaming=%s", connection_id, request_id, course_duration,
                len(s3_input_uri_list), len(user_prompt), len(course_title), is_streaming)

    course_outline = {}
    try:
        syllabus_text = ""
        for s3_input_uri in s3_input_uri_list:
            bucket, key = get_s3_bucket_and_key(s3_input_uri)
            if key.endswith('.pdf'):
                pdf_text = extract_text_from_pdf(bucket, key)
                syllabus_text = syllabus_text + pdf_text
        logger.info("Syllabus text extracted: requestId=%s syllabusLength=%d", request_id, len(syllabus_text))

        # Initialize the Pydantic model
        pydantic_classes = [CourseOutline]

        if is_streaming == "yes":
            converse_response = invoke_bedrock_converse_api(model_id, course_title, course_duration, syllabus_text,
                                                            user_prompt, pydantic_classes, is_streaming=is_streaming)
            stop_reason, message = process_stream_obj(converse_response, apigatewaymanagementapi_client, connection_id)
            if stop_reason == "tool_use":
                for content in message['content']:
                    if 'toolUse' in content:
                        tool = content['toolUse']
                        if tool['name'] == "CourseOutline":
                            course_outline = tool['input']

        else:
            count = 0
            while len(course_outline) == 0 and count < MAX_RETRIES:
                converse_response = invoke_bedrock_converse_api(model_id, course_title, course_duration, syllabus_text,
                                                                user_prompt, pydantic_classes, is_streaming=is_streaming)
                course_outline = parse_bedrock_tool_response(converse_response)
                count += 1

            if len(course_outline) != 0:
                send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, response=course_outline)
            else:
                _send_error(apigatewaymanagementapi_client, connection_id, request_id,
                            f"Unable to generate course outline after {MAX_RETRIES} attempts. Please try again.")

        logger.info("Course outline generated: connectionId=%s requestId=%s outlineKeys=%d",
                    connection_id, request_id, len(course_outline))

        # Save the course outline to S3 (only when something was generated)
        if len(course_outline) != 0:
            output_key = f"course_outline/{course_title}/course_outline.json"
            save_json_to_s3(output_bucket, output_key, course_outline)

    except RequestValidationError as exc:
        # Raised when the caller-supplied user_prompt template cannot be rendered
        logger.warning("Rejected courseOutline request: connectionId=%s requestId=%s reason=%s",
                       connection_id, request_id, exc)
        _send_error(apigatewaymanagementapi_client, connection_id, request_id, str(exc))
        return {'statusCode': 400, 'body': json.dumps({'error': str(exc)})}
    except Exception:
        # Log the detail, tell the client only that the request failed, then let SQS move the message to the DLQ
        logger.exception("Course outline generation failed: connectionId=%s requestId=%s", connection_id, request_id)
        _send_error(apigatewaymanagementapi_client, connection_id, request_id,
                    "Unable to generate the course outline. Please try again later.")
        raise

    return {'statusCode': 200,
            'body': json.dumps({
                        'course_outline': course_outline
                    })
        }
