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
    "mattermost-server",
    "stripe-backend",
    "incident-arena",
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
    "openwhisk",
  ]
}

group "publish" {
  targets = [
    "mattermost-server",
    "stripe-backend",
    "incident-arena",
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
    "openwhisk",
  ]
}

group "flight-ticket" {
  targets = [
    "flight-ticket-action-deployer",
    "flight-ticket-populate-redis",
    "flight-ticket-load-generator",
    "flight-ticket-python-runtime",
    "openwhisk",
  ]
}

group "openwhisk" {
  targets = [
    "openwhisk-controller", "openwhisk-invoker", "openwhisk-utility",
    "openwhisk-zookeeper", "openwhisk-apigateway", "openwhisk-alarmprovider",
    "openwhisk-kafkaprovider", "openwhisk-nodejs14", "openwhisk-python37",
  ]
}

target "_openwhisk" {
  inherits = ["_common"]
  context = "docker/openwhisk"
}

target "openwhisk-controller" {
  inherits = ["_openwhisk"]
  dockerfile = "java.Dockerfile"
  tags = ["${REGISTRY}/openwhisk:${IMAGE_TAG}-controller"]
}

target "openwhisk-invoker" {
  inherits = ["_openwhisk"]
  dockerfile = "java.Dockerfile"
  args = {
    COMPONENT = "invoker"
    UPSTREAM_IMAGE = "openwhisk/invoker@sha256:535ae356d136036743a8e74a3662386d8c8a3c152be626a167fcac9b5647c01f"
  }
  tags = ["${REGISTRY}/openwhisk:${IMAGE_TAG}-invoker"]
}

target "openwhisk-components" {
  inherits = ["_openwhisk"]
  name = "openwhisk-${component}"
  matrix = {
    component = ["utility", "zookeeper", "apigateway", "alarmprovider", "kafkaprovider", "nodejs14", "python37"]
  }
  dockerfile = "${component}.Dockerfile"
  tags = ["${REGISTRY}/openwhisk:${IMAGE_TAG}-${component}"]
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

group "postmortem-apps" {
  targets = ["mattermost-server", "stripe-backend"]
}

group "incident-arena" {
  targets = [
    "frappe-toolbox", "frappe-loadgen", "frappe-admin",
    "frappe-toolbox-legacy", "frappe-loadgen-legacy", "frappe-admin-legacy",
    "saleor-toolbox", "saleor-loadgen", "saleor-server",
    "slack-spine-app", "slack-spine-app-v2", "slack-spine-app-v3",
    "slack-spine-builder", "slack-spine-go", "slack-spine-python",
    "slack-spine-toolbox", "slack-spine-loadgen", "slack-spine-pgbouncer",
    "slack-spine-minio",
  ]
}

target "frappe-images" {
  inherits = ["_common"]
  name = "frappe-${image.name}"
  matrix = {
    image = [
      { name = "toolbox", component = "toolbox", tag = "toolbox-v2", digest = "d40b49eb1c050f14d4fa91ad5bb894ee1db61724466caea903ee0a952574210e", repository = "frappe-main" },
      { name = "loadgen", component = "loadgen", tag = "loadgen-v2", digest = "0372494536eb9a0b40745cdc92a1915ec5b144a67f8bc509845eba5e411b310c", repository = "frappe-loadgen" },
      { name = "admin", component = "admin", tag = "admin-v2", digest = "9975944f1d375dea6afb9c35159f978c4d82691f232e4818ff7faff66121cd2d", repository = "frappe-admin-sidecar" },
      { name = "toolbox-legacy", component = "toolbox", tag = "toolbox-v1", digest = "6204c277489b115057dbe2bb3465f7ba95327c3928e1be9076d92e960d94c9a7", repository = "frappe-main" },
      { name = "loadgen-legacy", component = "loadgen", tag = "loadgen-v1", digest = "9229609b8adf60157608f47abbba82b96a31efc26044ec41c793d1db8b1c6c87", repository = "frappe-loadgen" },
      { name = "admin-legacy", component = "admin", tag = "admin-v1", digest = "8682d96965e833d5d95814b612a67b8b206f9b6fdb694af40a975f9599e9dea6", repository = "frappe-admin-sidecar" },
    ]
  }
  context = "docker/apps/frappe/${image.component}"
  args = { UPSTREAM_IMAGE = "ghcr.io/abundant-ai/sre-world/${image.repository}@sha256:${image.digest}" }
  tags = ["${REGISTRY}/frappe:${IMAGE_TAG}-${image.tag}"]
}

target "saleor-images" {
  inherits = ["_common"]
  name = "saleor-${image.name}"
  matrix = {
    image = [
      { name = "toolbox", digest = "ece6147d06e9f60750b33827302a063cebfa4112e1a37f002dd778b441c09ba5", repository = "saleor-main" },
      { name = "loadgen", digest = "cdee1728463bbaa0c2665a809bb39b5c7919a48a4575fd629b3c4f6a857f812e", repository = "saleor-loadgen" },
    ]
  }
  context = "docker/apps/saleor/${image.name}"
  args = { UPSTREAM_IMAGE = "ghcr.io/abundant-ai/sre-world/${image.repository}@sha256:${image.digest}" }
  tags = ["${REGISTRY}/saleor:${IMAGE_TAG}-${image.name}"]
}

target "saleor-server" {
  inherits = ["_common"]
  context = "docker/apps/saleor/server"
  tags = ["${REGISTRY}/saleor:${IMAGE_TAG}-server"]
}

target "slack-spine-images" {
  inherits = ["_common"]
  name = "slack-spine-${image.name}"
  matrix = {
    image = [
      { name = "app", component = "app", variant = "normal", repository = "slack-app", digest = "e050b1d953721145ee580e1baf1e424c55aa31c37a15c909e296c9607c8f7f4a" },
      { name = "app-v2", component = "app", variant = "seq-lock", repository = "slack-app", digest = "4a0a6587c7023bbe553a2f3b08db7c2597190313cd5674909d65185793133d93" },
      { name = "app-v3", component = "app", variant = "normal", repository = "slack-app", digest = "b73d144c2d817481956653e34095f38cfdc70e554d78f3521e72d0d1d6cd15fd" },
      { name = "builder", component = "builder", variant = "normal", repository = "slack-app-builder", digest = "5201d7a4c6e4313c2b9d01209ff6aed413af6f4cc2e11d667631f0ce4920fa07" },
      { name = "python", component = "python", variant = "normal", repository = "slack-py", digest = "c3a51b1ab5082f337ae5e8c1f2d60467273b1e73440580fc7e1132564b943c12" },
      { name = "toolbox", component = "toolbox", variant = "normal", repository = "slack-main", digest = "e3dec9737ddfb496b7c380ff529248bf4216514f0d0127f91e41973c05d940f9" },
      { name = "loadgen", component = "loadgen", variant = "normal", repository = "slack-loadgen", digest = "5a8bfa7e291a3e9ddc32eca99d291531964f08e3b1e269bd59fc9236a1eebbd2" },
      { name = "pgbouncer", component = "pgbouncer", variant = "normal", repository = "slack-pgbouncer", digest = "70dc0e4aefb23ba0646a3410358732ba8358f3b8aa95b8595f2decbba56ceff0" },
    ]
  }
  context = "docker/apps/slack_spine/${image.component}"
  args = {
    UPSTREAM_IMAGE = "ghcr.io/abundant-ai/sre-world/${image.repository}@sha256:${image.digest}"
    VARIANT = image.variant
  }
  tags = ["${REGISTRY}/slack-spine:${IMAGE_TAG}-${image.name}"]
}

target "slack-spine-go" {
  inherits = ["_common"]
  context = "docker/apps/slack_spine/go"
  contexts = { go-source = "https://github.com/yuekai/sre-world.git#1f1a815e70f0f94f6307fd9854b26db963900ffe:substrates/slack-spine/go" }
  tags = ["${REGISTRY}/slack-spine:${IMAGE_TAG}-go"]
}

target "slack-spine-minio" {
  inherits = ["_common"]
  context = "docker/apps/slack_spine/minio"
  contexts = {
    minio-source = "https://github.com/minio/minio.git#5bf41aff1702adf2a11fa96a7b68f8651191e24d"
    mc-source = "https://github.com/minio/mc.git#04c5116c9bdf8b762acc54b5500a9a276a5ec05a"
  }
  tags = ["${REGISTRY}/slack-spine:${IMAGE_TAG}-minio"]
}

target "mattermost-server" {
  inherits = ["_common"]
  context = "docker/apps/mattermost/server"
  tags = ["${REGISTRY}/mattermost:${IMAGE_TAG}"]
}

target "stripe-backend" {
  inherits = ["_common"]
  context = "docker/apps/stripe/backend"
  tags = ["${REGISTRY}/stripe-marathon:${IMAGE_TAG}"]
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
  // Keep kubectl on the same Kubernetes minor version as the KIND node image.
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
  context = "docker/flight-ticket"
  dockerfile = "populate-redis.Dockerfile"
  contexts = { population-source = "./SREGym-applications/flight-ticket/populate_redis" }
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
