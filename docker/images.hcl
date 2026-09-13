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
    "kube-proxy-1",
    "blueprint-hotel",
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
    "kube-proxy-1",
    "blueprint-hotel",
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
  tags = ["${REGISTRY}/fleetcast-backend:${IMAGE_TAG}"]
}

target "flight-ticket-action-deployer" {
  inherits = ["_common"]
  context = "SREGym-applications/flight-ticket/deploy_ow_actions"
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

target "kube-proxy-1" {
  inherits = ["_common"]
  context = "docker/kube-proxy"
  dockerfile = "release1.Dockerfile"
  tags = ["${REGISTRY}/kube-proxy:${IMAGE_TAG}.1"]
}

group "blueprint-hotel" {
  targets = [
    "blueprint-hotel-frontend",
    "blueprint-hotel-geo",
    "blueprint-hotel-profile",
    "blueprint-hotel-rate",
    "blueprint-hotel-recomd",
    "blueprint-hotel-reserv",
    "blueprint-hotel-search",
    "blueprint-hotel-user",
    "blueprint-hotel-workload",
  ]
}

target "_blueprint-hotel-service" {
  inherits = ["_common"]
  context = "docker/blueprint-hotel"
  target = "service"
}

target "blueprint-hotel-frontend" {
  inherits = ["_blueprint-hotel-service"]
  args = {
    SERVICE = "frontend"
    ORIGINAL_IMAGE = "777lefty/docker-frontend-service-container@sha256:f8f8f2345c60bb2a4af74970a5fa09503d19fcf83cf277fb3aa22c7bb72a5487"
  }
  tags = ["${REGISTRY}/blueprint-hotel:${IMAGE_TAG}-frontend"]
}

target "blueprint-hotel-geo" {
  inherits = ["_blueprint-hotel-service"]
  args = {
    SERVICE = "geo"
    ORIGINAL_IMAGE = "777lefty/docker-geo-service-container@sha256:baabf51dcae4a2f92a7c3b6fd8deff0fe067f59ecdfe76f892da19e8b95a5e1c"
  }
  tags = ["${REGISTRY}/blueprint-hotel:${IMAGE_TAG}-geo"]
}

target "blueprint-hotel-profile" {
  inherits = ["_blueprint-hotel-service"]
  args = {
    SERVICE = "profile"
    ORIGINAL_IMAGE = "777lefty/docker-profile-service-container@sha256:360ca11f17db4487e396b2e25f711d890501c2209666e1ced2096229e45f1184"
  }
  tags = ["${REGISTRY}/blueprint-hotel:${IMAGE_TAG}-profile"]
}

target "blueprint-hotel-rate" {
  inherits = ["_blueprint-hotel-service"]
  args = {
    SERVICE = "rate"
    ORIGINAL_IMAGE = "777lefty/docker-rate-service-container@sha256:4bd401cbc2ddcd4b32daa6cfe5af4777aa14d8135652a3888546a273e4b128f4"
  }
  tags = ["${REGISTRY}/blueprint-hotel:${IMAGE_TAG}-rate"]
}

target "blueprint-hotel-recomd" {
  inherits = ["_blueprint-hotel-service"]
  args = {
    SERVICE = "recomd"
    ORIGINAL_IMAGE = "777lefty/docker-recomd-service-container@sha256:834b68108041da4a2ed65b19a13e043f372a9c83505127d3b893a661f0287f93"
  }
  tags = ["${REGISTRY}/blueprint-hotel:${IMAGE_TAG}-recomd"]
}

target "blueprint-hotel-reserv" {
  inherits = ["_blueprint-hotel-service"]
  args = {
    SERVICE = "reserv"
    ORIGINAL_IMAGE = "777lefty/docker-reserv-service-container@sha256:f40c8aecd6ac89cb440699a8964f5e488db57855d0cd7a17e616f220775296df"
  }
  tags = ["${REGISTRY}/blueprint-hotel:${IMAGE_TAG}-reserv"]
}

target "blueprint-hotel-search" {
  inherits = ["_blueprint-hotel-service"]
  args = {
    SERVICE = "search"
    ORIGINAL_IMAGE = "777lefty/docker-search-service-container@sha256:7f8fc1950a72f442c8e743bafa226ad81ad6180bf645fa2f35643aa0db9f327c"
  }
  tags = ["${REGISTRY}/blueprint-hotel:${IMAGE_TAG}-search"]
}

target "blueprint-hotel-user" {
  inherits = ["_blueprint-hotel-service"]
  args = {
    SERVICE = "user"
    ORIGINAL_IMAGE = "777lefty/docker-user-service-container@sha256:051f0d5c94226173a0834706651ab48b7c5e7ae5f3bd5302fb81b1589c604b9a"
  }
  tags = ["${REGISTRY}/blueprint-hotel:${IMAGE_TAG}-user"]
}

target "blueprint-hotel-workload" {
  inherits = ["_common"]
  context = "docker/blueprint-hotel"
  target = "workload"
  args = {
    ORIGINAL_IMAGE = "777lefty/wlgen-proc@sha256:2fc11305b6aa148893965c675952a1033c54ef65f56147e1c65835a4e125826c"
  }
  tags = ["${REGISTRY}/blueprint-hotel:${IMAGE_TAG}-workload"]
}
