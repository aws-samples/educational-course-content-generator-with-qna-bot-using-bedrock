# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import logging
import os

from helper import send_message_to_sqs

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def lambda_handler(event, context):
    request_context = event.get('requestContext', {})
    connection_id = request_context.get('connectionId', '')
    request_id = getattr(context, 'aws_request_id', '')
    logger.info("route=%s connectionId=%s requestId=%s bodyLength=%d",
                request_context.get('routeKey'), connection_id, request_id, len(event.get('body') or ''))

    # The request is processed asynchronously by the course_content_llm Lambda via SQS
    content_queue_url = os.environ["CONTENT_QUEUE_URL"]
    response = send_message_to_sqs(content_queue_url, event)
    logger.info("Course content request queued: connectionId=%s requestId=%s sqsMessageId=%s",
                connection_id, request_id, response.get('MessageId'))

    return {"statusCode": 200,
            "body": json.dumps({'course_content': response})
            }
