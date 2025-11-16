import os
from pathlib import Path

HOME_DIR = Path(os.path.expanduser("~"))
BASE_DIR = Path(__file__).resolve().parent
BASE_PARENT_DIR = Path(__file__).resolve().parent.parent

# Targe microservice and its utilities directories
TARGET_MICROSERVICES = BASE_PARENT_DIR / "SREGym-applications"

# Cache directories
CACHE_DIR = HOME_DIR / "cache_dir"
LLM_CACHE_FILE = CACHE_DIR / "llm_cache.json"

# Fault scripts
FAULT_SCRIPTS = BASE_DIR / "generators" / "fault" / "script"

# Metadata files
SOCIAL_NETWORK_METADATA = BASE_DIR / "service" / "metadata" / "social-network.json"
HOTEL_RES_METADATA = BASE_DIR / "service" / "metadata" / "hotel-reservation.json"
PROMETHEUS_METADATA = BASE_DIR / "service" / "metadata" / "prometheus.json"
TRAIN_TICKET_METADATA = BASE_DIR / "service" / "metadata" / "train-ticket.json"
ASTRONOMY_SHOP_METADATA = BASE_DIR / "service" / "metadata" / "astronomy-shop.json"
TIDB_METADATA = BASE_DIR / "service" / "metadata" / "tidb-with-operator.json"
FLIGHT_TICKET_METADATA = BASE_DIR / "service" / "metadata" / "flight-ticket.json"
FLEET_CAST_METADATA = BASE_DIR / "service" / "metadata" / "fleet-cast.json"
BLUEPRINT_HOTEL_RES_METADATA = BASE_DIR / "service" / "metadata" / "blueprint-hotel-reservation.json"
COCKROACH_DB_CLUSTER_METADATA = BASE_DIR / "service" / "metadata" / "cockroachdb-application.json"

# CockroachDB Deploy benchmark resources
COCKROACH_DB_DEPLOY_RESOURCES = BASE_DIR / "resources" / "cockroachdb-deploy"

# CockroachDB Initialize benchmark resources
COCKROACH_DB_INITIALIZE_RESOURCES = BASE_DIR / "resources" / "cockroachdb-initialize"

# CockroachDB Decommission benchmark resources
COCKROACH_DB_DECOMMISSION_RESOURCES = BASE_DIR / "resources" / "cockroachdb-decommission"

# CockroachDB ResizePVC benchmark resources
COCKROACH_DB_RESIZE_PVC_RESOURCES = BASE_DIR / "resources" / "cockroachdb-resize-pvc"

# CockroachDB Partitioned Update benchmark resources
COCKROACH_DB_PARTITIONED_UPDATE_RESOURCES = BASE_DIR / "resources" / "cockroachdb-partitioned-update"

# CockroachDB Cluster Settings benchmark resources
COCKROACH_DB_CLUSTER_SETTINGS_RESOURCES = BASE_DIR / "resources" / "cockroachdb-cluster-settings"

# CockroachDB Version Check benchmark resources
COCKROACH_DB_VERSION_CHECK_RESOURCES = BASE_DIR / "resources" / "cockroachdb-version-check"

# CockroachDB Zone Config benchmark resources
COCKROACH_DB_ZONE_CONFIG_RESOURCES = BASE_DIR / "resources" / "cockroachdb-zone-config"

# CockroachDB Expose Ingress benchmark resources
COCKROACH_DB_EXPOSE_INGRESS_RESOURCES = BASE_DIR / "resources" / "cockroachdb-expose-ingress"

# CockroachDB Health Check Recovery benchmark resources
COCKROACH_DB_HEALTH_CHECK_RECOVERY_RESOURCES = BASE_DIR / "resources" / "cockroachdb-health-check-recovery"

# CockroachDB Backup Restore benchmark resources
COCKROACH_DB_BACKUP_RESTORE_RESOURCES = BASE_DIR / "resources" / "cockroachdb-backup-restore"

# CockroachDB Certificate Rotation benchmark resources
COCKROACH_DB_CERTIFICATE_ROTATION_RESOURCES = BASE_DIR / "resources" / "cockroachdb-certificate-rotation"

# CockroachDB Generate Cert benchmark resources
COCKROACH_DB_GENERATE_CERT_RESOURCES = BASE_DIR / "resources" / "cockroachdb-generate-cert"

# CockroachDB Major Upgrade Finalize benchmark resources
COCKROACH_DB_MAJOR_UPGRADE_FINALIZE_RESOURCES = BASE_DIR / "resources" / "cockroachdb-major-upgrade-finalize"

# CockroachDB Monitoring Integration benchmark resources
COCKROACH_DB_MONITORING_INTEGRATION_RESOURCES = BASE_DIR / "resources" / "cockroachdb-monitoring-integration"

# CockroachDB Multi-Region Setup benchmark resources
COCKROACH_DB_MULTI_REGION_SETUP_RESOURCES = BASE_DIR / "resources" / "cockroachdb-multi-region-setup"

# CockroachDB Node Drain Maintenance benchmark resources
COCKROACH_DB_NODE_DRAIN_MAINTENANCE_RESOURCES = BASE_DIR / "resources" / "cockroachdb-node-drain-maintenance"

# CockroachDB Quorum Loss Recovery benchmark resources
COCKROACH_DB_QUORUM_LOSS_RECOVERY_RESOURCES = BASE_DIR / "resources" / "cockroachdb-quorum-loss-recovery"

# Khaos DaemonSet
KHAOS_DS = BASE_DIR / "service" / "khaos.yaml"
