# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the Knowledge Base sync Lambda handler (lambda/kb_sync/index.py).

The handler is loaded from its file because ``lambda`` is a reserved word and cannot be imported as
a package. Its Bedrock client is faked with a botocore ``Stubber``: every request the handler makes
and every canned response is validated against the service model that ships with botocore, and
nothing is sent to AWS.
"""
import importlib.util
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

HANDLER_PATH = Path(__file__).resolve().parents[2] / "lambda" / "kb_sync" / "index.py"
KNOWLEDGE_BASE_ID = "KB00000001"
DATA_SOURCE_ID = "DS00000001"

# S3 writes eventTime in this format
EARLIER_CHANGE = "2026-10-23T08:36:02.100Z"
LATEST_CHANGE = "2026-10-23T08:36:12.345Z"
LATEST_CHANGE_AT = datetime(2026, 10, 23, 8, 36, 12, 345000, tzinfo=timezone.utc)
BEFORE_LATEST_CHANGE = LATEST_CHANGE_AT - timedelta(minutes=5)
AFTER_LATEST_CHANGE = LATEST_CHANGE_AT + timedelta(seconds=20)

# The message S3 sends to the queue when the bucket notification is configured
S3_TEST_EVENT = json.dumps({
    "Service": "Amazon S3",
    "Event": "s3:TestEvent",
    "Time": "2026-10-23T08:30:00.000Z",
    "Bucket": "amzn-s3-demo-bucket",
    "RequestId": "EXAMPLEREQUESTID",
    "HostId": "EXAMPLEHOSTID",
})

# Message bodies that are not S3 event notifications. Each carries the text "lecture notes", which stands
# for message content that must never reach the logs.
NOT_S3_NOTIFICATIONS = {
    "not JSON": "lecture notes, not JSON",
    "JSON nested too deeply to parse": "[" * 100_000 + '"lecture notes"' + "]" * 100_000,
    "not a JSON object": '["lecture notes"]',
    "Records is not a list": '{"Records": "lecture notes"}',
    "Records is an empty object": '{"Records": {}, "eventName": "lecture notes"}',
    "record is not an object": '{"Records": ["lecture notes"]}',
    "record without eventTime": '{"Records": [{"eventName": "lecture notes"}]}',
    "eventTime is not a string": '{"Records": [{"eventTime": 1792744572, "eventName": "lecture notes"}]}',
    "eventTime is not a time": '{"Records": [{"eventTime": "lecture notes"}]}',
    "eventTime without UTC offset":
        '{"Records": [{"eventTime": "2026-10-23T08:36:12.345", "eventName": "lecture notes"}]}',
    # A message is skipped as a whole: its first record, which is later than LATEST_CHANGE, does not count
    "one of two records without eventTime":
        '{"Records": [{"eventTime": "2026-10-23T09:00:00.000Z"}, {"eventName": "lecture notes"}]}',
}

LIST_REQUEST = {
    "knowledgeBaseId": KNOWLEDGE_BASE_ID,
    "dataSourceId": DATA_SOURCE_ID,
    "sortBy": {"attribute": "STARTED_AT", "order": "DESCENDING"},
    "maxResults": 10,
}
# The handler passes no clientToken: the coverage check keeps a redelivered batch from starting a second job
START_REQUEST = {"knowledgeBaseId": KNOWLEDGE_BASE_ID, "dataSourceId": DATA_SOURCE_ID, "description": ANY}


@pytest.fixture(scope="module")
def kb_sync():
    """Load the handler module once, as Lambda does, with placeholders instead of real AWS settings."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("KNOWLEDGE_BASE_ID", KNOWLEDGE_BASE_ID)
        patch.setenv("DATA_SOURCE_ID", DATA_SOURCE_ID)
        # boto3 needs a region to create the client; placeholder credentials on a throwaway default
        # session keep it from looking up real ones and from leaving anything behind for other tests
        patch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
            patch.setenv(name, "testing")
        # Keep the AWS configuration of this machine out of the client: a profile that the environment
        # names but that is not configured here, or a broken configuration file, would fail its creation
        for name in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
            patch.delenv(name, raising=False)
        for name in ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE"):
            patch.setenv(name, os.devnull)
        patch.setattr(boto3, "DEFAULT_SESSION", None)
        # Keep the import from writing __pycache__ into the folder that CDK packages as the function code
        patch.setattr(sys, "dont_write_bytecode", True)
        spec = importlib.util.spec_from_file_location("kb_sync_index", HANDLER_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        yield module


@pytest.fixture
def bedrock(kb_sync):
    """Stub the handler's Bedrock client; a call for which no response is queued fails the test."""
    with Stubber(kb_sync.bedrock_agent_client) as stubber:
        yield stubber
        stubber.assert_no_pending_responses()


def sqs_event(*bodies):
    """An SQS batch with one message per body."""
    return {"Records": [{"messageId": f"message-{number}", "eventSource": "aws:sqs", "body": body}
                        for number, body in enumerate(bodies, start=1)]}


def s3_notification(*event_times):
    """The body of an S3 event notification with one record per event time."""
    return json.dumps({"Records": [
        {"eventSource": "aws:s3", "eventName": "ObjectCreated:Put", "eventTime": event_time}
        for event_time in event_times
    ]})


def ingestion_job(job_id, status, started_at):
    """An ingestion job with every member the service model requires in a response."""
    return {
        "knowledgeBaseId": KNOWLEDGE_BASE_ID,
        "dataSourceId": DATA_SOURCE_ID,
        "ingestionJobId": job_id,
        "status": status,
        "startedAt": started_at,
        "updatedAt": started_at,
    }


def queue_job_list(bedrock, *jobs):
    """Expect one ListIngestionJobs request and answer it with the given jobs, most recent first."""
    bedrock.add_response("list_ingestion_jobs", {"ingestionJobSummaries": list(jobs)}, LIST_REQUEST)


def queue_job_start(bedrock, job_id):
    """Expect one StartIngestionJob request and answer it with a new job."""
    new_job = ingestion_job(job_id, "STARTING", AFTER_LATEST_CHANGE)
    bedrock.add_response("start_ingestion_job", {"ingestionJob": new_job}, START_REQUEST)


@pytest.mark.parametrize(
    "bodies",
    [[S3_TEST_EVENT], list(NOT_S3_NOTIFICATIONS.values()), []],
    ids=["s3 test event", "no S3 notification", "empty batch"],
)
def test_batch_without_s3_records_is_ignored(kb_sync, bedrock, bodies):
    event = sqs_event(*bodies)

    assert kb_sync.latest_change_time(event) is None
    # No response is queued, so any call to Bedrock would fail the test
    assert kb_sync.lambda_handler(event, None) == {"status": "ignored"}


def test_job_is_started_when_the_data_source_has_no_jobs(kb_sync, bedrock):
    queue_job_list(bedrock)
    queue_job_start(bedrock, "NEWJOB0001")

    result = kb_sync.lambda_handler(sqs_event(s3_notification(LATEST_CHANGE)), None)

    assert result == {"status": "started", "ingestionJobId": "NEWJOB0001"}


@pytest.mark.parametrize("status", ["STARTING", "IN_PROGRESS", "COMPLETE"])
def test_change_is_covered_by_a_job_that_started_after_it(kb_sync, bedrock, status):
    queue_job_list(
        bedrock,
        ingestion_job("NEWERJOB01", status, AFTER_LATEST_CHANGE),
        ingestion_job("OLDERJOB01", "COMPLETE", BEFORE_LATEST_CHANGE),
    )

    result = kb_sync.lambda_handler(sqs_event(s3_notification(LATEST_CHANGE)), None)

    assert result == {"status": "covered", "ingestionJobId": "NEWERJOB01"}


@pytest.mark.parametrize("status", ["FAILED", "STOPPING", "STOPPED"])
def test_job_is_started_when_the_newer_job_failed_or_was_stopped(kb_sync, bedrock, status):
    queue_job_list(bedrock, ingestion_job("NEWERJOB01", status, AFTER_LATEST_CHANGE))
    queue_job_start(bedrock, "NEWJOB0001")

    result = kb_sync.lambda_handler(sqs_event(s3_notification(LATEST_CHANGE)), None)

    assert result == {"status": "started", "ingestionJobId": "NEWJOB0001"}


def test_failed_job_does_not_hide_an_earlier_job_that_covers_the_change(kb_sync, bedrock):
    queue_job_list(
        bedrock,
        ingestion_job("FAILEDJOB1", "FAILED", AFTER_LATEST_CHANGE),
        ingestion_job("NEWERJOB01", "COMPLETE", AFTER_LATEST_CHANGE - timedelta(seconds=10)),
    )

    result = kb_sync.lambda_handler(sqs_event(s3_notification(LATEST_CHANGE)), None)

    assert result == {"status": "covered", "ingestionJobId": "NEWERJOB01"}


@pytest.mark.parametrize(
    "started_at",
    [BEFORE_LATEST_CHANGE, LATEST_CHANGE_AT],
    ids=["before the change", "at the instant of the change"],
)
def test_job_is_started_when_no_job_started_after_the_change(kb_sync, bedrock, started_at):
    queue_job_list(
        bedrock,
        ingestion_job("OLDERJOB02", "IN_PROGRESS", started_at),
        ingestion_job("OLDERJOB01", "COMPLETE", BEFORE_LATEST_CHANGE - timedelta(hours=1)),
    )
    queue_job_start(bedrock, "NEWJOB0001")

    result = kb_sync.lambda_handler(sqs_event(s3_notification(LATEST_CHANGE)), None)

    assert result == {"status": "started", "ingestionJobId": "NEWJOB0001"}


def test_latest_event_time_of_all_messages_and_records_is_used(kb_sync, bedrock):
    event = sqs_event(
        s3_notification(EARLIER_CHANGE, LATEST_CHANGE),
        S3_TEST_EVENT,
        s3_notification("2026-10-23T08:36:07.000Z"),
    )

    assert kb_sync.latest_change_time(event) == LATEST_CHANGE_AT

    # This job started after the two earlier changes but before the latest one, so it does not cover the batch
    queue_job_list(bedrock, ingestion_job("OLDERJOB01", "COMPLETE", LATEST_CHANGE_AT - timedelta(seconds=1)))
    queue_job_start(bedrock, "NEWJOB0001")

    assert kb_sync.lambda_handler(event, None) == {"status": "started", "ingestionJobId": "NEWJOB0001"}


def test_bulk_upload_split_over_two_batches_starts_a_single_job(kb_sync, bedrock):
    upload_times = [f"2026-10-23T08:36:{second:02d}.000Z" for second in range(17)]
    first_batch = sqs_event(*[s3_notification(upload_time) for upload_time in upload_times[:12]])
    second_batch = sqs_event(*[s3_notification(upload_time) for upload_time in upload_times[12:]])

    queue_job_list(bedrock)
    queue_job_start(bedrock, "NEWJOB0001")
    assert kb_sync.lambda_handler(first_batch, None) == {"status": "started", "ingestionJobId": "NEWJOB0001"}

    # The job started after every upload, so the rest of the uploads (or a redelivered batch) need no second job
    queue_job_list(bedrock, ingestion_job("NEWJOB0001", "IN_PROGRESS", AFTER_LATEST_CHANGE))
    assert kb_sync.lambda_handler(second_batch, None) == {"status": "covered", "ingestionJobId": "NEWJOB0001"}


@pytest.mark.parametrize("failing_call", ["list_ingestion_jobs", "start_ingestion_job"])
def test_bedrock_error_fails_the_batch(kb_sync, bedrock, failing_call):
    """The error must escape the handler: only a failed invocation makes SQS redeliver the batch."""
    if failing_call == "start_ingestion_job":
        queue_job_list(bedrock)
    bedrock.add_client_error(failing_call, service_error_code="ThrottlingException", http_status_code=429)

    with pytest.raises(ClientError) as raised:
        kb_sync.lambda_handler(sqs_event(s3_notification(LATEST_CHANGE)), None)

    assert raised.value.response["Error"]["Code"] == "ThrottlingException"


@pytest.mark.parametrize("body", list(NOT_S3_NOTIFICATIONS.values()), ids=list(NOT_S3_NOTIFICATIONS))
def test_message_that_is_not_an_s3_notification_is_skipped_with_a_warning(kb_sync, bedrock, caplog, body):
    """Such a message must not fail the batch: the S3 notification that shares the batch would be retried with
    it and finally moved to the dead-letter queue, without its change being synchronized."""
    queue_job_list(bedrock)
    queue_job_start(bedrock, "NEWJOB0001")
    event = sqs_event(body, s3_notification(LATEST_CHANGE))

    assert kb_sync.lambda_handler(event, None) == {"status": "started", "ingestionJobId": "NEWJOB0001"}

    warnings = [record.getMessage() for record in caplog.records
                if record.name == kb_sync.logger.name and record.levelno == logging.WARNING]
    assert len(warnings) == 1
    # The skipped message is identified by its id; message bodies are never logged
    assert "messageId=message-1" in warnings[0]
    assert "lecture notes" not in caplog.text
    # Nothing in the skipped message counts as a change
    assert kb_sync.latest_change_time(event) == LATEST_CHANGE_AT
