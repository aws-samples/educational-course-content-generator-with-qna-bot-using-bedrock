## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import boto3
from botocore.config import Config
import json
from io import BytesIO
import PyPDF2
from urllib.parse import urlparse, unquote_plus
from langchain_core.prompts import PromptTemplate
from pydantic_utils import convert_pydantic_to_bedrock_converse_function

#increase the standard time out limits in boto3, because Bedrock may take a while to respond to large requests.
my_config = Config(
    connect_timeout=60*5,
    read_timeout=60*5,
)

s3_client=boto3.client("s3")
bedrock_runtime_client = boto3.client(service_name='bedrock-runtime',config=my_config)
bedrock_client = boto3.client(service_name='bedrock',config=my_config) 

## write a function to write json file into s3 bucket
def write_json_to_s3(value_dict_, s3_bucket, result_json_folder):
    for key, value in value_dict_.items():
        file_name = f"{key}.json"
        json_data=value
        s3_key = f"{result_json_folder}/{file_name}"
        s3_client.put_object(Body=json.dumps(json_data), Bucket=s3_bucket, Key=s3_key)
    return True

def get_s3_bucket_and_key(s3_input_uri):
    parsed_uri = urlparse(s3_input_uri)
    
    if parsed_uri.scheme == 's3':
        # s3://bucket-name/key format
        bucket = parsed_uri.netloc
        key = parsed_uri.path.lstrip('/')
    elif parsed_uri.scheme == 'https':
        # https://bucket-name.s3.amazonaws.com/key format
        bucket = parsed_uri.netloc.split('.')[0]
        key = parsed_uri.path.lstrip('/')
    else:
        raise ValueError("Unsupported URL format")
    
    # remove un necesssary special quotes
    key = unquote_plus(key)

    return bucket, key


def extract_text_from_pdf(bucket, key):
    response = s3_client.get_object(Bucket=bucket, Key=key)
    pdf_content = response['Body'].read()

    pdf_file = BytesIO(pdf_content)
    pdf_reader = PyPDF2.PdfReader(pdf_file)
    text = ""
    for page in pdf_reader.pages:
        text += page.extract_text() + "\n"  # Add newline after each page's text
    return text


def save_json_to_s3(bucket, key, llm_json_response):
    # Convert the dictionary to a JSON string
    json_content = json.dumps(llm_json_response)
    # Save the JSON string to S3 (S3 expects bytes, so we encode the string to bytes)
    s3_client.put_object(Bucket=bucket, Key=key, Body=json_content.encode('utf-8'))


def invoke_bedrock_converse_api(model_id, course_title, week_number, main_learning_outcome, 
                                   sub_learning_outcome_list, additional_context, 
                                   user_prompt, pydantic_classes, is_streaming):
    # model_id = "anthropic.claude-3-haiku-20240307-v1:0"
    # model_id = "anthropic.claude-3-5-sonnet-20240620-v1:0"

    system_prompt =f"""You are an AI assistant specialized in educational content creation.
Your task is to generate course materials based on given learning outcomes.
Produce concise, accurate, and engaging content suitable for college-level courses.
You may refer to additional context provided within <additional_context> tags if present.
Format your response in valid JSON for easy parsing and integration.
Respond only with the requested content, without any preamble or explanation."""

    user_msg_prompt = PromptTemplate.from_template(user_prompt)
    
    user_msg = user_msg_prompt.format(course_title=course_title, 
                                      week_number=week_number,
                                      main_learning_outcome=main_learning_outcome,
                                      sub_learning_outcome_list=sub_learning_outcome_list,
                                      additional_context=additional_context)

    messages = [{"role": "user", 
                 "content": [{
                     "text": user_msg}
                     ]}
                ]
    
    # inference_config = {
    #         "temperature": 0.7,
    #         "topP": 0.7
    #     }

    tools = []
    for class_ in pydantic_classes:
        tools.append(convert_pydantic_to_bedrock_converse_function(class_))
    tool_config = { "tools": tools }
    
    if is_streaming=="yes":
        response = bedrock_runtime_client.converse_stream(
                modelId=model_id,
                messages=messages,
                system=[{ "text": system_prompt}],
                # inferenceConfig=inference_config,
                toolConfig=tool_config,
            )
    else:
         response = bedrock_runtime_client.converse(
            system=[{ "text": system_prompt}],
            modelId=model_id,
            messages=messages,
            # inferenceConfig=inference_config,
            toolConfig=tool_config,
        )
    return response


def parse_llm_response(response):
    ai_message = response["output"]["message"]
    output_text = ai_message["content"][0]["text"]
    try:
        output_json = json.loads(output_text)
    except:
        output_json = {"error":"LLM Output is not json parsable", 
                       "LLM_Output":output_text}
    return output_json

def parse_bedrock_tool_response(response):
    value_dict_ = {}
    tool_name = ""

    stop_reason = response['stopReason']
    tool_requests = response['output']['message']['content']

    if stop_reason == 'tool_use':
        for tool_request in tool_requests:
            if 'toolUse' in tool_request:
                print("Claude used a tool")
                tool_name = tool_request['toolUse']["name"]
                tool_input = tool_request['toolUse']["input"]
                value_dict_[tool_name] = tool_input
        if len(tool_requests)==1:
            print("Claude didn't use any tools")
    return value_dict_

def send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, response):
        apigatewaymanagementapi_client.post_to_connection(ConnectionId=connection_id, 
                                                          Data=json.dumps(response).encode('utf-8'))

def process_stream_obj_old(response, apigatewaymanagementapi_client, connection_id):
        final_response=""
        for event in response['stream']:
            if 'messageStart' in event:
                    print(f"\nRole: {event['messageStart']['role']}")

            elif 'contentBlockDelta' in event:
                new_text = event['contentBlockDelta']['delta']['text']
                final_response+=new_text
                send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, new_text)

            elif 'messageStop' in event:
                print(f"\nStop reason: {event['messageStop']['stopReason']}")
                send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, event['messageStop'])
            
            elif 'metadata' in event:
                    metadata = event['metadata']
                    if 'usage' in metadata:
                        print("\nToken usage")
                        print(f"Input tokens: {metadata['usage']['inputTokens']}")
                        print(
                            f":Output tokens: {metadata['usage']['outputTokens']}")
                        print(f":Total tokens: {metadata['usage']['totalTokens']}")
                    if 'metrics' in event['metadata']:
                        print(
                            f"Latency: {metadata['metrics']['latencyMs']} milliseconds")
                    if 'guardrails_usage' in event['metadata']:
                        print(event['metadata']['guardrails_usage'])
            else:
                print("no key found")

        return final_response

def process_stream_obj(response, apigatewaymanagementapi_client, connection_id):
        stop_reason = ""
        message = {}
        content = []
        message['content'] = content
        text = ''
        tool_use = {}

        #stream the response into a message.
        for chunk in response['stream']:
            if 'messageStart' in chunk:
                message['role'] = chunk['messageStart']['role']
            elif 'contentBlockStart' in chunk:
                tool = chunk['contentBlockStart']['start']['toolUse']
                tool_use['toolUseId'] = tool['toolUseId']
                tool_use['name'] = tool['name']
            elif 'contentBlockDelta' in chunk:
                delta = chunk['contentBlockDelta']['delta']
                if 'toolUse' in delta:
                    if 'input' not in tool_use:
                        tool_use['input'] = ''
                    tool_use['input'] += delta['toolUse']['input']
                    print(delta['toolUse']['input'])
                    send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, delta['toolUse']['input'])
                elif 'text' in delta:
                    text += delta['text']
                    print(delta['text'], end='')
                    # send_message_to_ws_client(apigatewaymanagementapi_client, connection_id, delta['text'])
            elif 'contentBlockStop' in chunk:
                if 'input' in tool_use:
                    tool_use['input'] = json.loads(tool_use['input'])
                    content.append({'toolUse': tool_use})
                    tool_use = {}
                else:
                    content.append({'text': text})
                    text = ''

            elif 'messageStop' in chunk:
                stop_reason = chunk['messageStop']['stopReason']

        return stop_reason, message