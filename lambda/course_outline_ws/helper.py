# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import logging
import os

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# The stack passes the solution user-agent suffix in USER_AGENT_STRING (see "Solution ID and usage
# reporting" in the README). Without the variable the clients are created without a suffix.
SDK_CONFIG = Config(user_agent_extra=os.environ.get("USER_AGENT_STRING", ""))
sqs_client = boto3.client("sqs", config=SDK_CONFIG)


def send_message_to_client(apigatewaymanagementapi_client, connection_id, response):
    apigatewaymanagementapi_client.post_to_connection(ConnectionId=connection_id,
                                                      Data=json.dumps(response).encode('utf-8'))


def send_message_to_sqs(queue_url, event):
    message_body = json.dumps(event)
    response = sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=message_body
    )
    logger.info("Message sent to SQS: messageId=%s bodyLength=%d", response.get('MessageId'), len(message_body))
    return response
