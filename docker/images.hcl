// Local build definitions for SREGym's AMD64 and ARM64 images.
variable "REGISTRY" {
  default = "ghcr.io/sregym"
}

variable "IMAGE_TAG" {
  default = "local"
}

variable "REVISION" {
  default = "development"
}

group "default" {
  targets = [
    "hotel-reservation",
    "locust-exporter",
    "wrk2",
    "social-network",
    "openresty-thrift",
    "media-frontend",
    "fleetcast-backend",
    "flight-ticket-action-deployer",
    "flight-ticket-populate-redis",
    "flight-ticket-load-generator",
    "flight-ticket-python-runtime",
    "train-ticket-percona",
    "train-ticket-xenon",
    "train-ticket-nacos",
    "train-ticket-mysqlclient",
    "train-ticket-mysqld-exporter",
    "train-ticket-alertsnitch-mysql",
    "train-ticket-deploy",
    "hotel-reservation-1",
    "hotel-reservation-2",
    "stress",
  ]
}

group "publish" {
  targets = [
    "hotel-reservation",
    "locust-exporter",
    "wrk2",
    "social-network-deps",
    "social-network",
    "openresty-thrift",
    "media-frontend",
    "kind-node",
    "agent-base",
    "fleetcast-backend",
    "flight-ticket-action-deployer",
    "flight-ticket-populate-redis",
    "flight-ticket-load-generator",
    "flight-ticket-python-runtime",
    "train-ticket-percona",
    "train-ticket-xenon",
    "train-ticket-nacos",
    "train-ticket-mysqlclient",
    "train-ticket-mysqld-exporter",
    "train-ticket-alertsnitch-mysql",
    "train-ticket-deploy",
    "hotel-reservation-1",
    "hotel-reservation-2",
    "stress",
  ]
}

group "flight-ticket" {
  targets = [
    "flight-ticket-action-deployer",
    "flight-ticket-populate-redis",
    "flight-ticket-load-generator",
    "flight-ticket-python-runtime",
  ]
}

group "train-ticket" {
  targets = [
    "train-ticket-percona",
    "train-ticket-xenon",
    "train-ticket-nacos",
    "train-ticket-mysqlclient",
    "train-ticket-mysqld-exporter",
    "train-ticket-alertsnitch-mysql",
    "train-ticket-deploy",
  ]
}

target "_common" {
  platforms = ["linux/amd64", "linux/arm64"]
  labels = {
    "org.opencontainers.image.source" = "https://github.com/SREGym/SREGym"
    "org.opencontainers.image.revision" = REVISION
  }
}

target "hotel-reservation" {
  inherits = ["_common"]
  context = "SREGym-applications/hotelReservation"
  tags = ["${REGISTRY}/hotel-reservation:${IMAGE_TAG}"]
}

target "locust-exporter" {
  inherits = ["_common"]
  context = "docker/locust-exporter"
  tags = ["${REGISTRY}/locust-exporter:${IMAGE_TAG}"]
}

target "wrk2" {
  inherits = ["_common"]
  context = "docker/wrk2"
  tags = ["${REGISTRY}/wrk2:${IMAGE_TAG}"]
}

target "social-network-deps" {
  inherits = ["_common"]
  context = "SREGym-applications/socialNetwork"
  dockerfile = "docker/thrift-microservice-deps/cpp/Dockerfile"
  tags = ["${REGISTRY}/social-network-deps:${IMAGE_TAG}"]
}

target "social-network" {
  inherits = ["_common"]
  context = "SREGym-applications/socialNetwork"
  contexts = { social-deps = "target:social-network-deps" }
  args = { SOCIAL_NETWORK_BASE_IMAGE = "social-deps" }
  tags = ["${REGISTRY}/social-network:${IMAGE_TAG}"]
}

target "openresty-thrift" {
  inherits = ["_common"]
  context = "SREGym-applications/socialNetwork/docker/openresty-thrift"
  dockerfile = "xenial/Dockerfile"
  tags = ["${REGISTRY}/openresty-thrift:${IMAGE_TAG}"]
}

target "media-frontend" {
  inherits = ["_common"]
  context = "SREGym-applications/socialNetwork/docker/media-frontend"
  dockerfile = "xenial/Dockerfile"
  tags = ["${REGISTRY}/media-frontend:${IMAGE_TAG}"]
}

target "kind-node" {
  inherits = ["_common"]
  context = "kind"
  tags = ["${REGISTRY}/kind-node:${IMAGE_TAG}"]
}

target "agent-base" {
  inherits = ["_common"]
  context = "."
  dockerfile = "docker/agents/Dockerfile"
  // Match the Kubernetes version used by the bundled KIND node image.
  args = { KUBECTL_VERSION = "v1.32.1" }
  tags = ["${REGISTRY}/agent-base:${IMAGE_TAG}"]
}

target "fleetcast-backend" {
  inherits = ["_common"]
  context = "SREGym-applications/FleetCast/backend"
  dockerfile = "../../../docker/fleetcast/Dockerfile"
  contexts = { python-dependencies = "./docker/fleetcast" }
  tags = ["${REGISTRY}/fleetcast-backend:${IMAGE_TAG}"]
}

target "flight-ticket-action-deployer" {
  inherits = ["_common"]
  context = "SREGym-applications/flight-ticket/deploy_ow_actions"
  dockerfile = "../../../docker/flight-ticket/action-deployer.Dockerfile"
  tags = ["${REGISTRY}/flight-ticket-action-deployer:${IMAGE_TAG}"]
}

target "flight-ticket-populate-redis" {
  inherits = ["_common"]
  context = "SREGym-applications/flight-ticket/populate_redis"
  tags = ["${REGISTRY}/flight-ticket-populate-redis:${IMAGE_TAG}"]
}

target "flight-ticket-load-generator" {
  inherits = ["_common"]
  context = "SREGym-applications/flight-ticket/load_generator"
  dockerfile = "../../../docker/flight-ticket/load-generator.Dockerfile"
  tags = ["${REGISTRY}/flight-ticket-load-generator:${IMAGE_TAG}"]
}

target "train-ticket-xenon" {
  inherits = ["_common"]
  context = "docker/train-ticket"
  dockerfile = "xenon.Dockerfile"
  tags = ["${REGISTRY}/train-ticket-xenon:${IMAGE_TAG}"]
}

target "train-ticket-percona" {
  inherits = ["_common"]
  context = "docker/train-ticket"
  dockerfile = "percona.Dockerfile"
  tags = ["${REGISTRY}/train-ticket-percona:${IMAGE_TAG}"]
}

target "train-ticket-mysqlclient" {
  inherits = ["_common"]
  context = "docker/train-ticket"
  dockerfile = "mysqlclient.Dockerfile"
  tags = ["${REGISTRY}/train-ticket-mysqlclient:${IMAGE_TAG}"]
}

target "train-ticket-nacos" {
  inherits = ["_common"]
  context = "docker/train-ticket"
  dockerfile = "nacos.Dockerfile"
  tags = ["${REGISTRY}/train-ticket-nacos:${IMAGE_TAG}"]
}

target "train-ticket-alertsnitch-mysql" {
  inherits = ["_common"]
  context = "docker/train-ticket"
  dockerfile = "alertsnitch-mysql.Dockerfile"
  tags = ["${REGISTRY}/train-ticket-alertsnitch-mysql:${IMAGE_TAG}"]
}

target "train-ticket-mysqld-exporter" {
  inherits = ["_common"]
  context = "docker/train-ticket"
  dockerfile = "mysqld-exporter.Dockerfile"
  tags = ["${REGISTRY}/train-ticket-mysqld-exporter:${IMAGE_TAG}"]
}

target "flight-ticket-python-runtime" {
  inherits = ["_common"]
  context = "docker/flight-ticket"
  dockerfile = "python-runtime.Dockerfile"
  tags = ["${REGISTRY}/flight-ticket-python-runtime:${IMAGE_TAG}"]
}

target "train-ticket-deploy" {
  inherits = ["_common"]
  context = "docker/train-ticket"
  dockerfile = "deployer.Dockerfile"
  contexts = { image-releases = "./docker" }
  tags = ["${REGISTRY}/train-ticket-deploy:${IMAGE_TAG}"]
}

target "hotel-reservation-1" {
  inherits = ["_common"]
  context = "docker/hotel-reservation"
  dockerfile = "release1.Dockerfile"
  tags = ["${REGISTRY}/hotel-reservation:${IMAGE_TAG}.1"]
}

target "hotel-reservation-2" {
  inherits = ["_common"]
  context = "docker/hotel-reservation"
  dockerfile = "release2.Dockerfile"
  tags = ["${REGISTRY}/hotel-reservation:${IMAGE_TAG}.2"]
}

target "stress" {
  inherits = ["_common"]
  context = "docker/stress"
  tags = ["${REGISTRY}/stress:${IMAGE_TAG}"]
}
