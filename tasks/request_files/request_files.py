"""
Name: request_files.py
Description:  Lambda function that makes a restore request from glacier for each input file.
"""
import json
import os
import time
import uuid
from enum import Enum
from typing import Dict, Any, Union, List, Optional
from datetime import datetime, timezone

# noinspection PyPackageRequirements
import boto3
# noinspection PyPackageRequirements
from botocore.client import BaseClient
# noinspection PyPackageRequirements
from botocore.exceptions import ClientError
from cumulus_logger import CumulusLogger
from run_cumulus_task import run_cumulus_task


class RequestMethod(Enum):
    POST = 'post'
    PUT = 'put'


DEFAULT_RESTORE_EXPIRE_DAYS = 5
DEFAULT_MAX_REQUEST_RETRIES = 2
DEFAULT_RESTORE_RETRY_SLEEP_SECS = 0
DEFAULT_RESTORE_RETRIEVAL_TYPE = 'Standard'

OS_ENVIRON_RESTORE_EXPIRE_DAYS_KEY = 'RESTORE_EXPIRE_DAYS'
OS_ENVIRON_RESTORE_REQUEST_RETRIES_KEY = 'RESTORE_REQUEST_RETRIES'
OS_ENVIRON_RESTORE_RETRY_SLEEP_SECS_KEY = 'RESTORE_RETRY_SLEEP_SECS'
OS_ENVIRON_RESTORE_RETRIEVAL_TYPE_KEY = 'RESTORE_RETRIEVAL_TYPE'
OS_ENVIRON_DB_QUEUE_URL_KEY = 'DB_QUEUE_URL'

EVENT_CONFIG_KEY = 'config'
EVENT_INPUT_KEY = 'input'
INPUT_JOB_ID_KEY = 'job_id'

INPUT_GRANULES_KEY = 'granules'

CONFIG_GLACIER_BUCKET_KEY = 'glacier-bucket'  # todo: Rename. This ONE property uses '-' instead of '_'

GRANULE_GRANULE_ID_KEY = 'granuleId'
GRANULE_KEYS_KEY = 'keys'
GRANULE_RECOVER_FILES_KEY = 'recover_files'

# noinspection SpellCheckingInspection
FILE_DEST_BUCKET_KEY = 'dest_bucket'
FILE_KEY_KEY = 'key'
FILE_SUCCESS_KEY = 'success'
FILE_ERROR_MESSAGE_KEY = 'err_msg'

ORCA_STATUS_PENDING = 1
# ORCA_STATUS_STAGED = 2
# ORCA_STATUS_SUCCESS = 3
ORCA_STATUS_FAILED = 4

LOGGER = CumulusLogger()


class RestoreRequestError(Exception):
    """
    Exception to be raised if the restore request fails submission for any of the files.
    """


# noinspection PyUnusedLocal
def task(event: Dict, context: object) -> Dict[str, Any]:  # pylint: disable-msg=unused-argument
    """
    Task called by the handler to perform the work.
    This task will call the restore_request for each file. Restored files will be kept
    for {exp_days} days before they expire. A restore request will be tried up to {retries} times
    if it fails, waiting {retry_sleep_secs} between each attempt.
        Args:
            Note that because we are using CumulusMessageAdapter, this does not directly correspond to Lambda input.
            event: A dict with the following keys:
                'config' (dict): A dict with the following keys:
                    'glacier-bucket' (str): The name of the glacier bucket from which the files
                    will be restored.
                'input' (dict): A dict with the following keys:
                    'granules' (list(dict)): A list of dicts with the following keys:
                        'granuleId' (str): The id of the granule being restored.
                        'keys' (list(dict)): A list of dicts with the following keys:
                            'key' (str): Name of the file within the granule.  # TODO: This or example lies.
                            'dest_bucket' (str): The bucket the restored file will be moved
                                to after the restore completes.
                    'job_id' (str): The unique identifier used for tracking requests. If not present, will be generated.
            context: Passed through from the handler. Unused, but required by CMA.
        Environment Vars:
            RESTORE_EXPIRE_DAYS (int, optional, default = 5): The number of days
                the restored file will be accessible in the S3 bucket before it expires.
            RESTORE_REQUEST_RETRIES (int, optional, default = 3): The number of
                attempts to retry a restore_request that failed to submit.
            RESTORE_RETRY_SLEEP_SECS (int, optional, default = 0): The number of seconds
                to sleep between retry attempts.
            RESTORE_RETRIEVAL_TYPE (str, optional, default = 'Standard'): the Tier
                for the restore request. Valid values are 'Standard'|'Bulk'|'Expedited'.
        Returns:
            A dict with the following keys:
                'granules' (List): A list of dicts, each with the following keys:
                    'granuleId' (string): The id of the granule being restored.
                    'recover_files' (list(dict)): A list of dicts with the following keys:
                        'key' (str): Name of the file within the granule.
                        'dest_bucket' (str): The bucket the restored file will be moved
                            to after the restore completes.
                        'success' (boolean): True, indicating the restore request was submitted successfully.
                            If any value would be false, RestoreRequestError is raised instead.
                        'err_msg' (string): when success is False, this will contain
                            the error message from the restore error.
                    'keys': Same as recover_files, but without 'success' and 'err_msg'.
                'job_id' (str): The 'job_id' from event if present, otherwise a newly-generated uuid.
            Example:
                {'granules': [
                    {
                        'granuleId': 'granxyz',
                        'recover_files': [
                            {'key': 'path1', 'dest_bucket': 'bucket_name', 'success': True}
                        ]
                    }]}
        Raises:
            RestoreRequestError: Thrown if there are errors with the input request.
    """
    try:
        max_retries = int(os.environ[OS_ENVIRON_RESTORE_REQUEST_RETRIES_KEY])
    except KeyError:
        max_retries = DEFAULT_MAX_REQUEST_RETRIES

    try:
        retry_sleep_secs = float(os.environ[OS_ENVIRON_RESTORE_RETRY_SLEEP_SECS_KEY])
    except KeyError:
        retry_sleep_secs = DEFAULT_RESTORE_RETRY_SLEEP_SECS

    try:
        retrieval_type = os.environ[OS_ENVIRON_RESTORE_RETRIEVAL_TYPE_KEY]
        if retrieval_type not in ('Standard', 'Bulk', 'Expedited'):
            msg = (f"Invalid RESTORE_RETRIEVAL_TYPE: '{retrieval_type}'"
                   " defaulting to 'Standard'")
            LOGGER.info(msg)
            retrieval_type = DEFAULT_RESTORE_RETRIEVAL_TYPE
    except KeyError:
        retrieval_type = DEFAULT_RESTORE_RETRIEVAL_TYPE

    db_queue_url = str(os.environ[OS_ENVIRON_DB_QUEUE_URL_KEY])

    try:
        exp_days = int(os.environ[OS_ENVIRON_RESTORE_EXPIRE_DAYS_KEY])
    except KeyError:
        exp_days = DEFAULT_RESTORE_EXPIRE_DAYS

    if not event[EVENT_INPUT_KEY].keys().__contains__(INPUT_JOB_ID_KEY):
        event[EVENT_INPUT_KEY][INPUT_JOB_ID_KEY] = uuid.uuid4().__str__()

    return inner_task(event, max_retries, retry_sleep_secs, retrieval_type, exp_days, db_queue_url)


def inner_task(event: Dict, max_retries: int, retry_sleep_secs: float,
               retrieval_type: str, restore_expire_days: int, db_queue_url: str):
    try:
        glacier_bucket = event[EVENT_CONFIG_KEY][CONFIG_GLACIER_BUCKET_KEY]
    except KeyError:
        raise RestoreRequestError(
            f'request: {event} does not contain a config value for glacier-bucket')

    granules = event[EVENT_INPUT_KEY][INPUT_GRANULES_KEY]
    if len(granules) > 1:
        # todo: This is either a lie, or the loop below should be removed.
        raise RestoreRequestError(f'request_files can only accept 1 granule in the list. '
                                  f'This input contains {len(granules)}')
    s3 = boto3.client('s3')  # pylint: disable-msg=invalid-name

    # todo: Singular output variable from loop?
    copied_granule = {}
    for granule in granules:
        files = []
        for keys in granule[GRANULE_KEYS_KEY]:
            file_key = keys[FILE_KEY_KEY]
            destination_bucket_name = keys[FILE_DEST_BUCKET_KEY]
            if object_exists(s3, glacier_bucket, file_key):
                LOGGER.info(f"Added {file_key} to the list of files we'll attempt to recover.")
                a_file = {
                    FILE_KEY_KEY: file_key,
                    FILE_DEST_BUCKET_KEY: destination_bucket_name,
                    FILE_SUCCESS_KEY: False,
                    FILE_ERROR_MESSAGE_KEY: ''
                }
                files.append(a_file)
        copied_granule = granule.copy()
        copied_granule[GRANULE_RECOVER_FILES_KEY] = files

    # todo: Looks like this line is why multiple granules are not supported.
    # todo: Using the default value {} for copied_granule will cause this function to raise errors every time.
    process_granule(
        s3, copied_granule, glacier_bucket, restore_expire_days, max_retries, retry_sleep_secs, retrieval_type,
        event[EVENT_INPUT_KEY][INPUT_JOB_ID_KEY], db_queue_url)

    # Cumulus expects response (payload.granules) to be a list of granule objects.
    return {
        INPUT_GRANULES_KEY: [copied_granule],
        INPUT_JOB_ID_KEY: event[EVENT_INPUT_KEY][INPUT_JOB_ID_KEY]
    }


def process_granule(s3: BaseClient,
                    granule: Dict[str, Union[str, List[Dict]]],
                    glacier_bucket: str,
                    restore_expire_days: int,
                    max_retries: int, retry_sleep_secs: float,
                    retrieval_type: str,
                    job_id: str,
                    db_queue_url: str):  # pylint: disable-msg=invalid-name
    """Call restore_object for the files in the granule_list. Modifies granule for output.
        Args:
            s3: An instance of boto3 s3 client
            granule: A dict with the following keys:
                'granuleId' (str): The id of the granule being restored.
                'recover_files' (list(dict)): A list of dicts with the following keys:
                    'key' (str): Name of the file within the granule.
                    'dest_bucket' (str): The bucket the restored file will be moved
                        to after the restore completes
                    'success' (bool): Should enter this method set to False. Modified to 'True' by method end.
                    'err_msg' (str): Will be modified if error occurs.


            glacier_bucket: The S3 glacier bucket name.
            restore_expire_days:
                The number of days the restored file will be accessible in the S3 bucket before it expires.
            max_retries: todo
            retry_sleep_secs: todo
            retrieval_type: todo
            db_queue_url: todo
            job_id: The unique identifier used for tracking requests.

        Raises: RestoreRequestError if any file restore could not be initiated.
    """
    request_time = datetime.now(timezone.utc).isoformat()
    attempt = 1
    granule_id = granule[GRANULE_GRANULE_ID_KEY]

    # todo: Better async.
    post_status_for_job_to_queue(job_id, granule_id, ORCA_STATUS_PENDING, request_time, None, glacier_bucket,
                                 RequestMethod.POST,
                                 db_queue_url,
                                 max_retries, retry_sleep_secs)

    while attempt <= max_retries + 1:
        for a_file in granule[GRANULE_RECOVER_FILES_KEY]:
            if not a_file[FILE_SUCCESS_KEY]:
                try:
                    restore_object(s3, a_file[FILE_KEY_KEY], restore_expire_days, glacier_bucket, attempt, job_id,
                                   retrieval_type)
                    a_file[FILE_SUCCESS_KEY] = True
                    a_file[FILE_ERROR_MESSAGE_KEY] = ''

                    post_status_for_file_to_queue(
                        job_id, granule_id, os.path.basename(a_file[FILE_KEY_KEY]),
                        a_file[FILE_KEY_KEY],
                        a_file[FILE_DEST_BUCKET_KEY],
                        ORCA_STATUS_PENDING,
                        None,
                        request_time,
                        datetime.now(timezone.utc).isoformat(),
                        None,
                        RequestMethod.POST,
                        db_queue_url,
                        max_retries,
                        retry_sleep_secs)

                except ClientError as err:
                    LOGGER.warning(err)
                    a_file[FILE_ERROR_MESSAGE_KEY] = str(err)

        attempt = attempt + 1
        if attempt <= max_retries + 1:
            # Check for early completion.
            if all(a_file[FILE_SUCCESS_KEY] for a_file in granule[GRANULE_RECOVER_FILES_KEY]):
                break
            time.sleep(retry_sleep_secs)

    any_error = False
    for a_file in granule[GRANULE_RECOVER_FILES_KEY]:
        # if any file failed, the whole granule will fail
        if not a_file[FILE_SUCCESS_KEY]:
            any_error = True
            # If this is reached, that means there is no entry in the db for file's status.
            post_status_for_file_to_queue(
                job_id, granule_id, os.path.basename(a_file[FILE_KEY_KEY]),
                a_file[FILE_KEY_KEY],
                a_file[FILE_DEST_BUCKET_KEY],
                ORCA_STATUS_FAILED,
                a_file.get(FILE_ERROR_MESSAGE_KEY, None),
                request_time,
                datetime.now(timezone.utc).isoformat(),
                None,
                RequestMethod.POST,
                db_queue_url,
                max_retries,
                retry_sleep_secs)

    if any_error:
        LOGGER.error(f"One or more files failed to be requested from {glacier_bucket}.{granule}")
        raise RestoreRequestError(f'One or more files failed to be requested. {granule}')


def object_exists(s3_cli: BaseClient, glacier_bucket: str, file_key: str) -> bool:
    """Check to see if an object exists in S3 Glacier.
        Args:
            s3_cli: An instance of boto3 s3 client
            glacier_bucket: The S3 glacier bucket name
            file_key: The key of the Glacier object
        Returns:
            True if the object exists, otherwise False.
        """
    try:
        # head_object will fail with a thrown 404 if the object doesn't exist
        # todo: The above case was not covered, and should be considered untested.
        s3_cli.head_object(Bucket=glacier_bucket, Key=file_key)
        return True
    except ClientError as err:
        LOGGER.error(err)
        code = err.response['Error']['Code']
        if code == 'NoSuchKey' or code == 'NotFound':  # Unit tests say 'NotFound', some online docs say 'NoSuchKey'
            return False
        raise
        # todo: Online docs suggest we could catch 'S3.Client.exceptions.NoSuchKey instead of deconstructing ClientError


def restore_object(s3_cli: BaseClient, key: str, days: int, db_glacier_bucket_key: str, attempt: int, job_id: str,
                   retrieval_type: str = 'Standard'
                   ) -> None:
    # noinspection SpellCheckingInspection
    """Restore an archived S3 Glacier object in an Amazon S3 bucket.
        Args:
            s3_cli: An instance of boto3 s3 client.
            key: The key of the Glacier object being restored.
            days: How many days the restored file will be accessible in the S3 bucket before it expires.
            db_glacier_bucket_key: The S3 bucket name.
            attempt: The attempt number for logging purposes.
            job_id: The unique id of the job. Used for logging.
            retrieval_type: Glacier Tier. Valid values are 'Standard'|'Bulk'|'Expedited'. Defaults to 'Standard'.
        Raises:
            ClientError: Raises ClientErrors from restore_object.
    """
    request = {'Days': days,
               'GlacierJobParameters': {'Tier': retrieval_type}}
    # Submit the request
    try:
        s3_cli.restore_object(Bucket=db_glacier_bucket_key,
                              Key=key,
                              RestoreRequest=request)

    except ClientError as c_err:
        # NoSuchBucket, NoSuchKey, or InvalidObjectState error == the object's
        # storage class was not GLACIER
        LOGGER.error(f"{c_err}. bucket: {db_glacier_bucket_key} file: {key} Job ID: {job_id}")
        raise c_err

    LOGGER.info(
        f"Restore {key} from {db_glacier_bucket_key} "
        f"attempt {attempt} successful. Job ID: {job_id}")


# todo: Move to shared lib
def post_status_for_job_to_queue(job_id: str, granule_id: str, status_id: Optional[int],
                                 request_time: Optional[str], completion_time: Optional[str],
                                 archive_destination: Optional[str],
                                 request_method: RequestMethod, db_queue_url: str,
                                 max_retries: int, retry_sleep_secs: float):
    new_data = {'job_id': job_id, 'granule_id': granule_id}
    if status_id is not None:
        new_data['status_id'] = status_id
    if request_time is not None:
        new_data['request_time'] = request_time
    if completion_time is not None:
        new_data['completion_time'] = completion_time
    if archive_destination is not None:
        new_data['archive_destination'] = archive_destination

    post_entry_to_queue('orca_recoveryjob',
                        new_data,
                        request_method, db_queue_url, max_retries, retry_sleep_secs)


# todo: Move to shared lib
def post_status_for_file_to_queue(job_id: str, granule_id: str, filename: str, key_path: Optional[str],
                                  restore_destination: Optional[str],
                                  status_id: Optional[int], error_message: Optional[str],
                                  request_time: Optional[str], last_update: str,
                                  completion_time: Optional[str],
                                  request_method: RequestMethod,
                                  db_queue_url: str,
                                  max_retries: int, retry_sleep_secs: float):
    new_data = {'job_id': job_id,
                'granule_id': granule_id,
                'filename': filename}
    if key_path is not None:
        new_data['key_path'] = key_path
    if restore_destination is not None:
        new_data['restore_destination'] = restore_destination
    if status_id is not None:
        new_data['status_id'] = status_id
    if error_message is not None:
        new_data['error_message'] = error_message
    if request_time is not None:
        new_data['request_time'] = request_time
    if last_update is not None:
        new_data['last_update'] = last_update
    if completion_time is not None:
        new_data['completion_time'] = completion_time

    post_entry_to_queue('orca_recoverfile',
                        new_data,
                        request_method,
                        db_queue_url, max_retries, retry_sleep_secs)


sqs = boto3.client('sqs')


# todo: Move to shared lib
def post_entry_to_queue(table_name: str, new_data: Dict[str, Any], request_method: RequestMethod, db_queue_url: str,
                        max_retries: int, retry_sleep_secs: float):
    body = json.dumps(new_data, indent=4)
    for attempt in range(1, max_retries + 1):
        try:
            sqs.send_message(
                QueueUrl=db_queue_url
            )
            sqs.send_message(
                QueueUrl=db_queue_url,
                MessageDeduplicationId=table_name + request_method.value + body,
                MessageGroupId='request_files',
                MessageAttributes={
                    'RequestMethod': {
                        'DataType': 'String',
                        'StringValue': request_method.value
                    },
                    'TableName': {
                        'DataType': 'String',
                        'StringValue': table_name
                    }
                },
                MessageBody=body
            )
            return
        except Exception as e:
            if attempt == max_retries + 1:
                LOGGER.error(f"Error while logging row {json.dumps(new_data, indent=4)} "
                             f"to table {table_name}: {e}")
                raise e
            time.sleep(retry_sleep_secs)
            continue


def handler(event: Dict[str, Any], context):  # pylint: disable-msg=unused-argument
    """Lambda handler. Initiates a restore_object request from glacier for each file of a granule.
    Note that this function is set up to accept a list of granules, (because Cumulus sends a list),
    but at this time, only 1 granule will be accepted.
    This is due to the error handling. If the restore request for any file for a
    granule fails to submit, the entire granule (workflow) fails. If more than one granule were
    accepted, and a failure ocured, at present, it would fail all of them.
    Environment variables can be set to override how many days to keep the restored files, how
    many times to retry a restore_request, and how long to wait between retries.
        Environment Vars:
            RESTORE_EXPIRE_DAYS (int, optional, default = 5): The number of days
                the restored file will be accessible in the S3 bucket before it expires.
            RESTORE_REQUEST_RETRIES (int, optional, default = 3): The number of
                attempts to retry a restore_request that failed to submit.
            RESTORE_RETRY_SLEEP_SECS (int, optional, default = 0): The number of seconds
                to sleep between retry attempts.
            RESTORE_RETRIEVAL_TYPE (str, optional, default = 'Standard'): the Tier
                for the restore request. Valid values are 'Standard'|'Bulk'|'Expedited'.
            CUMULUS_MESSAGE_ADAPTER_DISABLED (str): If set to 'true', CumulusMessageAdapter does not modify input.
        Args:
            event: See schemas/input.json and combine with knowledge of CumulusMessageAdapter.
            context: An object required by AWS Lambda. Unused.
        Returns:
            A dict with the value at 'payload' matching schemas/output.json
                Combine with knowledge of CumulusMessageAdapter for other properties.
        Raises:
            RestoreRequestError: An error occurred calling restore_object for one or more files.
            The same dict that is returned for a successful granule restore, will be included in the
            message, with 'success' = False for the files for which the restore request failed to
            submit.
    """
    LOGGER.setMetadata(event, context)
    return run_cumulus_task(task, event, context)
