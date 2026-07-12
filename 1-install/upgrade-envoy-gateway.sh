#!/usr/bin/env bash
#
# upgrade-envoy-gateway.sh
# ------------------------------------------------------------------------------
# Upgrades an existing Envoy Gateway installation in the CURRENT kubectl context.
#
# Upgrade order matters:
#   1. Gateway API CRDs   (upgrade CRDs BEFORE the controller that consumes them)
#   2. Envoy Gateway Helm release (control plane)
#   3. Re-apply platform manifests (EnvoyProxy / GatewayClass / Gateway)
#
# Always test in a non-production cluster first. Use --dry-run to preview.
#
# Requirements: kubectl, helm (v3.8+)
# ------------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GATEWAY_API_VERSION="${GATEWAY_API_VERSION:-v1.2.1}"
ENVOY_GATEWAY_VERSION="${ENVOY_GATEWAY_VERSION:-v1.7.2}"
ENVOY_NAMESPACE="${ENVOY_NAMESPACE:-envoy-gateway-system}"
HELM_RELEASE="${HELM_RELEASE:-envoy-gateway}"
HELM_OCI_CHART="${HELM_OCI_CHART:-oci://docker.io/envoyproxy/gateway-helm}"
MANIFESTS_DIR="${MANIFESTS_DIR:-$SCRIPT_DIR/manifests}"
GATEWAY_API_CHANNEL="${GATEWAY_API_CHANNEL:-standard}"
DRY_RUN="false"

usage() {
  cat <<EOF
Usage: $(basename "$0") [options]

Upgrades Gateway API CRDs and the Envoy Gateway control plane in the current context.

Options:
  --gateway-api-version   Target Gateway API CRD version (default: $GATEWAY_API_VERSION)
  --envoy-version         Target Envoy Gateway chart version (default: $ENVOY_GATEWAY_VERSION)
  --namespace             Envoy Gateway namespace (default: $ENVOY_NAMESPACE)
  --manifests-dir         Directory with platform manifests (default: manifests/)
  --channel               Gateway API channel: standard|experimental (default: standard)
  --dry-run               Preview without applying
  -h, --help              Show this help

Example:
  ./$(basename "$0") --envoy-version v1.8.0 --gateway-api-version v1.3.0 --dry-run
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
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

log()  { echo -e "[INFO]  $*"; }
warn() { echo -e "[WARN]  $*" >&2; }
err()  { echo -e "[ERROR] $*" >&2; }
require() { command -v "$1" >/dev/null 2>&1 || { err "'$1' is required."; exit 1; }; }
run() { if [[ "$DRY_RUN" == "true" ]]; then echo "      + $*"; else "$@"; fi; }

require kubectl
require helm

CURRENT_CONTEXT="$(kubectl config current-context 2>/dev/null || true)"
[[ -n "$CURRENT_CONTEXT" ]] || { err "No current kubectl context."; exit 1; }

log "Target context : $CURRENT_CONTEXT"
log "Gateway API ->  : $GATEWAY_API_VERSION"
log "Envoy Gateway ->: $ENVOY_GATEWAY_VERSION"
[[ "$DRY_RUN" == "true" ]] && warn "DRY-RUN mode: no changes will be applied."
echo "------------------------------------------------------------------------"

# Show current state for reference / rollback planning.
log "Current Helm release history:"
helm -n "$ENVOY_NAMESPACE" history "$HELM_RELEASE" 2>/dev/null || warn "No existing release '$HELM_RELEASE' found."

# 1) Upgrade Gateway API CRDs first.
log "Upgrading Gateway API CRDs to $GATEWAY_API_VERSION"
run kubectl apply --server-side --force-conflicts \
  -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/${GATEWAY_API_CHANNEL}-install.yaml"

# 2) Upgrade Envoy Gateway control plane.
log "Upgrading Envoy Gateway control plane to $ENVOY_GATEWAY_VERSION"
run helm upgrade --install "$HELM_RELEASE" "$HELM_OCI_CHART" \
  --version "$ENVOY_GATEWAY_VERSION" \
  --namespace "$ENVOY_NAMESPACE"

if [[ "$DRY_RUN" != "true" ]]; then
  kubectl -n "$ENVOY_NAMESPACE" rollout status deploy/envoy-gateway --timeout=300s
fi

# 3) Re-apply platform manifests so any data-plane config changes take effect.
for f in envoyproxy.yaml gatewayclass.yaml shared-gateway.yaml; do
  if [[ -f "$MANIFESTS_DIR/$f" ]]; then
    log "Re-applying $f"
    run kubectl apply -f "$MANIFESTS_DIR/$f"
  fi
done

echo "------------------------------------------------------------------------"
log "Upgrade complete. Verify data-plane pods rolled over:"
echo "  kubectl get pods -n $ENVOY_NAMESPACE"
echo
log "Rollback (if needed):"
echo "  helm -n $ENVOY_NAMESPACE history $HELM_RELEASE"
echo "  helm -n $ENVOY_NAMESPACE rollback $HELM_RELEASE <REVISION>"
