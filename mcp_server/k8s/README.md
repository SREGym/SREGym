# In-Cluster Kubectl MCP Server

Kubernetes manifests for deploying the SREGym MCP server inside the cluster.

## Quick Start

```bash
# Build the Docker image (from repo root)
docker build -t sregym:latest -f mcp_server/Dockerfile .

# If using Kind, load the image
kind load docker-image sregym:latest

# Deploy to cluster
kubectl apply -k mcp_server/k8s/

# Verify deployment
kubectl get pods -n sregym
```

## Available MCP Endpoints

The server exposes multiple MCP tools via SSE:

| Endpoint | URL Path | Description |
|----------|----------|-------------|
| kubectl | `/kubectl/sse` | Kubernetes command execution |
| prometheus | `/prometheus/sse` | Metrics queries |
| loki | `/loki/sse` | Log queries |
| jaeger | `/jaeger/sse` | Trace queries |
| submit | `/submit/sse` | Benchmark submission |

## Customization

### Change Image Repository

Edit `kustomization.yaml`:

```yaml
images:
  - name: sregym
    newName: your-registry.com/sregym
    newTag: v1.0.0
```

### Configure Observability Endpoints

Edit the environment variables in `deployment.yaml`:

```yaml
env:
  - name: PROMETHEUS_BASE_URL
    value: "http://your-prometheus:9090"
  - name: LOKI_PORT
    value: "3100"
  - name: JAEGER_BASE_URL
    value: "http://your-jaeger:16686"
```

## RBAC

The ClusterRole grants permissions for common kubectl operations:
- Full CRUD on pods, deployments, services, configmaps, etc.
- Read-only access to namespaces and nodes
- Support for TiDB custom resources

Review `clusterrole.yaml` to adjust permissions as needed.
