from contextlib import contextmanager
from typing import Any, Optional, Tuple
import time

import duckdb

import dbt.exceptions
from dbt.adapters.base import Credentials
from dbt.adapters.sql import SQLConnectionManager
from dbt.contracts.connection import (
    AdapterRequiredConfig,
    ConnectionState,
    AdapterResponse,
)
from dbt.logger import GLOBAL_LOGGER as logger

from dataclasses import dataclass


@dataclass
class DuckDBCredentials(Credentials):
    database: str = "main"
    schema: str = "main"
    path: str = ":memory:"

    # any extensions we want to install/load (httpfs, json, etc.)
    extensions: Optional[Tuple[str, ...]] = None

    # for connecting to data in S3 via the httpfs extension
    s3_region: Optional[str] = None
    s3_access_key_id: Optional[str] = None
    s3_secret_access_key: Optional[str] = None
    s3_session_token: Optional[str] = None

    @property
    def type(self):
        return "duckdb"

    def _connection_keys(self):
        return ("database", "schema", "path")


class DuckDBCursorWrapper:
    def __init__(self, cursor):
        self._cursor = cursor

    # forward along all non-execute() methods/attribute look ups
    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def execute(self, sql, bindings=None):
        try:
            if bindings is None:
                return self._cursor.execute(sql)
            else:
                return self._cursor.execute(sql, bindings)
        except RuntimeError as e:
            raise dbt.exceptions.RuntimeException(str(e))


class DuckDBConnectionWrapper:
    def __init__(self, conn):
        self._conn = conn
        self._cursor = DuckDBCursorWrapper(self._conn.cursor())

    # forward along all non-cursor() methods/attribute look ups
    def __getattr__(self, name):
        return getattr(self._conn, name)

    def cursor(self):
        return self._cursor


class DuckDBConnectionManager(SQLConnectionManager):
    TYPE = "duckdb"

    def __init__(self, profile: AdapterRequiredConfig):
        super().__init__(profile)
        if profile.threads > 1:
            raise dbt.exceptions.RuntimeException(
                "dbt-duckdb only supports 1 thread at this time"
            )

    @classmethod
    def open(cls, connection):
        if connection.state == ConnectionState.OPEN:
            logger.debug("Connection is already open, skipping open.")
            return connection

        credentials = cls.get_credentials(connection.credentials)
        try:
            conn = duckdb.connect(credentials.path, read_only=False)
            connection.handle = DuckDBConnectionWrapper(conn)
            connection.state = ConnectionState.OPEN
            h = connection.handle

            # load any extensions on the handle
            if credentials.extensions is not None:
                for extension in credentials.extensions:
                    h.execute(f"LOAD '{extension}'")

            if credentials.s3_region is not None:
                h.execute("LOAD 'httpfs'")
                h.execute(f"SET s3_region = '{credentials.s3_region}'")
                if credentials.s3_session_token is not None:
                    h.execute(
                        f"SET s3_session_token = '{credentials.s3_session_token}'"
                    )
                elif credentials.s3_access_key_id is not None:
                    h.execute(
                        f"SET s3_access_key_id = '{credentials.s3_access_key_id}'"
                    )
                    h.execute(
                        f"SET s3_secret_access_key = '{credentials.s3_secret_access_key}'"
                    )
                else:
                    raise dbt.exceptions.RuntimeException(
                        "You must specify either s3_session_token or s3_access_key_id and s3_secret_access_key"
                    )

        except RuntimeError as e:
            logger.debug(
                "Got an error when attempting to open a duckdb "
                "database: '{}'".format(e)
            )

            connection.handle = None
            connection.state = ConnectionState.FAIL

            raise dbt.exceptions.FailedToConnectException(str(e))

        return connection

    def cancel(self, connection):
        pass

    @contextmanager
    def exception_handler(self, sql: str, connection_name="master"):
        try:
            yield
        except dbt.exceptions.RuntimeException as dbte:
            raise
        except RuntimeError as e:
            logger.debug("duckdb error: {}".format(str(e)))
        except Exception as exc:
            logger.debug("Error running SQL: {}".format(sql))
            logger.debug("Rolling back transaction.")
            raise dbt.exceptions.RuntimeException(str(exc)) from exc

    @classmethod
    def get_credentials(cls, credentials):
        return credentials

    @classmethod
    def get_response(cls, cursor) -> AdapterResponse:
        # https://github.com/dbt-labs/dbt-spark/issues/142
        message = "OK"
        return AdapterResponse(_message=message)
