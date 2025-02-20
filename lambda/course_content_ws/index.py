## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import json
import boto3
from helper import * 
import os

def lambda_handler(event, context):
    print(event)
    try:
        connection_id = event['requestContext']['connectionId']
        websocket_endpoint_url = os.getenv("WEBSOCKET_ENDPOINT_URL", "")
        content_queue_url = os.environ["CONTENT_QUEUE_URL"]

    except:
        connection_id = ""
        websocket_endpoint_url = ""
        content_queue_url = ""

    response = {"connection_id":connection_id, 'message':'Message received and is in processing '}
    
    # apigatewaymanagementapi_client = boto3.client('apigatewaymanagementapi', endpoint_url=websocket_endpoint_url)
    # send_message_to_client(apigatewaymanagementapi_client, connection_id, response=output_json)

    response = send_message_to_sqs(content_queue_url, event)

    return {"statusCode": 200,
            "body": json.dumps({'course_content': response})
        }


if __name__ == "__main__":
    event = None
    lambda_handler(event, None)