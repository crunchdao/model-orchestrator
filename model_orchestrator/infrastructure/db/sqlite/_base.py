from contextlib import closing
from typing import Optional, Any

import sqlite_utils

from model_orchestrator.utils.logging_utils import get_logger

logger = get_logger()


class SQLiteRepository:
    """
    A Repository implementation for SQLite using sqlite-utils.
    """

    def __init__(self, db_path: str):
        """
        db_path: path to the SQLite database file.
        Example: 'my_models.sqlite'
        """

        self.db_path = db_path
        logger.debug(f"Initializing {self.__class__.__name__} with db_path: %s", self.db_path)

    def _open(self) -> "closing[sqlite_utils.Database]":
        return closing(sqlite_utils.Database(self.db_path))

    def _add_column_if_not_exists(self, db: sqlite_utils.Database, table_name, column_name: str, column_type: Optional[Any], not_null_default=None):
        if column_name not in db[table_name].columns_dict:
            db[table_name].add_column(column_name, column_type, not_null_default=not_null_default)
