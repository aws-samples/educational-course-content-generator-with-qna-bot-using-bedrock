# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from aws_cdk import (
    Annotations,
    CfnResource,
    CustomResourceProviderBase,
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_logs as logs,
)

# Retention period of the log groups owned by the stacks (Lambda functions and VPC flow logs).
LOG_RETENTION = logs.RetentionDays.ONE_MONTH

# The stacks create the log groups, so no function role needs this permission.
DENY_LOG_GROUP_CREATION = {"Effect": "Deny", "Action": "logs:CreateLogGroup", "Resource": "*"}


def own_lambda_log_groups(stack: Stack, retention=LOG_RETENTION) -> int:
    """Give every Lambda function of the stack a log group that the stack owns.

    Lambda otherwise creates /aws/lambda/<function> on the first invocation, outside CloudFormation and
    without a retention period, and that log group survives `cdk destroy`. A log group that belongs to the
    stack has a retention period and is deleted together with the stack.

    The function resources are patched instead of passing a log group to each Function construct because
    this also has to cover the functions CDK creates for custom resources (bucket deployment, bucket
    notifications, S3 auto-delete objects, VPC default security group, custom resource provider framework).
    Call it as the last statement of the stack constructor, once all functions are defined.

    The role of each function is set up so that Lambda cannot create the log group again while the stack
    is deleted (see _keep_log_group_deleted). A function whose role cannot be set up that way gets a
    warning at synthesis.

    A function whose logging configuration already names a log group is left unchanged. Returns the number
    of log groups created.
    """
    count = 0
    for construct in stack.node.find_all():
        if not (isinstance(construct, CfnResource) and construct.cfn_resource_type == "AWS::Lambda::Function"):
            continue
        # Only CfnFunction exposes its logging config; the custom resource handlers are plain CfnResources
        logging_config = getattr(construct, "logging_config", None)
        if getattr(logging_config, "log_group", None) is not None:
            continue
        log_group = logs.LogGroup(construct.node.scope, "StackOwnedLogGroup",
                                  retention=retention, removal_policy=RemovalPolicy.DESTROY)
        construct.add_property_override("LoggingConfig.LogGroup", log_group.log_group_name)
        if not _keep_log_group_deleted(construct, log_group):
            Annotations.of(construct).add_warning(
                "The execution role of this function is not created next to it or already has inline policies. "
                "It keeps logs:CreateLogGroup and is not deleted before the log group of the function, so "
                "Lambda may create that log group again when the stack is deleted."
            )
        count += 1
    return count


def _keep_log_group_deleted(function_resource: CfnResource, log_group: logs.LogGroup) -> bool:
    """Keep Lambda from creating the log group of a function again while the stack is deleted.

    When a stack is deleted, the handlers of custom resources run one last time. Lambda delivers the log
    lines of that invocation a moment after CloudFormation deleted the function. If the log group is gone
    by then while the execution role still exists, Lambda creates the log group again with the
    logs:CreateLogGroup permission of AWSLambdaBasicExecutionRole, this time outside the stack and without
    a retention period. Two measures prevent that:

    1. The role depends on the log group. CloudFormation therefore deletes the function, then its role and
       only then the log group: late log lines find the log group still in place, and when it is deleted
       no role is left that could create it again.
    2. The role is denied logs:CreateLogGroup, which it never needs because the stack creates the log
       group. The statement is inline in the role, not in a separate policy resource. It also covers a log
       group that is deleted by hand.

    Returns False, without changing anything, if the role is not defined next to the function (for example
    a role passed in) or already has inline policies.
    """
    owner = function_resource.node.scope
    if isinstance(owner, CustomResourceProviderBase):
        # Custom resource providers of the CDK core library: the role is a plain CfnResource and the
        # statements added to the provider are rendered inline in it
        role_resource = owner.node.try_find_child("Role")
        if not isinstance(role_resource, CfnResource):
            return False
        owner.add_to_role_policy(DENY_LOG_GROUP_CREATION)
    else:
        role = owner.node.try_find_child("ServiceRole") or owner.node.try_find_child("Role")
        role_resource = getattr(getattr(role, "node", None), "default_child", None)
        if not isinstance(role_resource, iam.CfnRole) or role_resource.policies is not None:
            return False
        role_resource.add_property_override("Policies", [{
            "PolicyName": "DenyLogGroupCreation",
            "PolicyDocument": {"Version": "2012-10-17", "Statement": [DENY_LOG_GROUP_CREATION]},
        }])
    role_resource.node.add_dependency(log_group)
    return True
