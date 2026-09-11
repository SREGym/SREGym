// One build graph for local development and native multi-architecture CI.
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
  targets = ["hotel-reservation", "locust-exporter", "wrk2", "social-network", "openresty-thrift", "media-frontend"]
}

group "publish" {
  targets = ["hotel-reservation", "locust-exporter", "wrk2", "social-network-deps", "social-network", "openresty-thrift", "media-frontend"]
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
