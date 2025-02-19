import aws_cdk as core
import aws_cdk.assertions as assertions

from educational_course_content_generator_with_qna_bot_using_bedrock.educational_course_content_generator_with_qna_bot_using_bedrock_stack import EducationalCourseContentGeneratorWithQnaBotUsingBedrockStack

# example tests. To run these tests, uncomment this file along with the example
# resource in educational_course_content_generator_with_qna_bot_using_bedrock/educational_course_content_generator_with_qna_bot_using_bedrock_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = EducationalCourseContentGeneratorWithQnaBotUsingBedrockStack(app, "educational-course-content-generator-with-qna-bot-using-bedrock")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
