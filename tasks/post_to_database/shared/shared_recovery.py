"""
Name: shared_recovery.py
Description: Shared library that combines common functions and classes needed for recovery operations.
"""
from enum import Enum
import json
import boto3
from typing import Dict, Any, Optional, List
from datetime import datetime, timezone


class RequestMethod(Enum):
    """
    An enumeration.
    Provides potential actions for the database lambda to take when posting to the SQS queue.
    """
    NEW_JOB = "new_job"
    UPDATE_FILE = "update_file"


class OrcaStatus(Enum):
    """
    An enumeration.
    Defines the status value used in the ORCA Recovery database for use by the recovery functions.

    """
    PENDING = 1
    STAGED = 2
    FAILED = 3
    SUCCESS = 4


def create_status_for_job(
    job_id: str,
    granule_id: str,
    archive_destination: str,
    files: List[Dict[str, Any]],
    db_queue_url: str
):
    """
    Creates status information for a new job and its files, and posts to queue.

    Args:
        job_id: The unique identifier used for tracking requests.
        granule_id: The id of the granule being restored.
        archive_destination: The S3 bucket destination of where the data is archived.
        files: TODO
        db_queue_url: The SQS queue URL defined by AWS.

    """
    new_data = {"job_id": job_id, "granule_id": granule_id,
                "request_time": datetime.now(timezone.utc).isoformat(), "archive_destination": archive_destination,
                "files": files}

    post_entry_to_queue("orca_recoveryjob", new_data, RequestMethod.NEW_JOB, db_queue_url)


def update_status_for_file(
    job_id: str,
    granule_id: str,
    filename: str,
    status_id: OrcaStatus,
    error_message: Optional[str],
    db_queue_url: str,
):
    """
    Creates update information for a file's status entry, and posts to queue.
    Queue entry will be rejected by post_to_database if status for job_id + granule_id + filename does not exist.

    Args:
        job_id: The unique identifier used for tracking requests.
        granule_id: The id of the granule being restored.
        filename: The name of the file being copied.
        status_id: Defines the status id used in the ORCA Recovery database.
        error_message: message displayed on error.
        db_queue_url: The SQS queue URL defined by AWS.
    """
    last_update = datetime.now(timezone.utc).isoformat()
    new_data = {
        "job_id": job_id,
        "granule_id": granule_id,
        "filename": filename,
        "last_update": last_update,
        "status_id": status_id.value,
    }

    if status_id == OrcaStatus.SUCCESS or status_id == OrcaStatus.FAILED:
        new_data["completion_time"] = datetime.now(timezone.utc).isoformat()
        if status_id == OrcaStatus.FAILED:
            if len(error_message) == 0 or error_message is None:
                raise Exception("error message is required.")
            new_data["error_message"] = error_message

    post_entry_to_queue("orca_recoverfile", new_data, RequestMethod.UPDATE, db_queue_url)


def post_entry_to_queue(
    table_name: str,
    new_data: Dict[str, Any],
    request_method: RequestMethod,
    db_queue_url: str,
) -> None:
    """
    Posts messages to an SQS queue.

    Args:
        table_name: The name of the DB table.
        new_data: A dictionary representing the column/value pairs to write to the DB table.
        request_method: The method action for the database lambda to take when posting to the SQS queue.
        db_queue_url: The SQS queue URL defined by AWS.

    Raises:
        None
    """
    body = json.dumps(new_data)

    mysqs_resource = boto3.resource("sqs")
    mysqs = mysqs_resource.Queue(db_queue_url)

    mysqs.send_message(
        QueueUrl=db_queue_url,
        MessageDeduplicationId=table_name + request_method.value + body,
        MessageGroupId="request_files",
        MessageAttributes={
            "RequestMethod": {
                "DataType": "String",
                "StringValue": request_method.value,
            },
            "TableName": {"DataType": "String", "StringValue": table_name},
        },
        MessageBody=body,
    )
