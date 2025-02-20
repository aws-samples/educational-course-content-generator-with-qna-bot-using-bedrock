## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import json
import boto3

sns_client = boto3.client('sns')
sqs_client = boto3.client("sqs")

def send_message_to_client(apigatewaymanagementapi_client, connection_id, response):
        apigatewaymanagementapi_client.post_to_connection(ConnectionId=connection_id, 
                                                          Data=json.dumps(response).encode('utf-8'))

def send_message_to_sqs(queue_url, event):
    message_body = json.dumps(event)
    response = sqs_client.send_message(
        QueueUrl=queue_url,
        MessageBody=(message_body)
    )
    print(f"Message body sent to SQS:: {message_body}")
    return response