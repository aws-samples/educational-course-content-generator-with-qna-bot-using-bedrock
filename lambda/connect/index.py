## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import os
import json
import boto3
from datetime import datetime, timedelta, timezone

dynamodb = boto3.client('dynamodb')


def lambda_handler(event, context):
    print(event)

    CONNECTIONS_TABLE = os.environ.get('CONNECTIONS_TABLE')
    connection_id = event['requestContext']['connectionId']

    now = datetime.now(timezone.utc)
    future_time = now + timedelta(days=1)
    ttl = int(future_time.timestamp())

    try:
        dynamodb.put_item(
            TableName=CONNECTIONS_TABLE,
            Item={
                'connectionId': {'S': connection_id},
                'ttl': {'N': str(ttl)}
            }
        )
        print(f"Connection ID {connection_id} added.")

        return {
            'statusCode': 200, 
            'body': 'Connected.'
            }
    
    except Exception as e:
        print(f"Error adding connection ID: {e}")
        return {
            'statusCode': 500, 
            'body': 'Failed to connect.'
            }
    
if __name__ == "__main__":
    event = None
    lambda_handler(event, None)