"""
Name: get_current_archive_list.py

Description:  Pulls entries from a queue and posts them to a DB.
"""
from datetime import datetime, timezone
import json
from typing import Any, List, Dict, Optional

# noinspection SpellCheckingInspection
import fastjsonschema as fastjsonschema
from cumulus_logger import CumulusLogger
from sqlalchemy import text
from sqlalchemy.future import Engine

from orca_shared.database import shared_db
from orca_shared.reconcile.shared_reconcile import OrcaStatus

RECORD_S3_KEY = "s3"
S3_BUCKET_KEY = "bucket"
S3_OBJECT_KEY = "object"
OBJECT_NAME_KEY = "name"

LOGGER = CumulusLogger()
# Generating schema validators can take time, so do it once and reuse.
# todo: use
try:
    with open("schemas/input.json", "r") as raw_schema:
        _INPUT_VALIDATE = fastjsonschema.compile(json.loads(raw_schema.read()))
    with open("schemas/output.json", "r") as raw_schema:
        _OUTPUT_VALIDATE = fastjsonschema.compile(json.loads(raw_schema.read()))
except Exception as ex:
    # Can't use f"" because of '{}' bug in CumulusLogger.
    LOGGER.error("Could not build schema validator: {ex}", ex=ex)
    raise


def task(records: List[Dict[str, Any]], db_connect_info: Dict) -> None:
    """
    Sends each individual record to send_record_to_database.

    Args:
        records: A list of Dicts. See send_record_to_database for schema info.
        db_connect_info: See shared_db.py's get_configuration for further details.
    """
    engine = shared_db.get_user_connection(db_connect_info)
    for record in records:
        # todo: Create initial job
        # todo: get manifest
        create_job_with_s3_inventory_in_postgres(record[RECORD_S3_KEY][S3_BUCKET_KEY],
                                                 record[RECORD_S3_KEY][S3_OBJECT_KEY][OBJECT_NAME_KEY],
                                                 engine)
        # todo: On error, set job status


def create_job(engine: Engine) -> int:
    """
    Creates the initial status entry for a job.

    Args:
        engine: The sqlalchemy engine to use for contacting the database.

    Returns: The auto-incremented job_id from the database.
    """
    try:
        LOGGER.debug(f"Creating status for job.")
        with engine.begin() as connection:
            # Within this transaction import the csv and update the job status
            connection.execute(
                update_job_sql(),
                [
                    {
                        "id": job_id,
                        "status_id": OrcaStatus.STAGED.value,
                        "last_update": datetime.now(timezone.utc).isoformat(),
                        "end_time": None,
                        "error_message": None,
                    }
                ],
            )
            # todo: Generate uri from bucket, key, and region https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PostgreSQL.S3Import.html
            connection.execute(trigger_csv_load_from_s3(), [
                {
                    "report_bucket_name": report_bucket_name,
                    "csv_key_path": csv_key_path,
                    "region": region
                }
            ])
    except Exception as sql_ex:
        # Can't use f"" because of '{}' bug in CumulusLogger.
        LOGGER.error(
            "Error while updating job '{job_id}': {sql_ex}",
            job_id=job_id,
            sql_ex=sql_ex,
        )
        raise


def create_job_sql():
    return text(  # todo: review/create
        # todo: Auto-gen id
        """
        INSERT INTO reconcile_job
            ("status_id", "start_time", "last_update", "end_time", "orca_archive_location", "error_message")
        VALUES
            (:status_id, :start_time, :last_update, NULL, NULL, NULL)"""
    )


def update_job_with_s3_inventory_in_postgres(report_bucket_name, csv_key_path, job_id: int, engine: Engine):
    """
    Deconstructs a record to its components and calls send_values_to_database with the result.

    Args:
        report_bucket_name: The name of the bucket the csv is located in.
        csv_key_path: The path of the csv within the report bucket.
        job_id: The id of the job to associate info with.
        engine: The sqlalchemy engine to use for contacting the database.
    """
    try:
        LOGGER.debug(f"Creating reconcile records for job {job_id}.")
        with engine.begin() as connection:
            # Within this transaction import the csv and update the job status
            connection.execute(
                update_job_sql(),
                [
                    {
                        "id": job_id,
                        "status_id": OrcaStatus.STAGED.value,
                        "last_update": datetime.now(timezone.utc).isoformat(),
                        "end_time": None,
                        "error_message": None,
                    }
                ],
            )
            # todo: Generate uri from bucket, key, and region https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PostgreSQL.S3Import.html
            connection.execute(trigger_csv_load_from_s3(), [
                {
                    "report_bucket_name": report_bucket_name,
                    "csv_key_path": csv_key_path,
                    "region": region
                }
            ])
    except Exception as sql_ex:
        # Can't use f"" because of '{}' bug in CumulusLogger.
        LOGGER.error(
            "Error while updating job '{job_id}': {sql_ex}",
            job_id=job_id,
            sql_ex=sql_ex,
        )
        raise


def update_job_sql():
    return text(  # todo: review
        """
        UPDATE
            reconcile_job
        SET
            status_id = :status_id,
            last_update = :last_update
            end_time = :end_time,
            error_message = :error_message,
        WHERE
            id = :id
        VALUES"""
    )


# https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PostgreSQL.S3Import.html
def trigger_csv_load_from_s3():
    # todo: Complete
    return text(
        """
        SELECT aws_s3.table_import_from_s3(
            :report_table_name,
            # todo: columns, https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/USER_PostgreSQL.S3Import.html#USER_PostgreSQL.S3Import.FileFormats
            # todo: format,
            # todo: create_s3_uri
        )
        """
    )


def handler(event: Dict[str, List], context) -> None:
    """
    Lambda handler. Receives a list of queue entries from an SQS queue, and posts them to a database.

    Args:
        event: See input.json for details.
        context: An object passed through by AWS. Used for tracking.
    Environment Vars: See shared_db.py's get_configuration for further details.
    """
    LOGGER.setMetadata(event, context)

    db_connect_info = shared_db.get_configuration()

    task(event["Records"], db_connect_info)
