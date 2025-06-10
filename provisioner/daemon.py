import datetime
import signal
import subprocess
import threading
import time
from typing import Optional

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from provisioner.cloudlab_provisioner import CloudlabProvisioner
from provisioner.config.settings import DefaultSettings
from provisioner.state_manager import CLUSTER_STATUS, SREARENA_STATUS, StateManager
from provisioner.utils.email_sender import EmailSender
from provisioner.utils.logger import logger
from provisioner.utils.ssh import SSHManager
from scripts.geni_lib.cluster_setup import setup_cloudlab_cluster_with_srearena

# Global stop event for graceful shutdown
stop_event = threading.Event()


class ProvisionerDaemon:
    def __init__(self):
        logger.info("Initializing Provisioner Daemon...")
        self.state_manager = StateManager(db_path=DefaultSettings.DATABASE_PATH)
        self.cloudlab = CloudlabProvisioner()

        self.scheduler = BlockingScheduler()
        logger.info("Provisioner Daemon initialized.")

    def _get_ssh_manager(
        self, hostname: str, port: int = 22, timeout: int = DefaultSettings.DEFAULT_SSH_TIME_OUT_SECONDS
    ) -> SSHManager:
        """
        Create an SSHManager instance for a given host.
        """
        return SSHManager(
            hostname=hostname,
            username=DefaultSettings.PROVISIONER_DEFAULT_SSH_USERNAME,
            private_key_path=DefaultSettings.PROVISIONER_SSH_PRIVATE_KEY_PATH,
            port=port,
            timeout=timeout,
        )

    def check_automatic_provisioning(self):
        logger.info("Running: Automatic Provisioning Check")
        try:
            effective_pool_size = self.state_manager.count_total_available_clusters()
            needed = DefaultSettings.MIN_AVAILABLE_CLUSTERS - effective_pool_size

            logger.info(f"Pool Status: EffectivePool={effective_pool_size}. Needed={needed}")

            for _ in range(max(0, needed)):
                current_total_managed = self.state_manager.count_total_managed_clusters()
                if current_total_managed >= DefaultSettings.MAX_TOTAL_CLUSTERS:
                    logger.warning(
                        f"Max total clusters ({DefaultSettings.MAX_TOTAL_CLUSTERS}) reached. Cannot auto-provision more."
                    )
                    break

                logger.info(f"Attempting to auto-provision a new cluster. Current total: {current_total_managed}")
                slice_name = self.cloudlab.generate_slice_name()

                # Record intention to provision
                self.state_manager.create_cluster_record(
                    slice_name=slice_name,
                    aggregate_name="<PENDING>",
                    # hardware_type=DefaultSettings.DEFAULT_HARDWARE_TYPE,
                    os_type=DefaultSettings.DEFAULT_OS_TYPE,
                    node_count=DefaultSettings.DEFAULT_NODE_COUNT,
                    status=CLUSTER_STATUS.STATUS_AUTO_PROVISIONING,
                )

                experiment_info = None

                try:
                    experiment_info = self.cloudlab.create_experiment(
                        slice_name=slice_name,
                        hardware_type=DefaultSettings.DEFAULT_HARDWARE_TYPE,
                        os_type=DefaultSettings.DEFAULT_OS_TYPE,
                        node_count=DefaultSettings.DEFAULT_NODE_COUNT,
                        duration=DefaultSettings.UNCLAIMED_CLUSTER_TIMEOUT_HOURS,
                    )

                    if experiment_info and experiment_info.get("login_info"):
                        control_node_info = next((n for n in experiment_info["login_info"] if n[0] == "control"), None)
                        if not control_node_info:
                            raise ValueError("Control node info not found in login_info")

                        hostname = control_node_info[2]
                        expires_at = datetime.datetime.now() + datetime.timedelta(hours=experiment_info["duration"])

                        self.state_manager.update_cluster_record(
                            slice_name,
                            aggregate_name=experiment_info["aggregate_name"],
                            hardware_type=experiment_info["hardware_type"],
                            control_node_hostname=hostname,
                            login_info=experiment_info["login_info"],
                            cloudlab_expires_at=expires_at,
                            # Status remains PROVISIONING until SRE Arena setup
                        )
                        logger.info(f"Cluster {slice_name} provisioned by Cloudlab. Host: {hostname}")

                        try:
                            while not self.cloudlab.are_nodes_ready(slice_name, experiment_info["aggregate_name"]):
                                logger.info(f"Waiting for nodes to be ready for {slice_name} on {hostname}...")
                                time.sleep(10)
                            logger.info(f"Nodes are ready for {slice_name} on {hostname}.")
                        except Exception as e:
                            logger.error(f"Error: {e}")
                            self.state_manager.update_cluster_record(
                                slice_name, status=CLUSTER_STATUS.STATUS_ERROR, last_error_message=str(e)
                            )
                            continue

                        # NOTE: not setting up SRE Arena when auto provisioning rather when user claims a cluster
                        # self._setup_sre_arena_and_finalize(experiment_info)

                        self.state_manager.update_cluster_record(
                            slice_name, status=CLUSTER_STATUS.STATUS_UNCLAIMED_READY
                        )

                    else:
                        err_msg = f"Failed to create experiment {slice_name} via Cloudlab."
                        logger.error(err_msg)
                        self.state_manager.update_cluster_record(
                            slice_name,
                            status=CLUSTER_STATUS.STATUS_ERROR,
                            last_error_message=err_msg,
                        )
                except Exception as e:
                    logger.error(f"Error during Cloudlab provisioning for {slice_name}: {e}", exc_info=True)
                    self.state_manager.update_cluster_record(
                        slice_name, status=CLUSTER_STATUS.STATUS_ERROR, last_error_message=str(e)
                    )

                    # If was provisioned, delete the cluster
                    if experiment_info and experiment_info.get("aggregate_name"):
                        self.cloudlab.delete_experiment(slice_name, experiment_info["aggregate_name"])
        except Exception as e:
            logger.error(f"Critical error in automatic provisioning check: {e}", exc_info=True)

    def _setup_sre_arena_and_finalize(self, experiment_info: dict):
        """
        Setup SRE Arena and finalize cluster state.
        """
        try:
            slice_name = experiment_info["slice_name"]
            login_info = experiment_info["login_info"]

            hosts = [info[2] for info in login_info]

            cfg = {
                "cloudlab": {
                    "ssh_user": DefaultSettings.PROVISIONER_DEFAULT_SSH_USERNAME,
                    "ssh_key": DefaultSettings.PROVISIONER_SSH_PRIVATE_KEY_PATH,
                    "nodes": hosts,
                },
                "pod_network_cidr": DefaultSettings.DEFAULT_POD_NETWORK_CIDR,
                "deploy_srearena": True,
                "deploy_key": DefaultSettings.DEPLOY_KEY_PATH,
            }

            setup_cloudlab_cluster_with_srearena(cfg)

            logger.info(f"SRE Arena setup for {slice_name} completed successfully.")

            self.state_manager.update_cluster_record(
                slice_name,
                status=CLUSTER_STATUS.STATUS_UNCLAIMED_READY,
                sre_arena_setup_status=SREARENA_STATUS.SRE_ARENA_SUCCESS,
            )
        except Exception as e:
            logger.error(f"Error during SRE Arena setup for {slice_name}: {e}", exc_info=True)
            self.state_manager.update_cluster_record(
                slice_name,
                status=CLUSTER_STATUS.STATUS_ERROR,
                sre_arena_setup_status=SREARENA_STATUS.SRE_ARENA_FAILED,
                last_error_message="SRE Arena setup failed",
            )
            raise e

    def check_unclaimed_cluster_timeout(self):
        logger.info("Running: Unclaimed Cluster Timeout Check")
        try:
            unclaimed_clusters = self.state_manager.get_clusters_by_status(CLUSTER_STATUS.STATUS_UNCLAIMED_READY)
            now = datetime.datetime.now()

            for cluster in unclaimed_clusters:
                slice_name = cluster["slice_name"]

                created_at = cluster["created_at"]

                if not isinstance(created_at, datetime.datetime):
                    created_at = datetime.datetime.fromisoformat(str(created_at))

                if now - created_at > datetime.timedelta(hours=DefaultSettings.UNCLAIMED_CLUSTER_TIMEOUT_HOURS):
                    logger.info(
                        f"Unclaimed cluster {slice_name} (in pool since {created_at}) has timed out. Marking for termination."
                    )
                    # Always mark for termination. Auto-provisioning will handle replenishment.
                    self.state_manager.update_cluster_record(slice_name, status=CLUSTER_STATUS.STATUS_TERMINATING)
                else:
                    logger.debug(
                        f"Unclaimed cluster {slice_name} (in pool since {created_at}) is within timeout window."
                    )
        except Exception as e:
            logger.error(f"Critical error in unclaimed cluster timeout check: {e}", exc_info=True)

    # The provisioner should extend the cluster daily until the user reliquishing timeout
    def check_claimed_cluster_extension(self):
        logger.info("Running: Claimed Cluster Extension Check")
        try:
            claimed_clusters = self.state_manager.get_clusters_by_status(CLUSTER_STATUS.STATUS_CLAIMED)
            now = datetime.datetime.now()
            for cluster in claimed_clusters:
                # Check if we need to extend based on last extension time
                last_extended_at = cluster.get("last_extended_at")
                if last_extended_at:
                    if not isinstance(last_extended_at, datetime.datetime):
                        last_extended_at = datetime.datetime.fromisoformat(str(last_extended_at))
                    # If last extension was less than 24 hours ago, skip
                    if now - last_extended_at < datetime.timedelta(
                        hours=DefaultSettings.CLAIMED_CLUSTER_EXTENSION_CHECK_HOURS
                    ):
                        continue

                logger.info(f"Performing daily extension for claimed cluster {cluster['slice_name']}.")
                new_duration_hours = DefaultSettings.CLAIMED_CLUSTER_DEFAULT_DURATION_HOURS
                try:
                    if self.cloudlab.renew_experiment(
                        cluster["slice_name"], new_duration_hours, cluster["aggregate_name"]
                    ):
                        new_cloudlab_expires_at = now + datetime.timedelta(hours=new_duration_hours)
                        self.state_manager.update_cluster_record(
                            cluster["slice_name"], cloudlab_expires_at=new_cloudlab_expires_at, last_extended_at=now
                        )
                        logger.info(f"Successfully extended {cluster['slice_name']} to {new_cloudlab_expires_at}.")

                    else:
                        logger.error(
                            f"Failed to extend claimed cluster {cluster['slice_name']}. User should be notified."
                        )

                        try:
                            email_sender = EmailSender()
                            if email_sender.is_email_set():
                                email_sender.send_cluster_extension_failure_notice(
                                    to_addresses=[cluster["claimed_by_user_id"]],
                                    cluster_name=cluster["slice_name"],
                                    error_message="Failed to extend cluster",
                                    current_expiry=cluster["cloudlab_expires_at"],
                                )
                        except Exception as e:
                            logger.error(f"Error sending cluster extension failure notice: {e}", exc_info=True)
                except Exception as e:
                    logger.error(f"Error extending claimed cluster {cluster['slice_name']}: {e}", exc_info=True)

                    try:
                        email_sender = EmailSender()
                        if email_sender.is_email_set():
                            email_sender.send_cluster_extension_failure_notice(
                                to_addresses=[cluster["claimed_by_user_id"]],
                                cluster_name=cluster["slice_name"],
                                error_message="Failed to extend cluster",
                                current_expiry=cluster["cloudlab_expires_at"],
                            )
                    except Exception as e:
                        logger.error(f"Error sending cluster extension failure notice: {e}", exc_info=True)
        except Exception as e:
            logger.error(f"Critical error in claimed cluster extension check: {e}", exc_info=True)

    def _get_key_fingerprint(self, key_path: str) -> str:
        result = subprocess.run(["ssh-keygen", "-lf", key_path], capture_output=True, text=True)
        output = result.stdout.strip()
        fingerprint = output.split()[1]  # Get the SHA256:xxxxxxxx part
        return fingerprint

    def _get_user_inactivity_duration(self, hostname: str) -> Optional[datetime.datetime]:
        logger.info(f"Attempting to get actual last SSH time for {hostname}.")
        try:
            provisioner_fingerprint = self._get_key_fingerprint(self.cloudlab.user_pubkeypath)

            ssh_manager = self._get_ssh_manager(hostname)

            # Command to get SSH activity from remote auth.log with sudo
            cmd = (
                "sudo cat /var/log/auth.log | grep sshd | grep 'Accepted publickey for' | awk '{print $1,$2,$3,$9,$16}'"
            )
            stdout, stderr, exit_code = ssh_manager.execute_ssh_command(cmd)

            if exit_code != 0 or not stdout:
                logger.warning(f"No SSH activity found for {hostname}. Exit code: {exit_code}, Error: {stderr}")
                return None

            # Parse the timestamps from the log entries
            provisioner_timestamps = []
            non_provisioner_timestamps = []

            for line in stdout.splitlines():
                try:
                    parts = line.split()
                    if len(parts) >= 3:
                        # Combine month, day, and time
                        timestamp_str = " ".join(parts[:3])
                        timestamp = datetime.datetime.strptime(timestamp_str, "%b %d %H:%M:%S")
                        # Add current year since log entries don't include it
                        timestamp = timestamp.replace(year=datetime.datetime.now().year)

                        # Check if this is a provisioner SSH
                        if provisioner_fingerprint in line:
                            provisioner_timestamps.append(timestamp)
                        else:
                            non_provisioner_timestamps.append(timestamp)

                except Exception as e:
                    logger.warning(f"Failed to parse timestamp from line: {line}, error: {e}")
                    continue

            # Since we just SSH'd in with provisioner key, the the latest provisioner time is the current time
            current_time = provisioner_timestamps[-1]

            if not provisioner_timestamps:
                logger.warning(f"No provisioner SSH activity found for {hostname}")
                return None

            # Case 1: If we have non-provisioner SSH activity
            if non_provisioner_timestamps:
                last_non_provisioner = max(non_provisioner_timestamps)
                time_diff = current_time - last_non_provisioner
                logger.info(f"Last non-provisioner SSH was {time_diff.total_seconds()/3600:.2f} hours ago")
                return time_diff

            # Case 2: If no non-provisioner SSH activity, use first provisioner time
            else:
                time_diff = current_time - provisioner_timestamps[0]
                logger.info(
                    f"No non-provisioner SSH found. First provisioner SSH was {time_diff.total_seconds()/3600:.2f} hours ago"
                )
                return time_diff

        except Exception as e:
            logger.error(f"Error getting SSH time for {hostname}: {e}", exc_info=True)
            return None

    def check_claimed_cluster_inactivity(self):
        logger.info("Running: Claimed Cluster Inactivity Check")
        try:
            claimed_clusters = self.state_manager.get_clusters_by_status(CLUSTER_STATUS.STATUS_CLAIMED)
            now = datetime.datetime.now()
            for cluster in claimed_clusters:
                slice_name = cluster["slice_name"]
                if cluster.get("evaluation_override") in (True, 1):
                    logger.debug(f"Cluster {slice_name} has evaluation override. Skipping inactivity check.")
                    continue

                # Get latest duration from all nodes
                node_durations = []
                for node in cluster["login_info"]:
                    hostname = node[2]
                    node_durations.append(self._get_user_inactivity_duration(hostname))

                # Get the latest duration
                user_inactivity_duration = min(node_durations)

                if user_inactivity_duration is None:
                    logger.warning(f"No user inactivity duration found for {slice_name}. Skipping inactivity check.")
                    continue

                self.state_manager.update_cluster_record(slice_name, last_activity_at=now - user_inactivity_duration)

                if user_inactivity_duration > datetime.timedelta(
                    hours=DefaultSettings.CLAIMED_CLUSTER_INACTIVITY_TIMEOUT_HOURS
                ):
                    logger.info(f"Claimed cluster {slice_name} inactive for {user_inactivity_duration}. Relinquishing.")
                    self.state_manager.update_cluster_record(
                        slice_name,
                        status=CLUSTER_STATUS.STATUS_TERMINATING,
                        claimed_by_user_id=None,
                        user_ssh_key_installed=False,
                    )

                    try:
                        email_sender = EmailSender()
                        if email_sender.is_email_set():
                            email_sender.send_inactive_cluster_deletion_notice(
                                to_addresses=[cluster["claimed_by_user_id"]],
                                cluster_name=cluster["slice_name"],
                                last_activity=now - user_inactivity_duration,
                            )
                            logger.info(
                                f"Sent inactive cluster deletion notice to {cluster['claimed_by_user_id']} for cluster {slice_name}"
                            )
                    except Exception as e:
                        logger.error(f"Error sending inactive cluster deletion notice: {e}", exc_info=True)
                else:
                    logger.debug(
                        f"Cluster {slice_name} last activity at {now - user_inactivity_duration} is within inactivity window."
                    )
        except Exception as e:
            logger.error(f"Critical error in claimed cluster inactivity check: {e}", exc_info=True)

    def process_terminating_clusters(self):
        logger.info("Running: Process Terminating Clusters")
        try:
            terminating_clusters = self.state_manager.get_clusters_by_status(CLUSTER_STATUS.STATUS_TERMINATING)

            for cluster in terminating_clusters:
                slice_name = cluster["slice_name"]
                aggregate_name = cluster["aggregate_name"]
                logger.info(f"Attempting to terminate cluster {slice_name} on {aggregate_name}.")
                try:
                    if not aggregate_name or aggregate_name == "<PENDING>":
                        logger.warning(
                            f"Cannot terminate {slice_name}, aggregate_name is unknown ('{aggregate_name}'). Deleting DB record only."
                        )
                        self.state_manager.delete_cluster_record(slice_name)
                        continue

                    if self.cloudlab.delete_experiment(slice_name, aggregate_name):
                        logger.info(f"Successfully deleted experiment {slice_name} from Cloudlab.")
                        self.state_manager.delete_cluster_record(slice_name)
                        logger.info(f"Removed cluster record for {slice_name}.")
                    else:
                        err_msg = f"Cloudlab API failed to delete {slice_name}. Will retry on next check."
                        logger.error(err_msg)
                        self.state_manager.update_cluster_record(
                            slice_name, last_error_message=err_msg, status=CLUSTER_STATUS.STATUS_TERMINATING
                        )
                except Exception as e:
                    err_msg = f"Error deleting {slice_name} from Cloudlab: {e}"
                    logger.error(err_msg + ". Will retry on next check.", exc_info=True)
                    self.state_manager.update_cluster_record(
                        slice_name, last_error_message=err_msg, status=CLUSTER_STATUS.STATUS_TERMINATING
                    )

        except Exception as e:
            logger.error(f"Critical error in processing terminating clusters: {e}", exc_info=True)

    def run_all_checks(self):
        """Runs all periodic checks in sequence."""
        if stop_event.is_set():
            logger.info("Stop event received by run_all_checks, skipping scheduled run.")
            return

        logger.info("======== Starting Periodic Checks Cycle ========")
        try:
            self.check_unclaimed_cluster_timeout()
            self.check_claimed_cluster_inactivity()
            self.check_automatic_provisioning()
            self.check_claimed_cluster_extension()
            self.process_terminating_clusters()
        except Exception as e:
            logger.critical(f"Unhandled exception during periodic checks cycle: {e}", exc_info=True)
        logger.info("======== Finished Periodic Checks Cycle ========")

    def start(self):
        logger.info("Starting Provisioner Daemon Scheduler...")
        # Run once immediately at start, then schedule
        try:
            logger.info("Performing initial run of all checks...")
            self.run_all_checks()
            logger.info("Initial run of checks complete.")
        except Exception as e:
            logger.critical(f"Initial run of checks failed critically: {e}", exc_info=True)

        # Schedule jobs
        self.scheduler.add_job(
            self.run_all_checks,
            trigger=IntervalTrigger(seconds=DefaultSettings.DEFAULT_SSH_TIME_OUT_SECONDS),
            id="provisioner_main_checks_job",
            name="Run all provisioner checks",
            replace_existing=True,
            misfire_grace_time=300,
            max_instances=1,
        )

        try:
            self.scheduler.start()
        except (KeyboardInterrupt, SystemExit):
            logger.info("Scheduler stopped by user/system.")
        finally:
            if self.scheduler.running:
                logger.info("Shutting down scheduler...")
                self.scheduler.shutdown(wait=True)
            logger.info("Provisioner Daemon scheduler shut down.")

    # --- Signal Handler and Main Execution ---
    _scheduler_instance = None

    def signal_handler(signum, frame):
        global _scheduler_instance
        logger.info(f"Signal {signal.Signals(signum).name} received, initiating graceful shutdown...")
        stop_event.set()
        if _scheduler_instance and _scheduler_instance.running:
            logger.info("Requesting scheduler shutdown...")
            _scheduler_instance.shutdown(wait=False)
        else:
            logger.info("Scheduler not running or not initialized for signal handler.")


if __name__ == "__main__":
    daemon = ProvisionerDaemon()
    daemon.start()
