# Runtime images

These images include the packages and application files that some helpers need at startup.

| Image | Used by |
| -- | -- |
| `ghcr.io/sregym/redis-client:8.1.0-py3.10` | Valkey memory helper |
| `ghcr.io/sregym/kafka-client:2.5.3-py3.12` | Kafka producer and validator helpers |
| `ghcr.io/sregym/tls-client:ubuntu22.04` | TLS verification sidecar |
| `ghcr.io/sregym/social-network-assets:v1` | Social Network init containers |
| `ghcr.io/sregym/grafana:12.3.1-opensearch2.34.3` | Astronomy Shop Grafana with the OpenSearch plugin |

The images support `linux/amd64` and `linux/arm64`. Normal runs do not require a local image build.
