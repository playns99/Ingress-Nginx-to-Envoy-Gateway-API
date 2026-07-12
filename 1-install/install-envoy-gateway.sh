#!/usr/bin/env bash
#
# install-envoy-gateway.sh
# ------------------------------------------------------------------------------
# Installs Envoy Gateway (Gateway API implementation) into the CURRENT
# kubectl context, in this order:
#
#   1. Gateway API CRDs            (upstream Kubernetes SIG)
#   2. Envoy Gateway control plane (Helm chart from OCI registry, ships its CRDs)
#   3. EnvoyProxy data-plane config (custom resource)
#   4. GatewayClass                (links Gateway API to the Envoy controller)
#   5. Shared Gateway + HTTP->HTTPS redirect
#
# It is intentionally cloud-agnostic: it uses whatever cluster your current
# kubectl context points at. Set the context BEFORE running this script.
#
# Requirements: kubectl, helm (v3.8+ for OCI support)
# ------------------------------------------------------------------------------
set -euo pipefail

# ------------------------------------------------------------------------------
# Defaults (override with flags or environment variables)
# ------------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GATEWAY_API_VERSION="${GATEWAY_API_VERSION:-v1.2.1}"      # Kubernetes Gateway API CRDs
ENVOY_GATEWAY_VERSION="${ENVOY_GATEWAY_VERSION:-v1.7.2}"  # Envoy Gateway Helm chart
ENVOY_NAMESPACE="${ENVOY_NAMESPACE:-envoy-gateway-system}"
HELM_RELEASE="${HELM_RELEASE:-envoy-gateway}"
HELM_OCI_CHART="${HELM_OCI_CHART:-oci://docker.io/envoyproxy/gateway-helm}"

MANIFESTS_DIR="${MANIFESTS_DIR:-$SCRIPT_DIR/manifests}"
GATEWAY_API_CHANNEL="${GATEWAY_API_CHANNEL:-standard}"    # standard | experimental

STEPS="all"
DRY_RUN="false"

# ------------------------------------------------------------------------------
usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Installs Envoy Gateway into the current kubectl context.

Options:
  --steps <list>          Comma-separated steps to run. Default: all
                          Valid: gateway-api-crds, envoy-gateway, envoyproxy,
                                 gatewayclass, shared-gateway, all
  --gateway-api-version   Gateway API CRD version (default: $GATEWAY_API_VERSION)
  --envoy-version         Envoy Gateway chart version (default: $ENVOY_GATEWAY_VERSION)
  --namespace             Envoy Gateway namespace (default: $ENVOY_NAMESPACE)
  --manifests-dir         Directory with platform manifests (default: manifests/)
  --channel               Gateway API channel: standard|experimental (default: standard)
  --dry-run               Print actions without applying anything
  -h, --help              Show this help

Environment variables mirror each flag (GATEWAY_API_VERSION, ENVOY_GATEWAY_VERSION,
ENVOY_NAMESPACE, HELM_RELEASE, HELM_OCI_CHART, MANIFESTS_DIR, GATEWAY_API_CHANNEL).

Examples:
  # Full install into the current context
  ./$(basename "$0")

  # Only (re)apply the shared gateway
  ./$(basename "$0") --steps shared-gateway

  # Preview everything without changing the cluster
  ./$(basename "$0") --dry-run
EOF
}

# ------------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --steps)                STEPS="$2"; shift 2 ;;
    --gateway-api-version)  GATEWAY_API_VERSION="$2"; shift 2 ;;
    --envoy-version)        ENVOY_GATEWAY_VERSION="$2"; shift 2 ;;
    --namespace)            ENVOY_NAMESPACE="$2"; shift 2 ;;
    --manifests-dir)        MANIFESTS_DIR="$2"; shift 2 ;;
    --channel)              GATEWAY_API_CHANNEL="$2"; shift 2 ;;
    --dry-run)              DRY_RUN="true"; shift ;;
    -h|--help)              usage; exit 0 ;;
    *) echo "Unknown argument: $1"; usage; exit 1 ;;
  esac
done

# ------------------------------------------------------------------------------
log()  { echo -e "[INFO]  $*"; }
warn() { echo -e "[WARN]  $*" >&2; }
err()  { echo -e "[ERROR] $*" >&2; }

require() {
  command -v "$1" >/dev/null 2>&1 || { err "'$1' is required but not installed."; exit 1; }
}

run() {
  # Wrapper that respects --dry-run.
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "      + $*"
  else
    "$@"
  fi
}

step_enabled() {
  [[ "$STEPS" == "all" ]] && return 0
  [[ ",$STEPS," == *",$1,"* ]] && return 0
  return 1
}

# ------------------------------------------------------------------------------
# Pre-flight checks
# ------------------------------------------------------------------------------
require kubectl
require helm

CURRENT_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
if [[ -z "$CURRENT_CONTEXT" ]]; then
  err "No current kubectl context. Set one with: kubectl config use-context <name>"
  exit 1
fi

log "Target context : $CURRENT_CONTEXT"
log "Gateway API     : $GATEWAY_API_VERSION ($GATEWAY_API_CHANNEL channel)"
log "Envoy Gateway   : $ENVOY_GATEWAY_VERSION"
log "Namespace       : $ENVOY_NAMESPACE"
log "Steps           : $STEPS"
[[ "$DRY_RUN" == "true" ]] && warn "DRY-RUN mode: no changes will be applied."
echo "------------------------------------------------------------------------"

# ------------------------------------------------------------------------------
# Step 1: Gateway API CRDs (from upstream)
# ------------------------------------------------------------------------------
install_gateway_api_crds() {
  log "Step 1: Installing Gateway API CRDs ($GATEWAY_API_CHANNEL/$GATEWAY_API_VERSION)"
  local url="https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/${GATEWAY_API_CHANNEL}-install.yaml"
  run kubectl apply --server-side --force-conflicts -f "$url"

  if [[ "$DRY_RUN" != "true" ]]; then
    kubectl wait --for=condition=Established crd/gatewayclasses.gateway.networking.k8s.io --timeout=180s
    kubectl wait --for=condition=Established crd/gateways.gateway.networking.k8s.io --timeout=180s
    kubectl wait --for=condition=Established crd/httproutes.gateway.networking.k8s.io --timeout=180s
  fi
}

# ------------------------------------------------------------------------------
# Step 2: Envoy Gateway control plane (Helm, ships its own CRDs)
# ------------------------------------------------------------------------------
install_envoy_gateway() {
  log "Step 2: Installing Envoy Gateway control plane via Helm"
  run helm upgrade --install "$HELM_RELEASE" "$HELM_OCI_CHART" \
    --version "$ENVOY_GATEWAY_VERSION" \
    --namespace "$ENVOY_NAMESPACE" \
    --create-namespace

  if [[ "$DRY_RUN" != "true" ]]; then
    kubectl -n "$ENVOY_NAMESPACE" rollout status deploy/envoy-gateway --timeout=300s
  fi
}

# ------------------------------------------------------------------------------
# Step 3: EnvoyProxy data-plane config
# ------------------------------------------------------------------------------
install_envoyproxy() {
  log "Step 3: Applying EnvoyProxy data-plane configuration"
  local file="$MANIFESTS_DIR/envoyproxy.yaml"
  [[ -f "$file" ]] || { err "Missing manifest: $file"; exit 1; }
  run kubectl apply -f "$file"
}

# ------------------------------------------------------------------------------
# Step 4: GatewayClass
# ------------------------------------------------------------------------------
install_gatewayclass() {
  log "Step 4: Applying GatewayClass"
  local file="$MANIFESTS_DIR/gatewayclass.yaml"
  [[ -f "$file" ]] || { err "Missing manifest: $file"; exit 1; }
  run kubectl apply -f "$file"
}

# ------------------------------------------------------------------------------
# Step 5: Shared Gateway + HTTP->HTTPS redirect
# ------------------------------------------------------------------------------
install_shared_gateway() {
  log "Step 5: Applying shared Gateway and HTTP->HTTPS redirect"
  local file="$MANIFESTS_DIR/shared-gateway.yaml"
  [[ -f "$file" ]] || { err "Missing manifest: $file"; exit 1; }
  run kubectl apply -f "$file"
}

# ------------------------------------------------------------------------------
# Orchestrate
# ------------------------------------------------------------------------------
step_enabled gateway-api-crds && install_gateway_api_crds
step_enabled envoy-gateway    && install_envoy_gateway
step_enabled envoyproxy       && install_envoyproxy
step_enabled gatewayclass     && install_gatewayclass
step_enabled shared-gateway   && install_shared_gateway

echo "------------------------------------------------------------------------"
log "Done. Validate with:"
echo "  kubectl get crd | grep gateway.networking.k8s.io"
echo "  kubectl get pods -n $ENVOY_NAMESPACE"
echo "  kubectl get gatewayclass"
echo "  kubectl get gateway -n $ENVOY_NAMESPACE"
echo "  kubectl get svc -n $ENVOY_NAMESPACE"
