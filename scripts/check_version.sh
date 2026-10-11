#!/usr/bin/env bash
# Fail unless every place that names the release version agrees.
# Usage: ./scripts/check_version.sh [tag]     e.g. v0.6.0 (checked when given)
#
# __version__ is what the running service logs, Chart appVersion is the image
# tag the chart deploys, and k8s/deployment.yaml pins the image for the static
# manifests -- a release that bumps one and not the others ships a mismatch.
# The chart's own `version` is independent and is not checked here.

set -euo pipefail
cd "$(dirname "$0")/.."

pyproject=$(sed -nE 's/^version = "([^"]+)"/\1/p' pyproject.toml)
package=$(sed -nE 's/^__version__ = "([^"]+)"/\1/p' src/floodgate/__init__.py)
chart=$(sed -nE 's/^appVersion: "([^"]+)"/\1/p' charts/floodgate/Chart.yaml)
manifest=$(sed -nE 's#^ *image: ghcr\.io/eric-becker/floodgate:([^@[:space:]]+).*#\1#p' k8s/deployment.yaml)

status=0
check() {   # check <where> <found>
    if [ -z "$2" ]; then
        echo "MISSING: could not read a version from $1"
        status=1
    elif [ "$2" != "$pyproject" ]; then
        echo "MISMATCH: $1 is '$2', pyproject.toml is '$pyproject'"
        status=1
    fi
}

if [ -z "$pyproject" ]; then
    echo "MISSING: could not read a version from pyproject.toml"
    exit 1
fi
check "src/floodgate/__init__.py __version__" "$package"
check "charts/floodgate/Chart.yaml appVersion" "$chart"
check "k8s/deployment.yaml image tag" "$manifest"
if [ $# -gt 0 ]; then
    check "the tag ($1)" "${1#v}"
fi

[ $status -eq 0 ] && echo "Version $pyproject is consistent${1:+ with tag $1}"
exit $status
