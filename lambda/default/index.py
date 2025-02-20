## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import time
import boto3
import json

# Create a client to interact with API Gateway Management API
def get_api_client(event):
    domain_name = event['requestContext']['domainName']
    stage = event['requestContext']['stage']
    return boto3.client('apigatewaymanagementapi',
                        endpoint_url=f'https://{domain_name}/{stage}')


def lambda_handler(event, context):
    print(f"Received event: {json.dumps(event)}")

    route_key = event.get('requestContext', {}).get('routeKey')

    connection_id = event['requestContext']['connectionId']
    api_client = get_api_client(event)

    if route_key == '$default':
        # Custom route handling for messages
        message = json.loads(event['body'])

        message = '''if you want to invoke courseOutline route please pass dictonary in following format {"action":"courseOutline", "user_prompt":"As a user..."} 
        If you want to invoke courseContent route please pass dictonary in following format {"action":"courseContent", "user_prompt":"As a user..."} 
        If you want to invoke qnaBot route please pass dictonary in following format {"action":"qnaBot", "user_question":"What is Machine..."}'''

        response_message = {
            'status': "you invoked the default route!! Check the correct ROUTE and WS endpoint",
            'message': message
        }

        # Send the response back to the client
        try:
            api_client.post_to_connection(
                ConnectionId=connection_id,
                Data=json.dumps(response_message).encode('utf-8')
            )
        except Exception as e:
            print(f"Failed to send message to client: {e}")

        return {'statusCode': 200, 'body': json.dumps(response_message).encode('utf-8')}

    else:
        return {'statusCode': 500, 'body': f'Unrecognized routeKey {route_key}'}


if __name__ == "__main__":
    event = None
    lambda_handler(event, None)