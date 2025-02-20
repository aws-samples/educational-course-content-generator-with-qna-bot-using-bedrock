## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import json
import boto3
import os
from helper import *


def lambda_handler(event, context):
    print(event)  
    try:
        body = json.loads(event["body"])
        user_question = body.get("user_question", None)
        course_name = body.get("course_name", None)
        learning_objective = body.get("learning_objective", None)
        course_id = body.get("course_id", None)
        week_number = body.get("week_number", None)
        session_id = body.get("session_id", None)
        
        kb_id = os.getenv("KB_ID", "")
        model_id = os.getenv("QnA_MODEL_ID", "")
        guardrail_id = os.getenv("GUARDRAIL_ID", "")
        guardrail_version = os.getenv("GUARDRAIL_VERSION", "")
       
    except:
        print("dev mode activated")
        
        user_question = "what is Machine Learning?"
        course_name = "Fundamentals of Machine Learning"
        course_id = "Dummy-c002"
        week_number = 2
        session_id = ""
        
        kb_id = ""
        model_id = ""
        guardrail_id = ""
        guardrail_version = ""

    num_of_results = 3
    region = boto3.Session().region_name
    model_arn = f'arn:aws:bedrock:{region}::foundation-model/{model_id}'
    
    prompt_template = '''You are an academic question answering assistant. Use only the provided search results to answer the student's question. If the results don't contain relevant information, state that you cannot find a definitive answer. Verify any claims made by the student against the search results before confirming them. Do not provide information beyond what is found in the search results.

Search results:
$search_results$
'''

    and_all_condition = get_filter_condition(course_name, course_id, week_number)

    response = retrive_from_kb(user_question, kb_id, model_arn, guardrail_id, guardrail_version, prompt_template, and_all_condition, num_of_results)

    output_text = response['output']['text']
    print(output_text)

    return {'statusCode': 200,
            'body': json.dumps({
                        'bot_response': output_text,
                        'response': response
                    })
        }

if __name__ == "__main__":
    event = None
    lambda_handler(event, None)