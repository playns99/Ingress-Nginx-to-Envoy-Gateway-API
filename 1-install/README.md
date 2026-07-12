# 1 - Install Envoy Gateway

This folder installs [Envoy Gateway](https://gateway.envoyproxy.io/) (a Gateway
API implementation) into whatever cluster your **current kubectl context**
points at. It is cloud-agnostic — no vendor-specific login logic.

## What gets installed

| Order | Component | Source | Purpose |
|-------|-----------|--------|---------|
| 1 | Gateway API CRDs | Kubernetes SIG (upstream) | The `Gateway`, `HTTPRoute`, `GatewayClass` API types |
| 2 | Envoy Gateway control plane | Helm chart (OCI) | The controller that reconciles Gateway API objects |
| 3 | `EnvoyProxy` config | `manifests/envoyproxy.yaml` | Data-plane sizing, image, autoscaling, Service type |
| 4 | `GatewayClass` | `manifests/gatewayclass.yaml` | Binds Gateways to the Envoy controller |
| 5 | Shared `Gateway` + HTTP→HTTPS redirect | `manifests/shared-gateway.yaml` | The platform gateway apps attach routes to |

## Prerequisites

- `kubectl` (pointed at the target cluster)
- `helm` v3.8+ (for OCI chart support)
- A TLS secret for your wildcard domain in `envoy-gateway-system`
  (referenced by `shared-gateway.yaml`)

Set your context **before** running anything:

```bash
kubectl config use-context <your-cluster>
kubectl config current-context   # confirm
```

## Configure

Edit `manifests/shared-gateway.yaml` and replace the placeholders:

- `*.example.com` → your wildcard domain
- `shared-tls-secret` → the Secret holding your wildcard certificate

Optionally adjust `manifests/envoyproxy.yaml` (image registry, CPU/memory, HPA,
Service type / load-balancer annotations).

## Install

```bash
chmod +x install-envoy-gateway.sh upgrade-envoy-gateway.sh

# Full install
./install-envoy-gateway.sh

# Preview only (no cluster changes)
./install-envoy-gateway.sh --dry-run

# Run a single step (e.g. re-apply just the shared gateway)
./install-envoy-gateway.sh --steps shared-gateway

# Pin versions
./install-envoy-gateway.sh \
  --gateway-api-version v1.2.1 \
  --envoy-version v1.7.2
```

## Validate

```bash
kubectl get crd | grep gateway.networking.k8s.io
kubectl get pods -n envoy-gateway-system
kubectl get gatewayclass
kubectl get gateway -n envoy-gateway-system
kubectl get svc -n envoy-gateway-system         # note the external/LB address
```

A healthy `GatewayClass` shows `ACCEPTED=True`; a healthy `Gateway` shows
`PROGRAMMED=True`.

## Upgrade

Always upgrade **CRDs before the controller**, and test in a non-prod cluster first.

```bash
# Preview
./upgrade-envoy-gateway.sh \
  --gateway-api-version v1.3.0 \
  --envoy-version v1.8.0 \
  --dry-run

# Apply
./upgrade-envoy-gateway.sh \
  --gateway-api-version v1.3.0 \
  --envoy-version v1.8.0
```

Check the latest stable versions here:

- Envoy Gateway releases: <https://github.com/envoyproxy/gateway/releases>
- Gateway API releases: <https://github.com/kubernetes-sigs/gateway-api/releases>
- Data-plane image compatibility: listed in each Envoy Gateway release note

Use stable tags (e.g. `v1.7.2`), not `-rc` or `latest`.

## Rollback

```bash
helm -n envoy-gateway-system history envoy-gateway
helm -n envoy-gateway-system rollback envoy-gateway <REVISION>
```

For CRD rollbacks, read the Gateway API release notes carefully before
downgrading — removed fields can break existing objects.

## Notes on defaults

- **Body size / buffering:** Envoy streams request bodies by default (no size
  cap). NGINX `proxy-body-size` annotations are intentionally **not** carried
  over — the Envoy default is correct for most apps.
- **Timeouts:** `shared-gateway.yaml` sets a 60s default request timeout to
  match NGINX's default and avoid surprise 15s timeouts after migration.
