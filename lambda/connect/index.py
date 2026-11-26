# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# The stack passes the solution user-agent suffix in USER_AGENT_STRING (see "Solution ID and usage
# reporting" in the README). Without the variable the clients are created without a suffix.
SDK_CONFIG = Config(user_agent_extra=os.environ.get("USER_AGENT_STRING", ""))
dynamodb = boto3.client('dynamodb', config=SDK_CONFIG)


def lambda_handler(event, context):
    request_context = event.get('requestContext', {})
    connection_id = request_context['connectionId']
    request_id = getattr(context, 'aws_request_id', '')
    logger.info("route=%s connectionId=%s requestId=%s",
                request_context.get('routeKey'), connection_id, request_id)

    connections_table = os.environ.get('CONNECTIONS_TABLE')

    now = datetime.now(timezone.utc)
    future_time = now + timedelta(days=1)
    ttl = int(future_time.timestamp())

    try:
        dynamodb.put_item(
            TableName=connections_table,
            Item={
                'connectionId': {'S': connection_id},
                'ttl': {'N': str(ttl)}
            }
        )
        logger.info("Connection registered: connectionId=%s requestId=%s", connection_id, request_id)

        return {
            'statusCode': 200,
            'body': 'Connected.'
        }

    except Exception:
        logger.exception("Failed to register connection: connectionId=%s requestId=%s",
                         connection_id, request_id)
        return {
            'statusCode': 500,
            'body': 'Failed to connect.'
        }
