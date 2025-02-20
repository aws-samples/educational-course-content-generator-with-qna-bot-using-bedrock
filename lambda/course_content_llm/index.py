## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import json
from helper import * 
from CourseContentPydantic import CourseContent
import os

def lambda_handler(event, context):
    print(event)
    event = json.loads(event['Records'][0]['body'])
    try:
        connection_id = event['requestContext']['connectionId']
        body = json.loads(event["body"])
        s3_input_uri_list = body["s3_input_uri_list"]
        user_prompt = body["user_prompt"]
        week_number = body["week_number"]
        course_title = body["course_title"]
        main_learning_outcome = body["main_learning_outcome"]
        sub_learning_outcome_list = body["sub_learning_outcome_list"]
        is_streaming = body["is_streaming"]
        model_id = os.getenv("MODEL_ID", "")
        websocket_endpoint_url = os.environ["WEBSOCKET_ENDPOINT_URL"]
        output_bucket = os.environ["OUTPUT_BUCKET"]

    except:
        print("dev mode activated")
        websocket_endpoint_url = ""
        connection_id=""

        s3_input_uri_list = []
        week_number = 1
        course_title = ""
        main_learning_outcome = ""
        sub_learning_outcome_list = []
        is_streaming = "no"
        output_bucket = ""
        user_prompt='''For the course {course_title}, 
generate Week {week_number} content for the main learning outcome:
{main_learning_outcome}

Include the following sub-learning outcomes:
{sub_learning_outcome_list}

For each sub-learning outcome, provide:
- 3 video scripts, each 3 minutes long
- 1 set of reading materials, atleast one page long
- 1 multiple-choice question per video with correct answer

If provided, refer to the information within the <additional_context> tags for any supplementary details or guidelines.

<additional_context>
{additional_context}
</additional_context>

Format the response in JSON, with clear structure for each component. Ensure the content is academically sound and engaging for students.
Generate the content without any introductory text or explanations.'''

    ## send message to api that message received
    apigatewaymanagementapi_client = boto3.client('apigatewaymanagementapi', endpoint_url=websocket_endpoint_url)
    # send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, response={'message':'Debugging... inside another lambda', "connection_id":connection_id})

    additional_context = ""
    for s3_input_uri in s3_input_uri_list:
        bucket, key = get_s3_bucket_and_key(s3_input_uri)
        if key.endswith('.pdf'):
            pdf_text = extract_text_from_pdf(bucket, key)
            additional_context = additional_context + pdf_text
    
    # Initialize the Pydantic model
    pydantic_classes = [CourseContent]

    course_content={}    
    if is_streaming == "yes":
        converse_response = invoke_bedrock_converse_api(model_id, course_title, week_number, main_learning_outcome, 
                                                    sub_learning_outcome_list, additional_context, user_prompt, 
                                                    pydantic_classes, is_streaming=is_streaming)
        stop_reason, message = process_stream_obj(converse_response, apigatewaymanagementapi_client, connection_id)
        if stop_reason == "tool_use":
            for content in message['content']:
                if 'toolUse' in content:
                    tool = content['toolUse']
                    if tool['name'] == "CourseContent":
                        course_content = tool['input']
    else:
        MAX_RETRIES = 2
        count = 0
        while len(course_content) == 0 and count < MAX_RETRIES:
            converse_response = invoke_bedrock_converse_api(model_id, course_title, week_number, main_learning_outcome, 
                                                    sub_learning_outcome_list, additional_context, user_prompt, 
                                                    pydantic_classes, is_streaming=is_streaming)
            course_content = parse_bedrock_tool_response(converse_response)
            count += 1

        if len(course_content) != 0:
            send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, response=course_content)
        else:
            send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, response=f"Unable to generate course content after {MAX_RETRIES} attempts.")

    print(course_content)
    
    # Save the course content to S3
    output_key = f"course_content/{course_title}/{week_number}/{main_learning_outcome}/course_content.json"
    save_json_to_s3(output_bucket, output_key, course_content)
    
        
    return {'statusCode': 200,
            'body': json.dumps({
                        'course_content': json.dumps(course_content)
                    })
        }


if __name__ == "__main__":
    event = None
    lambda_handler(event, None)