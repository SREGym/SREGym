import datetime
import json
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from provisioner.utils.logger import logger


class CLUSTER_STATUS:
    STATUS_AUTO_PROVISIONING = "auto_provisioning"
    STATUS_USER_PROVISIONING = "user_provisioning"
    STATUS_UNCLAIMED_READY = "unclaimed_ready"
    STATUS_CLAIMED = "claimed"
    # STATUS_PENDING_CLEANUP = "pending_cleanup"
    STATUS_TERMINATING = "terminating"
    STATUS_ERROR = "error"
    STATUS_TERMINATED = "terminated"


class SREGYM_STATUS:
    SREGYM_PENDING = "pending"
    SREGYM_SUCCESS = "success"
    SREGYM_FAILED = "failed"
    SREGYM_NOT_ATTEMPTED = "not_attempted"


class StateManager:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)
        self._init_db()

    def _get_db_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _init_db(self):
        conn = self._get_db_connection()
        try:
            with self._get_db_connection() as conn:
                cursor = conn.cursor()

                # Users Table
                cursor.execute(
                    """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    ssh_public_key TEXT NOT NULL,
                    registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )"""
                )

                # Clusters Table
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS clusters (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        slice_name TEXT UNIQUE NOT NULL,
                        aggregate_name TEXT,
                        status TEXT NOT NULL DEFAULT 'auto_provisioning',
                        hardware_type TEXT,
                        os_type TEXT,
                        node_count INTEGER,
                        login_info TEXT,
                        control_node_hostname TEXT,
                        claimed_by_user_id TEXT,
                        user_ssh_key_installed BOOLEAN DEFAULT FALSE,
                        sregym_setup_status TEXT DEFAULT 'not_attempted',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        claimed_at TIMESTAMP,
                        last_extended_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        last_activity_at TIMESTAMP,
                        cloudlab_expires_at TIMESTAMP,
                        evaluation_override BOOLEAN DEFAULT FALSE,
                        last_error_message TEXT,
                        FOREIGN KEY (claimed_by_user_id) REFERENCES users(user_id) ON DELETE SET NULL
                    )
                """
                )
                conn.commit()
                logger.info(f"Database initialized/checked at {self.db_path}")
        except sqlite3.Error as e:
            logger.error(f"Database initialization error: {e}", exc_info=True)
            raise e

    # --- User Management ---
    def add_user(self, user_id: str, ssh_public_key: str) -> bool:
        try:
            with self._get_db_connection() as conn:
                conn.execute("INSERT INTO users (user_id, ssh_public_key) VALUES (?, ?)", (user_id, ssh_public_key))
                conn.commit()
                logger.info(f"User {user_id} registered.")
                return True
        except sqlite3.IntegrityError:
            logger.warning(f"User {user_id} already exists.")
            return False
        except sqlite3.Error as e:
            logger.error(f"Error adding user {user_id}: {e}", exc_info=True)
            return False

    def get_user(self, user_id: str) -> Optional[Dict[str, Any]]:
        try:
            with self._get_db_connection() as conn:
                cursor = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()
                return dict(row) if row else None
        except sqlite3.Error as e:
            logger.error(f"Error getting user {user_id}: {e}", exc_info=True)
            raise e

    def user_exists(self, user_id: str) -> bool:
        return self.get_user(user_id) is not None

    # --- Cluster Management ---
    def create_cluster_record(
        self,
        slice_name: str,
        aggregate_name: str,
        os_type: str,
        node_count: int,
        hardware_type: Optional[str] = None,
        login_info: Optional[List[List[Any]]] = None,
        status: str = CLUSTER_STATUS.STATUS_AUTO_PROVISIONING,
        cloudlab_expires_at: Optional[datetime.datetime] = None,
        claimed_by_user_id: Optional[str] = None,
        evaluation_override: bool = False,
        last_extended_at: Optional[datetime.datetime] = None,
    ) -> Optional[str]:
        """Creates a new cluster record. Returns slice_name on success."""
        now = datetime.datetime.now()
        login_info_json = json.dumps(login_info) if login_info else None
        try:
            with self._get_db_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO clusters (slice_name, aggregate_name, status, hardware_type, os_type, node_count,
                                          created_at, last_activity_at, cloudlab_expires_at, claimed_by_user_id,
                                          evaluation_override, login_info, last_extended_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        slice_name,
                        aggregate_name,
                        status,
                        hardware_type,
                        os_type,
                        node_count,
                        now,
                        now,
                        cloudlab_expires_at,
                        claimed_by_user_id,
                        evaluation_override,
                        login_info_json,
                        last_extended_at or now,
                    ),
                )
                conn.commit()
                logger.info(f"Cluster record created for {slice_name} with status {status}.")
                return slice_name
        except sqlite3.IntegrityError:
            logger.error(f"Cluster with slice_name {slice_name} already exists.", exc_info=True)
            raise sqlite3.IntegrityError(f"Cluster with slice_name {slice_name} already exists.")
        except sqlite3.Error as e:
            logger.error(f"Error creating cluster record for {slice_name}: {e}", exc_info=True)
            raise e

    def get_cluster_by_slice_name(self, slice_name: str) -> Optional[Dict[str, Any]]:
        try:
            with self._get_db_connection() as conn:
                cursor = conn.execute("SELECT * FROM clusters WHERE slice_name = ?", (slice_name,))
                row = cursor.fetchone()

                if row:
                    cluster_data = dict(row)
                    if cluster_data.get("login_info"):
                        try:
                            cluster_data["login_info"] = json.loads(cluster_data["login_info"])
                        except json.JSONDecodeError:
                            logger.error(f"Invalid JSON in login_info for cluster {slice_name}.")
                            cluster_data["login_info"] = None
                    return cluster_data
                else:
                    return None

        except sqlite3.Error as e:
            logger.error(f"Error getting cluster {slice_name}: {e}", exc_info=True)
            raise e

    def update_cluster_record(self, slice_name: str, **kwargs) -> bool:
        if not kwargs:
            logger.warning(f"No fields provided to update for cluster {slice_name}.")
            raise ValueError("No fields provided to update for cluster {slice_name}.")

        valid_keys = [
            # "slice_name",
            "aggregate_name",
            "hardware_type",
            # "os_type",
            # "node_count",
            "status",
            "control_node_hostname",
            "login_info",
            "claimed_by_user_id",
            "user_ssh_key_installed",
            "sregym_setup_status",
            "claimed_at",
            "last_extended_at",
            "last_activity_at",
            "cloudlab_expires_at",
            "evaluation_override",
            "last_error_message",
            "created_at",
        ]

        set_clauses = []
        values = []
        for key, value in kwargs.items():
            if key not in valid_keys:
                logger.error(f"Invalid field '{key}' for cluster update.")
                return False

            if key == "login_info" and value is not None:
                value = json.dumps(value)

            set_clauses.append(f"{key} = ?")
            values.append(value)

        values.append(slice_name)
        sql = f"UPDATE clusters SET {', '.join(set_clauses)} WHERE slice_name = ?"

        try:
            with self._get_db_connection() as conn:
                conn.execute(sql, tuple(values))
                conn.commit()
                logger.info(f"Cluster {slice_name} updated with: {kwargs}")
        except sqlite3.Error as e:
            logger.error(f"Error updating cluster {slice_name}: {e}", exc_info=True)
            raise e

    def delete_cluster_record(self, slice_name: str, soft_delete: bool = True) -> bool:
        try:
            with self._get_db_connection() as conn:
                if soft_delete:
                    cursor = conn.execute(
                        "UPDATE clusters SET status = ? WHERE slice_name = ?",
                        (CLUSTER_STATUS.STATUS_TERMINATED, slice_name),
                    )
                else:
                    cursor = conn.execute("DELETE FROM clusters WHERE slice_name = ?", (slice_name,))
                conn.commit()
                if cursor.rowcount > 0:
                    logger.info(f"Cluster record {slice_name} deleted.")
                else:
                    logger.warning(f"No cluster record found to delete for {slice_name}.")
        except sqlite3.Error as e:
            logger.error(f"Error deleting cluster record {slice_name}: {e}", exc_info=True)
            raise e

    # --- Specific Queries ---

    def _parse_cluster_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """Helper to convert a row to dict and parse login_info."""
        if not row:
            return {}
        cluster_data = dict(row)
        if cluster_data.get("login_info"):
            try:
                cluster_data["login_info"] = json.loads(cluster_data["login_info"])
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse login_info for cluster ID {cluster_data.get('id')}")
                cluster_data["login_info"] = None
        return cluster_data

    def get_clusters_by_status(self, status: str) -> List[Dict[str, Any]]:
        try:
            with self._get_db_connection() as conn:
                cursor = conn.execute("SELECT * FROM clusters WHERE status = ?", (status,))
                rows = cursor.fetchall()
                return [self._parse_cluster_row(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting clusters with status {status}: {e}", exc_info=True)
            raise e

    def get_unclaimed_ready_clusters(self) -> List[Dict[str, Any]]:
        return self.get_clusters_by_status(CLUSTER_STATUS.STATUS_UNCLAIMED_READY)

    def get_claimed_clusters_by_user(self, user_id: str) -> List[Dict[str, Any]]:
        try:
            with self._get_db_connection() as conn:
                cursor = conn.execute(
                    "SELECT * FROM clusters WHERE claimed_by_user_id = ? AND status = ?",
                    (user_id, CLUSTER_STATUS.STATUS_CLAIMED),
                )
                rows = cursor.fetchall()
                return [self._parse_cluster_row(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting claimed clusters for user {user_id}: {e}", exc_info=True)
            raise e

    def count_total_managed_clusters(self) -> int:
        """Counts clusters that contribute to the MAX_TOTAL_CLUSTERS limit."""
        # These are clusters that are active or in a state that will soon become active/cleaned.
        # Excludes clusters that are definitely gone or in a permanent error state.
        managed_statuses = (
            CLUSTER_STATUS.STATUS_AUTO_PROVISIONING,
            CLUSTER_STATUS.STATUS_USER_PROVISIONING,
            CLUSTER_STATUS.STATUS_UNCLAIMED_READY,
            CLUSTER_STATUS.STATUS_CLAIMED,
            # CLUSTER_STATUS.STATUS_PENDING_CLEANUP,
        )

        try:
            with self._get_db_connection() as conn:
                cursor = conn.execute(
                    f"SELECT COUNT(*) FROM clusters WHERE status IN (? , ? , ? , ?)", managed_statuses
                )
                count = cursor.fetchone()[0]
                return count if count is not None else 0
        except sqlite3.Error as e:
            logger.error(f"Error counting total managed clusters: {e}", exc_info=True)
            raise e

    def count_total_available_clusters(self) -> int:
        """Counts clusters that are available to be claimed."""
        available_statuses = (
            CLUSTER_STATUS.STATUS_AUTO_PROVISIONING,
            # CLUSTER_STATUS.STATUS_USER_PROVISIONING,
            CLUSTER_STATUS.STATUS_UNCLAIMED_READY,
            # CLUSTER_STATUS.STATUS_PENDING_CLEANUP,
        )

        try:
            with self._get_db_connection() as conn:
                cursor = conn.execute(f"SELECT COUNT(*) FROM clusters WHERE status IN (? , ?)", available_statuses)
                count = cursor.fetchone()[0]
                return count if count is not None else 0
        except sqlite3.Error as e:
            logger.error(f"Error counting total available clusters: {e}", exc_info=True)
            raise e

    def count_user_claimed_clusters(self, user_id: str) -> int:
        try:
            with self._get_db_connection() as conn:
                cursor = conn.execute(
                    "SELECT COUNT(*) FROM clusters WHERE claimed_by_user_id = ? AND (status = ? OR status = ?)",
                    (user_id, CLUSTER_STATUS.STATUS_CLAIMED, CLUSTER_STATUS.STATUS_USER_PROVISIONING),
                )
                count = cursor.fetchone()[0]
                return count if count is not None else 0
        except sqlite3.Error as e:
            logger.error(f"Error counting clusters for user {user_id}: {e}", exc_info=True)
            raise e

    def get_all_clusters(self) -> List[Dict[str, Any]]:
        try:
            with self._get_db_connection() as conn:
                cursor = conn.execute("SELECT * FROM clusters ORDER BY created_at DESC")
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except sqlite3.Error as e:
            logger.error(f"Error getting all clusters: {e}", exc_info=True)
            raise e
