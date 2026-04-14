## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import boto3


bedrock_agent_runtime_client = boto3.client("bedrock-agent-runtime")

def get_filter_condition(course_name, course_id, week_number):
    and_all_condition = []
    course_name_condition = { 'equals': {'key': 'course_name', 'value': course_name }}
    course_id_condition = { 'equals': { 'key': 'course_id', 'value': course_id }}
    week_number_condition = { 'lessThanOrEquals': {'key': 'week','value': int(week_number) }}
    
    # Use this code when you want to include Course name for filter
    if course_name_condition != "" and course_name_condition is not None:
        and_all_condition.append(course_name_condition)

    if course_id != "" and course_id is not None:
        and_all_condition.append(course_id_condition)

    if week_number_condition != "" and week_number_condition is not None:
        and_all_condition.append(week_number_condition)
    
    return and_all_condition


def retrive_from_kb(user_question, kb_id, model_arn, guardrail_id, guardrail_version, prompt_template, and_all_condition, num_of_results):
    response = bedrock_agent_runtime_client.retrieve_and_generate(
        input={'text': user_question},
        retrieveAndGenerateConfiguration={
            'type': 'KNOWLEDGE_BASE',
            'knowledgeBaseConfiguration': {
                    'knowledgeBaseId': kb_id,
                    'modelArn': model_arn,
                    'generationConfiguration': {
                        "guardrailConfiguration": { 
                            "guardrailId": guardrail_id,
                            "guardrailVersion": guardrail_version
                        },
                        'inferenceConfig': { 
                            'textInferenceConfig': { 
                                'temperature': 0,        # not to hallucinate
                            }
                        },
                        'promptTemplate': {
                            'textPromptTemplate': prompt_template
                        }
                    },
                    'retrievalConfiguration': {
                        'vectorSearchConfiguration': {
                            'filter': {
                                'andAll': and_all_condition,
                            },
                            'numberOfResults': num_of_results,
                            'overrideSearchType': 'HYBRID'
                        }
                    }
                },
        },
    )
    return response 