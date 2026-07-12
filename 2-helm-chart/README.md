# 2 - `gateway-routes` Helm chart

A small, generic Helm chart that deploys **Gateway API** `HTTPRoute`s and
**Envoy Gateway** policies for a single application. Use it to expose an app on
the shared platform gateway, on its own custom domain, or both.

## What it can render

| Template | Resource | When |
|----------|----------|------|
| `httproute.yaml` | `HTTPRoute` | Always (when `gatewayAPI.enabled=true`) |
| `gateway.yaml` | `Gateway` | When `gatewayAPI.customHostnames` is set |
| `backendtrafficpolicy.yaml` | `BackendTrafficPolicy` | When `policies.backendTraffic.enabled=true` |
| `backendtlspolicy.yaml` | `BackendTLSPolicy` | When `policies.backendTLS.enabled=true` |
| `clienttrafficpolicy.yaml` | `ClientTrafficPolicy` | When `policies.clientTraffic.enabled=true` |
| `envoypatchpolicy.yaml` | `EnvoyPatchPolicy` | When `policies.envoyPatch.enabled=true` |

Nothing is created unless `gatewayAPI.enabled=true`.

## Quick start

```bash
# From this folder
helm lint gateway-routes

# Render locally to inspect the output (no cluster needed)
helm template myapp gateway-routes -f gateway-routes/examples/01-shared-gateway-basic.yaml

# Install
helm install myapp gateway-routes \
  -f gateway-routes/examples/01-shared-gateway-basic.yaml \
  -n myapp --create-namespace
```

## Examples

| File | Scenario |
|------|----------|
| `examples/01-shared-gateway-basic.yaml` | One hostname on the shared gateway, default backend |
| `examples/02-shared-gateway-with-policies.yaml` | Path routing + rewrite + timeout/retry + upstream TLS |
| `examples/03-custom-domain.yaml` | App-owned Gateway with its own domain + TLS secret |
| `examples/04-shared-and-custom.yaml` | Shared wildcard **and** custom domain together |

## Key values

```yaml
application:
  name: myapp            # defaults to release name

service:
  port: 8080             # default backend port

gatewayAPI:
  enabled: true          # master switch
  gatewayName: main-gateway
  gatewayNamespace: envoy-gateway-system
  hostnames:             # served on the shared wildcard gateway
    - myapp.example.com
  customHostnames: []    # app-owned Gateway domains (optional)
  rules: []              # advanced path/filter/backend rules (optional)
  policies:
    backendTraffic: { enabled: false, spec: {} }
    backendTLS:     { enabled: false, spec: {} }
    clientTraffic:  { enabled: false, spec: {} }
    envoyPatch:     { enabled: false, spec: {} }
```

See `values.yaml` for the fully commented set of options.

## Guardrail

On a shared **wildcard** gateway, an `HTTPRoute` with no hostnames becomes a
catch-all that hijacks every unmatched subdomain. This chart therefore **fails
the render** if `gatewayAPI.enabled=true` but neither `hostnames` nor
`customHostnames` is set.

## Design notes

- **Body size:** no request body size limit is set. Envoy streams bodies by
  default; this matches typical needs and avoids porting NGINX `proxy-body-size`.
- **Timeouts:** apply per-app via `policies.backendTraffic.spec.timeout`. A
  gateway-wide default (60s) is set by the platform install (folder `1-install`).
- **`ClientTrafficPolicy`** can only target a Gateway, so it is meant for the
  custom-domain Gateway case or platform-level use — not per-route tuning.
- **`EnvoyPatchPolicy`** is kept in the chart for future use even though it is
  disabled by default. It requires the control plane to have
  `enableEnvoyPatchPolicy: true`.
