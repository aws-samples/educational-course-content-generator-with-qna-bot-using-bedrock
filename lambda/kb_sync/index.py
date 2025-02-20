import os
import json
import boto3
import hashlib

bedrock_agent_client = boto3.client('bedrock-agent')
def lambda_handler(event, context):
    print(event)
    data_source_id = os.environ['DATA_SOURCE_ID']
    knowledge_base_id = os.environ['KNOWLEDGE_BASE_ID']
    
    x_amz_request_id = event['Records'][0]['responseElements']['x-amz-request-id'].encode()
    client_token=hashlib.sha256(x_amz_request_id).hexdigest()

    response = bedrock_agent_client.start_ingestion_job(clientToken=client_token,
                                                        dataSourceId=data_source_id,
                                                        knowledgeBaseId=knowledge_base_id,
                                                        description='S3 files uploaded or created event'
                                                        )
    print(response)

    return {
        'statusCode': 200, 
        'body': json.dumps(response)
        }
