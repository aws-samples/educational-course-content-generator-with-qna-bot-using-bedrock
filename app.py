#!/usr/bin/env python3

## Copyright 2024 Amazon.com, Inc. or its affiliates. All Rights Reserved.
## SPDX-License-Identifier: LicenseRef-.amazon.com.-AmznSL-1.0
## Licensed under the Amazon Software License  https://aws.amazon.com/asl/
import json
import cdk_nag
from aws_cdk import Aspects
import aws_cdk as cdk
import os

from educational_course_content_generator_with_qna_bot_using_bedrock.course_stack import CourseStack
from educational_course_content_generator_with_qna_bot_using_bedrock.cloudfront_waf_stack import CloudFrontWAFStack
from educational_course_content_generator_with_qna_bot_using_bedrock.qna_stack import QnAStack

with open('project_config.json', 'r') as file:
    variables = json.load(file)

app = cdk.App()

# This stack will create WAF fro cloudfront
waf_stack = CloudFrontWAFStack(app, variables["CloudFrontWAFStackName"],)

# This Stack will create resources for the Course Outline and course content module
course_stack = CourseStack(app,variables["CourseStackName"],)

qna_stack = QnAStack(app, variables["QnAStackName"],)

# qna_stack.add_dependency(course_stack)

Aspects.of(app).add(cdk_nag.AwsSolutionsChecks(reports=True, verbose=True))
app.synth()
