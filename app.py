#!/usr/bin/env python3

# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import json
import cdk_nag
from aws_cdk import Aspects
import aws_cdk as cdk
import os

from educational_course_content_generator_with_qna_bot_using_bedrock.course_stack import CourseStack
from educational_course_content_generator_with_qna_bot_using_bedrock.cloudfront_waf_stack import CloudFrontWAFStack
from educational_course_content_generator_with_qna_bot_using_bedrock.qna_stack import QnAStack
from educational_course_content_generator_with_qna_bot_using_bedrock.solution import stack_description

with open('project_config.json', 'r') as file:
    variables = json.load(file)

# The stacks pass the solution user-agent token of project_config.json to the Lambda functions and to the
# agent. A USER_AGENT_STRING variable in the shell that runs the deployment would take its place (see
# user_agent_string in solution.py), so it is ignored here.
os.environ.pop("USER_AGENT_STRING", None)

app = cdk.App()

# The stack descriptions carry the solution id (see solution.py). One installation deploys all three
# stacks and must be counted once, so only QnAStack, which depends on the other two, carries the bare
# id; the two supporting stacks carry the id with a suffix.

# This stack will create the WAF web ACL for CloudFront
waf_stack = CloudFrontWAFStack(app, variables["CloudFrontWAFStackName"],
                               description=stack_description("AWS WAF web ACL for CloudFront", id_suffix="waf"))

# This Stack will create resources for the Course Outline and course content module
course_stack = CourseStack(app,variables["CourseStackName"],
                           description=stack_description("Course outline and content generation",
                                                         id_suffix="course"))

qna_stack = QnAStack(app, variables["QnAStackName"], vpc=course_stack.vpc,
                     description=stack_description("QnA bot"))

# CourseStack imports the WAF ACL ARN (Fn.import_value) and QnAStack imports CourseStack exports:
# make both dependencies explicit so deploy and destroy run in the right order.
course_stack.add_dependency(waf_stack)
qna_stack.add_dependency(course_stack)

Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(reports=True, verbose=True))
app.synth()
