import datetime
import logging
import os
import re
import time
from pathlib import Path

import click

from provisioner.cloudlab_provisioner import CloudlabProvisioner
from provisioner.config.settings import DefaultSettings
from provisioner.state_manager import CLUSTER_STATUS, SREGYM_STATUS, StateManager
from provisioner.utils.ssh import SSHManager, SSHUtilError
from scripts.geni_lib.cluster_setup import setup_cloudlab_cluster_with_sregym

logger = logging.getLogger(__name__)

_state_manager_instance: StateManager = None
_cloudlab_provisioner_instance: CloudlabProvisioner = None
EMAIL_REGEX = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"


def is_valid_email(email: str) -> bool:
    return re.match(EMAIL_REGEX, email) is not None


def get_state_manager() -> StateManager:
    global _state_manager_instance
    if _state_manager_instance is None:
        _state_manager_instance = StateManager(db_path=DefaultSettings.DATABASE_PATH)
    return _state_manager_instance


def get_cloudlab_provisioner() -> CloudlabProvisioner:
    global _cloudlab_provisioner_instance
    if _cloudlab_provisioner_instance is None:
        _cloudlab_provisioner_instance = CloudlabProvisioner()
    return _cloudlab_provisioner_instance


def _ensure_ssh_prerequisites():
    """Checks if necessary SSH configuration for the provisioner is present."""
    if not DefaultSettings.PROVISIONER_DEFAULT_SSH_USERNAME:
        click.echo(
            click.style("ERROR: PROVISIONER_DEFAULT_SSH_USERNAME is not correctly set in settings.py.", fg="red")
        )
        return False
    key_path = Path(os.path.expanduser(DefaultSettings.PROVISIONER_SSH_PRIVATE_KEY_PATH))
    if not key_path.exists():
        click.echo(
            click.style(
                f"ERROR: Provisioner's SSH private key not found at '{key_path}'. This is required for node operations.",
                fg="red",
            )
        )
        return False
    return True


def _get_ssh_manager(hostname: str) -> SSHManager:
    """Creates an SSHManager instance after ensuring prerequisites."""
    if not _ensure_ssh_prerequisites():
        raise click.Abort()  # Abort the current command
    return SSHManager(
        hostname=hostname,
        username=DefaultSettings.PROVISIONER_DEFAULT_SSH_USERNAME,
        private_key_path=DefaultSettings.PROVISIONER_SSH_PRIVATE_KEY_PATH,
    )


def _format_ssh_command(login_info_entry: list) -> str:
    """Formats an SSH command string from a login_info entry."""
    ssh_user = DefaultSettings.PROVISIONER_DEFAULT_SSH_USERNAME
    hostname = login_info_entry[2]
    port = login_info_entry[3]
    return f"ssh {ssh_user}@{hostname} -p {port}"


def _add_user_ssh_key_to_node(ssh_mgr: SSHManager, user_public_key: str, user_id_for_log: str) -> bool:
    """
    Safely adds a user's SSH public key to the authorized_keys file on a remote node.
    Returns True on success, False on failure.
    """
    hostname_for_log = ssh_mgr.hostname
    try:
        # Escape the public key for shell safety
        escaped_key = user_public_key.replace('"', '\\"')

        # Single command to create .ssh directory, add key, and set permissions
        cmd = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f'echo "{escaped_key}" >> ~/.ssh/authorized_keys && '
            "chmod 600 ~/.ssh/authorized_keys"
        )
        _, stderr, exit_code = ssh_mgr.execute_ssh_command(cmd)

        if exit_code != 0:
            click.echo(click.style(f"ERROR: Failed to setup SSH key on {hostname_for_log}: {stderr}", fg="red"))
            logger.error(f"SSH key setup failed on {hostname_for_log} for user {user_id_for_log}: {stderr}")
            return False

        logger.info(f"User SSH key for {user_id_for_log} added to {hostname_for_log}")
        return True

    except SSHUtilError as e:
        click.echo(click.style(f"ERROR: SSH operation failed on {hostname_for_log} while adding key: {e}", fg="red"))
        logger.error(f"SSHUtilError on {hostname_for_log} for user {user_id_for_log}: {e}")
        return False
    except Exception as e:
        click.echo(
            click.style(f"ERROR: Unexpected error during SSH key injection on {hostname_for_log}: {e}", fg="red")
        )
        logger.error(f"Unexpected error injecting key on {hostname_for_log} for {user_id_for_log}: {e}", exc_info=True)
        return False


def _remove_user_ssh_key_from_node(ssh_mgr: SSHManager, user_public_key: str, user_id_for_log: str) -> bool:
    hostname_for_log = ssh_mgr.hostname
    operation_id = datetime.datetime.now().strftime("%Y%m%d%H%M%S%f")
    logger.debug(
        f"Minimally attempting to remove SSH key for user {user_id_for_log} from {hostname_for_log} (OpID: {operation_id})."
    )

    user_public_key_cleaned = user_public_key.strip()
    if not user_public_key_cleaned:
        logger.error(f"Empty public key provided for {user_id_for_log}. Cannot remove.")
        return False

    try:
        # Escape the public key for shell safety
        escaped_key = user_public_key_cleaned.replace('"', '\\"')

        # Single command to remove the key and update permissions
        cmd = (
            f"if [ -f ~/.ssh/authorized_keys ]; then "
            f'grep -v -F -x "{escaped_key}" ~/.ssh/authorized_keys > ~/.ssh/authorized_keys.tmp && '
            f"mv ~/.ssh/authorized_keys.tmp ~/.ssh/authorized_keys && "
            f"chmod 600 ~/.ssh/authorized_keys; "
            f"else "
            f'echo "Authorized_keys file not found, key considered absent."; '
            f"fi"
        )

        stdout, stderr, exit_code = ssh_mgr.execute_ssh_command(cmd)

        if exit_code == 0:
            if "Authorized_keys file not found" in stdout:
                logger.info(
                    f"Authorized_keys file not found on {hostname_for_log} for user {user_id_for_log}. Key considered absent."
                )
            else:
                logger.info(
                    f"Successfully processed authorized_keys for key removal for {user_id_for_log} on {hostname_for_log}."
                )
            return True  # Success or file not found (key absent)
        else:
            logger.error(
                f"Failed to execute key removal command for {user_id_for_log} on {hostname_for_log}. "
                f"Exit code: {exit_code}, Stdout: '{stdout}', Stderr: '{stderr}'."
            )
            return False

    except SSHUtilError as e:
        logger.error(f"SSHUtilError during minimal key removal for {user_id_for_log} on {hostname_for_log}: {e}")
        return False
    except Exception as e:
        logger.error(
            f"Unexpected error during minimal key removal for {user_id_for_log} on {hostname_for_log}: {e}",
            exc_info=True,
        )
        return False


def _setup_sregym(cluster_info: dict) -> bool:
    """
    Setup SREGym on a newly provisioned cluster.
    Returns True on success, False on failure.
    """
    try:
        slice_name = cluster_info["slice_name"]
        login_info = cluster_info["login_info"]

        click.echo(click.style(f"Setting up SREGym on cluster {slice_name}...", fg="yellow"))
        hosts = [info[2] for info in login_info]

        cfg = {
            "cloudlab": {
                "ssh_user": DefaultSettings.PROVISIONER_DEFAULT_SSH_USERNAME,
                "ssh_key": DefaultSettings.PROVISIONER_SSH_PRIVATE_KEY_PATH,
                "nodes": hosts,
            },
            "pod_network_cidr": DefaultSettings.DEFAULT_POD_NETWORK_CIDR,
            "deploy_sregym": True,
            "deploy_key": DefaultSettings.DEPLOY_KEY_PATH,
        }

        setup_cloudlab_cluster_with_sregym(cfg)

        click.echo(click.style(f"SREGym setup completed successfully for {slice_name}.", fg="green"))
        return True
    except Exception as e:
        click.echo(click.style(f"Error setting up SREGym: {e}", fg="red"))
        logger.error(f"Error setting up SREGym for {slice_name}: {e}", exc_info=True)
        return False


# --- Click Command Group ---
@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose output for some operations.")
@click.pass_context
def cli(ctx, verbose):
    """Cloudlab Cluster Provisioner CLI."""
    ctx.ensure_object(dict)
    ctx.obj["VERBOSE"] = verbose
    if verbose:
        click.echo("Verbose mode enabled for CLI.")


# --- User Commands ---
@cli.command()
@click.option("--email", required=True, help="Your unique email address for registration.")
@click.option("--ssh-key", required=True, help="Your SSH public key.")
def register(email, ssh_key):
    """Registers a new user with their email and SSH public key."""
    sm = get_state_manager()
    if not is_valid_email(email):
        click.echo(click.style("ERROR: Invalid email address format.", fg="red"))
        return

    try:
        # Basic validation of key format
        if not (
            ssh_key.startswith("ssh-rsa ")
            or ssh_key.startswith("ssh-ed25519 ")
            or ssh_key.startswith("ecdsa-sha2-nistp")
        ):  # Note the space
            click.echo(
                click.style(
                    "ERROR: Invalid or incomplete SSH public key format. Ensure it includes the key type (e.g., 'ssh-rsa AAA...').",
                    fg="red",
                )
            )
            return
    except Exception as e:
        click.echo(click.style(f"ERROR: Could not read SSH key file: {e}", fg="red"))
        return

    if sm.add_user(email, ssh_key):
        click.echo(click.style(f"User with email '{email}' registered successfully.", fg="green"))
    else:
        click.echo(click.style(f"User with email '{email}' might already be registered", fg="yellow"))


@cli.command()
@click.option("--email", required=True, help="Your registered email address.")
@click.option("--eval-override", is_flag=True, help="Request evaluation override for longer inactivity timeout.")
@click.option("--deploy-sregym", is_flag=True, help="Deploy SREGym on the cluster.")
@click.pass_context
def claim(ctx, email, eval_override, deploy_sregym):
    """Claims an available cluster or requests a new one."""
    sm = get_state_manager()
    cp = get_cloudlab_provisioner()

    if not is_valid_email(email):
        click.echo(click.style("ERROR: Invalid email address format.", fg="red"))
        return

    user = sm.get_user(email)
    if not user:
        click.echo(click.style(f"ERROR: User with email '{email}' not registered. Please register first.", fg="red"))
        return

    user_claimed_count = sm.count_user_claimed_clusters(email)
    if user_claimed_count >= DefaultSettings.MAX_CLUSTERS_PER_USER:
        click.echo(
            click.style(
                f"ERROR: User '{email}' has already claimed the maximum of {DefaultSettings.MAX_CLUSTERS_PER_USER} clusters.",
                fg="red",
            )
        )
        return

    # 1. Try to get an existing unclaimed_ready cluster
    unclaimed_clusters = sm.get_clusters_by_status(CLUSTER_STATUS.STATUS_UNCLAIMED_READY)
    if unclaimed_clusters:
        cluster_to_claim = unclaimed_clusters[0]  # Simple: take the first one
        slice_name = cluster_to_claim["slice_name"]
        hostname = cluster_to_claim["control_node_hostname"]

        click.echo(f"Found available cluster: {slice_name}. Attempting to claim for '{email}'...")
        sm.update_cluster_record(slice_name, status=CLUSTER_STATUS.STATUS_USER_PROVISIONING, claimed_by_user_id=email)

        if not hostname:
            click.echo(
                click.style(
                    f"ERROR: Cluster {slice_name} has no control_node_hostname. Cannot proceed with claim.", fg="red"
                )
            )
            logger.error(f"Claim aborted for {slice_name}: missing control_node_hostname in DB.")
            return

        try:
            while not cp.are_nodes_ready(slice_name, cluster_to_claim["aggregate_name"]):
                click.echo(click.style(f"Waiting for nodes to be ready on {slice_name}...", fg="yellow"))
                time.sleep(10)
        except Exception as e:
            click.echo(click.style(f"ERROR: Failed to wait for nodes to be ready on {slice_name}: {e}", fg="red"))
            logger.error(f"Failed to wait for nodes to be ready on {slice_name}: {e}")
            sm.update_cluster_record(slice_name, status=CLUSTER_STATUS.STATUS_ERROR, last_error_message=str(e))
            return

        try:
            # add ssh public key to all nodes in the cluster
            for node_info in cluster_to_claim["login_info"]:
                node_hostname = node_info[2]
                ssh_mgr = _get_ssh_manager(node_hostname)
                logger.info(f"Adding user SSH key to node {node_hostname} for user {email}")
                if not _add_user_ssh_key_to_node(ssh_mgr, user["ssh_public_key"], email):
                    return
            user_ssh_key_installed_flag = True

        except (SSHUtilError, click.Abort) as e_ssh:  # Catch Abort from _get_ssh_manager
            click.echo(click.style(f"ERROR: SSH operation failed for new cluster {slice_name}: {e_ssh}", fg="red"))
            sm.update_cluster_record(
                slice_name,
                status=CLUSTER_STATUS.STATUS_ERROR,
                last_error_message=f"SSH key injection failed: {e_ssh}",
            )
            if cluster_to_claim.get("aggregate_name"):  # Attempt cleanup
                # Mark the experiment for termination
                logger.info(f"Marking experiment {slice_name} for termination")
                sm.update_cluster_record(slice_name, status=CLUSTER_STATUS.STATUS_TERMINATING)
            return

        # Extend Cloudlab duration
        now = datetime.datetime.now()
        new_duration_hours = DefaultSettings.CLAIMED_CLUSTER_DEFAULT_DURATION_HOURS
        new_cloudlab_expires_at = cluster_to_claim["cloudlab_expires_at"]
        try:
            if cp.renew_experiment(slice_name, new_duration_hours, cluster_to_claim["aggregate_name"]):
                new_cloudlab_expires_at = now + datetime.timedelta(hours=new_duration_hours)
                logger.info(f"Extended Cloudlab experiment {slice_name} to {new_cloudlab_expires_at}")
            else:
                click.echo(
                    click.style(
                        f"WARNING: Failed to extend Cloudlab duration for {slice_name}. It may expire sooner. Current expiry: {new_cloudlab_expires_at}",
                        fg="yellow",
                    )
                )
        except Exception as e:
            click.echo(
                click.style(
                    f"WARNING: Error extending Cloudlab duration for {slice_name}: {e}. Current expiry: {new_cloudlab_expires_at}",
                    fg="yellow",
                )
            )

        if deploy_sregym:
            click.echo("Setting up SREGym for your cluster. This may take several minutes...")
            experiment_info = {
                "slice_name": slice_name,
                "login_info": cluster_to_claim["login_info"],
            }
            setup_success = _setup_sregym(experiment_info)
            if setup_success:
                sm.update_cluster_record(
                    slice_name,
                    sregym_setup_status=SREGYM_STATUS.SREGYM_SUCCESS,
                )
                click.echo(click.style("SREGym successfully set up on your cluster!", fg="green"))
            else:
                sm.update_cluster_record(
                    slice_name,
                    sregym_setup_status=SREGYM_STATUS.SREGYM_FAILED,
                    last_error_message="SREGym setup failed",
                )
                click.echo(
                    click.style(
                        "SREGym setup failed. You may still use the cluster for basic operations.", fg="yellow"
                    )
                )

        # Update DB
        sm.update_cluster_record(
            slice_name,
            status=CLUSTER_STATUS.STATUS_CLAIMED,
            claimed_by_user_id=email,
            user_ssh_key_installed=user_ssh_key_installed_flag,
            cloudlab_expires_at=new_cloudlab_expires_at,
            evaluation_override=eval_override,
            claimed_at=now,
        )
        click.echo(click.style(f"Cluster '{slice_name}' successfully claimed by '{email}'.", fg="green"))
        click.echo("SSH Access (Control Node):")
        if cluster_to_claim.get("login_info"):
            for node_info in cluster_to_claim["login_info"]:
                click.echo(f"  {_format_ssh_command(node_info)}")
        elif hostname:  # Fallback if login_info is missing/malformed but hostname exists
            click.echo(f"  ssh {DefaultSettings.PROVISIONER_DEFAULT_SSH_USERNAME}@{hostname}")
        else:
            click.echo(click.style("  Could not determine SSH access details.", fg="yellow"))

    else:  # No UNCLAIMED_READY clusters, try to provision a new one for the user
        click.echo("No readily available clusters. Attempting to provision a new one for you...")

        current_total_managed = sm.count_total_managed_clusters()
        if current_total_managed >= DefaultSettings.MAX_TOTAL_CLUSTERS:
            click.echo(
                click.style(
                    f"ERROR: Maximum total clusters ({DefaultSettings.MAX_TOTAL_CLUSTERS}) reached. Cannot provision for user '{email}' at this time.",
                    fg="red",
                )
            )
            return

        slice_name = cp.generate_slice_name()
        click.echo(f"Requesting new cluster: {slice_name} (this may take several minutes)...")

        # Create DB record first, marking it as user-provisioning and pre-assigning to user
        sm.create_cluster_record(
            slice_name=slice_name,
            aggregate_name="<PENDING>",
            os_type=DefaultSettings.DEFAULT_OS_TYPE,
            node_count=DefaultSettings.DEFAULT_NODE_COUNT,
            status=CLUSTER_STATUS.STATUS_USER_PROVISIONING,
            claimed_by_user_id=email,  # Pre-claim
            evaluation_override=eval_override,
        )

        experiment_info = None
        try:
            user_provision_duration = DefaultSettings.CLAIMED_CLUSTER_DEFAULT_DURATION_HOURS
            experiment_info = cp.create_experiment(
                slice_name=slice_name,
                hardware_type=DefaultSettings.DEFAULT_HARDWARE_TYPE,
                os_type=DefaultSettings.DEFAULT_OS_TYPE,
                node_count=DefaultSettings.DEFAULT_NODE_COUNT,
                duration=user_provision_duration,
            )

            if not (experiment_info and experiment_info.get("login_info")):
                raise Exception("Cloudlab experiment creation failed or returned no login_info.")

            control_node_info = next((n for n in experiment_info["login_info"] if n[0] == "control"), None)
            if not control_node_info:
                raise ValueError("Control node info not found in login_info after user provisioning.")
            hostname = control_node_info[2]
            now = datetime.datetime.now()
            expires_at = now + datetime.timedelta(hours=experiment_info["duration"])

            try:
                while not cp.are_nodes_ready(slice_name, experiment_info["aggregate_name"]):
                    click.echo(click.style(f"Waiting for nodes to be ready on {slice_name}...", fg="yellow"))
                    time.sleep(10)
                logger.info(f"Nodes are ready for {slice_name}.")
            except Exception as e:
                click.echo(click.style(f"ERROR: Failed to wait for nodes to be ready on {slice_name}: {e}", fg="red"))
                logger.error(f"Failed to wait for nodes to be ready on {slice_name}: {e}")
                sm.update_cluster_record(slice_name, status=CLUSTER_STATUS.STATUS_ERROR, last_error_message=str(e))
                return

            try:
                # add ssh public key to all nodes in the cluster
                for node_info in experiment_info["login_info"]:
                    node_hostname = node_info[2]
                    ssh_mgr = _get_ssh_manager(node_hostname)
                    logger.info(f"Adding user SSH key to node {node_hostname} for user {email}")
                    if not _add_user_ssh_key_to_node(ssh_mgr, user["ssh_public_key"], email):
                        return
                user_ssh_key_installed_flag = True

            except (SSHUtilError, click.Abort) as e_ssh:  # Catch Abort from _get_ssh_manager
                click.echo(click.style(f"ERROR: SSH operation failed for new cluster {slice_name}: {e_ssh}", fg="red"))
                sm.update_cluster_record(
                    slice_name,
                    status=CLUSTER_STATUS.STATUS_ERROR,
                    last_error_message=f"SSH key injection failed: {e_ssh}",
                )
                if experiment_info.get("aggregate_name"):  # Attempt cleanup
                    # Mark the experiment for termination
                    logger.info(f"Marking experiment {slice_name} for termination")
                    sm.update_cluster_record(slice_name, status=CLUSTER_STATUS.STATUS_TERMINATING)
                return

            if deploy_sregym:
                click.echo("Setting up SREGym for your cluster. This may take several minutes...")
                setup_success = _setup_sregym(experiment_info)
                if setup_success:
                    sm.update_cluster_record(
                        slice_name,
                        sregym_setup_status=SREGYM_STATUS.SREGYM_SUCCESS,
                    )
                    click.echo(click.style("SREGym successfully set up on your cluster!", fg="green"))
                else:
                    sm.update_cluster_record(
                        slice_name,
                        sregym_setup_status=SREGYM_STATUS.SREGYM_FAILED,
                        last_error_message="SREGym setup failed",
                    )
                    click.echo(
                        click.style(
                            "SREGym setup failed. You may still use the cluster for basic operations.", fg="yellow"
                        )
                    )

            sm.update_cluster_record(
                slice_name,
                status=CLUSTER_STATUS.STATUS_CLAIMED,
                aggregate_name=experiment_info["aggregate_name"],
                hardware_type=experiment_info["hardware_type"],
                control_node_hostname=hostname,
                login_info=experiment_info["login_info"],
                user_ssh_key_installed=user_ssh_key_installed_flag,
                cloudlab_expires_at=expires_at,
                claimed_at=now,
            )
            click.echo(
                click.style(
                    f"New cluster '{slice_name}' successfully provisioned and claimed by '{email}'.", fg="green"
                )
            )
            click.echo("SSH Access:")
            if experiment_info.get("login_info"):
                for node_info in experiment_info["login_info"]:
                    click.echo(f"  {_format_ssh_command(node_info)}")
            elif hostname:
                click.echo(f"  ssh {DefaultSettings.PROVISIONER_DEFAULT_SSH_USERNAME}@{hostname}")

        except Exception as e:
            click.echo(
                click.style(
                    f"ERROR: An unexpected error occurred during user-triggered provisioning for {slice_name}: {e}",
                    fg="red",
                )
            )
            logger.error(f"User provision error for {slice_name}: {e}", exc_info=True)
            # Ensure status is ERROR if it was created in DB
            if sm.get_cluster_by_slice_name(slice_name):
                sm.update_cluster_record(slice_name, status=CLUSTER_STATUS.STATUS_ERROR, last_error_message=str(e))
            # Attempt to delete from Cloudlab if experiment_info was partially obtained
            if experiment_info and experiment_info.get("aggregate_name"):
                logger.info(
                    f"Attempting to cleanup partially provisioned Cloudlab experiment {slice_name} (user-triggered)"
                )
                # Mark the experiment for termination
                sm.update_cluster_record(slice_name, status=CLUSTER_STATUS.STATUS_TERMINATING)


@cli.command(name="list")
@click.option("--email", help="List clusters claimed by this email. If not provided, lists unclaimed ready clusters.")
@click.pass_context
def list_clusters(ctx, email):
    """Lists clusters. Shows unclaimed ready, or user's claimed clusters."""
    sm = get_state_manager()
    verbose = ctx.obj.get("VERBOSE", False)

    if email:
        if not is_valid_email(email):
            click.echo(click.style("ERROR: Invalid email address format.", fg="red"))
            return
        user = sm.get_user(email)
        if not user:
            click.echo(click.style(f"ERROR: User with email '{email}' not registered.", fg="red"))
            return
        clusters = sm.get_claimed_clusters_by_user(email)
        if not clusters:
            click.echo(f"User '{email}' has no claimed clusters.")
            return
        click.echo(f"Clusters claimed by '{email}':")
    else:
        clusters = sm.get_clusters_by_status(CLUSTER_STATUS.STATUS_UNCLAIMED_READY)
        if not clusters:
            click.echo("No unclaimed ready clusters available.")
            return
        click.echo("Unclaimed Ready Clusters:")

    for cluster in clusters:
        click.echo(f"  Slice: {cluster['slice_name']} (Status: {cluster['status']})")
        if verbose or email:  # Show more details if verbose or listing user's clusters
            if cluster.get("control_node_hostname"):
                click.echo(f"    Control Node: {cluster['control_node_hostname']}")
            if cluster.get("cloudlab_expires_at"):
                expires_at_str = (
                    cluster["cloudlab_expires_at"].strftime("%Y-%m-%d %H:%M:%S %Z")
                    if isinstance(cluster["cloudlab_expires_at"], datetime.datetime)
                    and cluster["cloudlab_expires_at"].tzinfo
                    else str(cluster["cloudlab_expires_at"])
                )
                click.echo(f"    Cloudlab Expires: {expires_at_str}")
            if cluster.get("login_info") and isinstance(cluster.get("login_info"), list):
                for node_info in cluster["login_info"]:
                    if node_info[0] == "control":
                        click.echo(f"    SSH: {_format_ssh_command(node_info)}")
        if verbose:  # Even more details for verbose mode
            click.echo(f"    Aggregate: {cluster.get('aggregate_name')}")
            click.echo(f"    Hardware: {cluster.get('hardware_type')}")
            click.echo(f"    Claimed by: {cluster.get('claimed_by_user_id', 'N/A')}")
            click.echo(f"    SREGym: {cluster.get('sregym_setup_status', 'N/A')}")


@cli.command()
@click.option("--email", required=True, help="Your registered email address.")
@click.option("--experiment", required=True, help="The name of the experiment to relinquish.")
def relinquish(email, experiment):
    """Relinquishes a claimed cluster, marking it for termination."""
    try:
        sm = get_state_manager()
        if not is_valid_email(email):
            click.echo(click.style("ERROR: Invalid email address format.", fg="red"))
            return

        user = sm.get_user(email)
        if not user:
            click.echo(click.style(f"ERROR: User with email '{email}' not registered.", fg="red"))
            return

        cluster = sm.get_cluster_by_slice_name(experiment)
        if not cluster:
            click.echo(click.style(f"ERROR: Cluster '{experiment}' not found.", fg="red"))
            return

        if cluster["claimed_by_user_id"] != email or cluster["status"] != CLUSTER_STATUS.STATUS_CLAIMED:
            click.echo(
                click.style(f"ERROR: Cluster '{experiment}' is not currently claimed by user '{email}'.", fg="red")
            )
            return

        sm.update_cluster_record(
            experiment,
            status=CLUSTER_STATUS.STATUS_TERMINATING,
            claimed_by_user_id=None,  # Disassociate user
            user_ssh_key_installed=False,
        )
        click.echo(
            click.style(f"Cluster '{experiment}' relinquished by '{email}' and marked for termination.", fg="green")
        )
        logger.info(f"User {email} relinquished cluster {experiment}. Marked for termination.")
    except Exception as e:
        click.echo(click.style(f"ERROR: Failed to update cluster '{experiment}' status to terminating: {e}", fg="red"))
        logger.error(f"Failed to update cluster '{experiment}' status to terminating: {e}")


@cli.command()
@click.option("--experiment", required=True, help="The name of the experiment to get status for.")
def status(experiment):
    """Shows detailed status of a specific cluster."""
    sm = get_state_manager()
    cluster = sm.get_cluster_by_slice_name(experiment)
    if not cluster:
        click.echo(click.style(f"ERROR: Cluster '{experiment}' not found.", fg="red"))
        return

    click.echo(f"Status for Experiment: {click.style(cluster['slice_name'], bold=True)}")
    for key, value in sorted(cluster.items()):  # Sort for consistent output
        if key == "id":
            continue  # Skip internal DB id

        display_key = key.replace("_", " ").title()
        display_value = value

        if isinstance(value, datetime.datetime):
            display_value = value.strftime("%Y-%m-%d %H:%M:%S %Z") if value.tzinfo else value.isoformat()
        elif key == "login_info" and isinstance(value, list):
            click.echo(f"  {display_key}:")
            for node_entry in value:
                # node_entry is [client_id, user_on_node, hostname, port]
                if node_entry[0] == "control":
                    click.echo(f"    - Control Node SSH: {_format_ssh_command(node_entry)}")
                else:
                    click.echo(f"    - {node_entry[0]}: {node_entry[2]}:{node_entry[3]}")  # client_id: hostname:port
            continue  # Skip default print for login_info
        elif value is None:
            display_value = click.style("N/A", dim=True)

        click.echo(f"  {display_key + ':':<30} {display_value}")


# --- Main Execution ---
if __name__ == "__main__":
    cli(obj={})
