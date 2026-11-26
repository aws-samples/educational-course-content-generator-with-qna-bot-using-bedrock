# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Start a Knowledge Base ingestion job when course content changes in the S3 bucket.

Amazon S3 sends a notification to an SQS queue for every ``.metadata.json`` object that is created
or removed, and this function receives those notifications in batches. An ingestion job scans the
whole data source, so one job picks up every change made before it started. The function therefore
starts a job only when no job has started since the latest change in the batch, and a bulk upload
typically results in a single ingestion job instead of one per file.
"""
import json
import logging
import os
from datetime import datetime

import boto3
from botocore.config import Config

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# The stack passes the solution user-agent suffix in USER_AGENT_STRING (see "Solution ID and usage
# reporting" in the README). Without the variable the clients are created without a suffix.
SDK_CONFIG = Config(user_agent_extra=os.environ.get("USER_AGENT_STRING", ""))
bedrock_agent_client = boto3.client('bedrock-agent', config=SDK_CONFIG)

# Jobs in these states scan the whole data source; a failed or stopped job may have missed changes
COVERING_STATUSES = ('STARTING', 'IN_PROGRESS', 'COMPLETE')


def _change_times(message):
    """Return the event times of the S3 event records that one SQS message carries.

    Raises KeyError, TypeError, ValueError or RecursionError if the message is not an S3 event
    notification.
    """
    body = json.loads(message['body'])
    if not isinstance(body, dict):
        raise TypeError("the message body is not a JSON object")
    # The s3:TestEvent message that S3 sends when the notification is configured has no Records
    records = body.get('Records', [])
    if not isinstance(records, list):
        raise TypeError("Records is not a list")
    change_times = [datetime.fromisoformat(record['eventTime']) for record in records]
    # S3 reports event times in UTC. A time without a UTC offset cannot be compared with the start
    # time of an ingestion job.
    if any(change_time.tzinfo is None for change_time in change_times):
        raise ValueError("an event time has no UTC offset")
    return change_times


def latest_change_time(event):
    """Return the latest S3 event time in the SQS batch, or None if the batch holds no S3 records.

    S3 reports event times in UTC (for example 2026-10-23T08:36:12.345Z), so the result is a
    timezone-aware datetime that can be compared with the start time of an ingestion job.

    A message that is not an S3 event notification is skipped with a warning. An exception would
    fail the whole batch, and the S3 notifications that share the batch would be retried and
    finally moved to the dead-letter queue together with that message.
    """
    change_times = []
    for message in event.get('Records', []):
        try:
            change_times.extend(_change_times(message))
        except (KeyError, TypeError, ValueError, RecursionError):
            # Only the message id is logged: the exception text can quote the message body
            logger.warning("Skipping SQS message that is not an S3 event notification: messageId=%s",
                           message.get('messageId'))
    return max(change_times, default=None)


def find_covering_job(changed_at):
    """Return a recent ingestion job that started after ``changed_at``, or None.

    Such a job scans the bucket after the change, so the change is already covered. Jobs that
    failed or were stopped do not count.
    """
    response = bedrock_agent_client.list_ingestion_jobs(
        knowledgeBaseId=os.environ['KNOWLEDGE_BASE_ID'],
        dataSourceId=os.environ['DATA_SOURCE_ID'],
        sortBy={'attribute': 'STARTED_AT', 'order': 'DESCENDING'},
        maxResults=10,
    )
    for job in response['ingestionJobSummaries']:
        if job['status'] in COVERING_STATUSES and job['startedAt'] > changed_at:
            return job
    return None


def lambda_handler(event, context):
    request_id = getattr(context, 'aws_request_id', '')
    message_count = len(event.get('Records', []))

    changed_at = latest_change_time(event)
    if changed_at is None:
        logger.info("No S3 change in the batch, nothing to sync: messages=%d requestId=%s",
                    message_count, request_id)
        return {'status': 'ignored'}

    covering_job = find_covering_job(changed_at)
    if covering_job:
        logger.info("Change already covered by an ingestion job: ingestionJobId=%s status=%s startedAt=%s "
                    "latestChange=%s messages=%d requestId=%s",
                    covering_job['ingestionJobId'], covering_job['status'], covering_job['startedAt'].isoformat(),
                    changed_at.isoformat(), message_count, request_id)
        return {'status': 'covered', 'ingestionJobId': covering_job['ingestionJobId']}

    # Bedrock errors are deliberately not caught: a failed invocation leaves the batch in the queue,
    # which redelivers it after the visibility timeout and moves it to the dead-letter queue after
    # repeated failures. The coverage check above keeps a redelivered batch from starting a second
    # job, so no client token is passed.
    response = bedrock_agent_client.start_ingestion_job(
        knowledgeBaseId=os.environ['KNOWLEDGE_BASE_ID'],
        dataSourceId=os.environ['DATA_SOURCE_ID'],
        description='S3 files created or removed',
    )
    ingestion_job = response['ingestionJob']
    logger.info("Ingestion job started: ingestionJobId=%s status=%s latestChange=%s messages=%d requestId=%s",
                ingestion_job['ingestionJobId'], ingestion_job['status'], changed_at.isoformat(),
                message_count, request_id)
    return {'status': 'started', 'ingestionJobId': ingestion_job['ingestionJobId']}
