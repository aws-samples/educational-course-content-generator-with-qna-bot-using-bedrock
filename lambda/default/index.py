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

HELP_MESSAGE = (
    'if you want to invoke courseOutline route please pass dictionary in following format '
    '{"action":"courseOutline", "user_prompt":"As a user..."}\n'
    'If you want to invoke courseContent route please pass dictionary in following format '
    '{"action":"courseContent", "user_prompt":"As a user..."}\n'
    'If you want to invoke qnaBot route please pass dictionary in following format '
    '{"action":"qnaBot", "user_question":"What is Machine..."}'
)


# Create a client to interact with API Gateway Management API
def get_api_client(event):
    domain_name = event['requestContext']['domainName']
    stage = event['requestContext']['stage']
    return boto3.client('apigatewaymanagementapi',
                        endpoint_url=f'https://{domain_name}/{stage}',
                        config=SDK_CONFIG)


def lambda_handler(event, context):
    request_context = event.get('requestContext', {})
    route_key = request_context.get('routeKey')
    connection_id = request_context['connectionId']
    request_id = getattr(context, 'aws_request_id', '')
    logger.info("route=%s connectionId=%s requestId=%s bodyLength=%d",
                route_key, connection_id, request_id, len(event.get('body') or ''))

    if route_key == '$default':
        response_message = {
            'status': "you invoked the default route!! Check the correct ROUTE and WS endpoint",
            'message': HELP_MESSAGE
        }

        # Send the response back to the client
        try:
            get_api_client(event).post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps(response_message).encode('utf-8')
            )
        except Exception:
            logger.exception("Failed to send message to client: connectionId=%s requestId=%s",
                             connection_id, request_id)

        return {'statusCode': 200, 'body': json.dumps(response_message)}

    logger.warning("Unrecognized routeKey=%s connectionId=%s requestId=%s", route_key, connection_id, request_id)
    return {'statusCode': 500, 'body': f'Unrecognized routeKey {route_key}'}
