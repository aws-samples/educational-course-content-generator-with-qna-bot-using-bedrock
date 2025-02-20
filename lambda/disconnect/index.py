## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import os
import json
import boto3
from datetime import datetime, timedelta

dynamodb = boto3.client('dynamodb')
CONNECTIONS_TABLE = os.environ.get('CONNECTIONS_TABLE')

def lambda_handler(event, context):
    print("Disconect_Lambda")
    print(event)
    print("*******______*******")

    connection_id = event['requestContext']['connectionId']

    try:
        dynamodb.delete_item(
            TableName=CONNECTIONS_TABLE,
            Key={
                'connectionId': {'S': connection_id}
            }
        )
        print(f"Connection ID {connection_id} removed.")
        return {
            'statusCode': 200, 
            'body': 'Disconnected.'
            }
    except Exception as e:
        print(f"Error removing connection ID: {e}")
        return {
            'statusCode': 500, 
            'body': 'Failed to disconnect.'
            }