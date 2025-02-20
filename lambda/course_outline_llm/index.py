## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import json
from helper import * 
from CourseOutlinePydantic import CourseOutline
import os

def lambda_handler(event, context):
    print(event)
    event = json.loads(event['Records'][0]['body'])
    try:
        connection_id = event['requestContext']['connectionId']
        body = json.loads(event["body"])
        s3_input_uri_list = body["s3_input_uri_list"]
        user_prompt = body["user_prompt"]
        course_title = body["course_title"]
        course_duration = body["course_duration"]
        is_streaming = body["is_streaming"]
        model_id = os.getenv("MODEL_ID", "")
        websocket_endpoint_url = os.getenv("WEBSOCKET_ENDPOINT_URL","")
        output_bucket = os.getenv("OUTPUT_BUCKET", "")

    except:
        print("dev mode activated")
        websocket_endpoint_url = ""

        s3_input_uri_list = []
        course_title="Fundamentals of Machine learning"
        course_duration = 2
        is_streaming = "no"
        output_bucket = ""
        user_prompt='''I need help developing a {course_duration}-week course outline for a {course_title} course. Please use the following syllabus to:

1. If provided, refer to the syllabus text from <syllabus> tags to extract the course learning outcomes.
2. Design each week to focus on 3 main learning outcomes.
3. For each main learning outcome, provide 3 supporting sub-learning outcomes.

<syllabus>

{syllabus_text}

</syllabus>

Ensure that each week has 3 main learning outcomes and each of those has 3 supporting sub-learning outcomes.'''
    
    ## send message to api that message received
    apigatewaymanagementapi_client = boto3.client('apigatewaymanagementapi', endpoint_url=websocket_endpoint_url)
    # send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, response={'message':'Debugging... inside another lambda', "connection_id":connection_id})

    syllabus_text = ""
    for s3_input_uri in s3_input_uri_list:
        bucket, key = get_s3_bucket_and_key(s3_input_uri)
        if key.endswith('.pdf'):
            pdf_text = extract_text_from_pdf(bucket, key)
            syllabus_text = syllabus_text + pdf_text


    # Initialize the Pydantic model
    pydantic_classes = [CourseOutline]

    course_outline = {}
    if is_streaming == "yes":
        converse_response = invoke_bedrock_converse_api(model_id, course_title, course_duration, syllabus_text, user_prompt, pydantic_classes, is_streaming=is_streaming)
        stop_reason, message = process_stream_obj(converse_response, apigatewaymanagementapi_client, connection_id)
        if stop_reason == "tool_use":
            for content in message['content']:
                if 'toolUse' in content:
                    tool = content['toolUse']
                    if tool['name'] == "CourseOutline":
                        course_outline = tool['input']
                        
    else:
        MAX_RETRIES = 2
        count = 0
        while len(course_outline) == 0 and count < MAX_RETRIES:
            converse_response = invoke_bedrock_converse_api(model_id, course_title, course_duration, syllabus_text, user_prompt, pydantic_classes, is_streaming=is_streaming)
            course_outline = parse_bedrock_tool_response(converse_response)
            count += 1
            
        if len(course_outline) != 0:
            send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, response=course_outline)
        else:
            send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, response=f"Unable to generate course outline after {MAX_RETRIES} attempts.")
    
    print(course_outline)
    

    # Save the course content to S3
    output_key = f"course_outline/{course_title}/course_outline.json"
    save_json_to_s3(output_bucket, output_key, course_outline)
        
    return {'statusCode': 200,
            'body': json.dumps({
                        'course_outline': course_outline
                    })
        }


if __name__ == "__main__":
    event = None
    lambda_handler(event, None)