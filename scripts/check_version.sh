#!/usr/bin/env bash
# Fail unless every place that names the release version agrees.
# Usage: ./scripts/check_version.sh [tag]     e.g. v0.6.0 (checked when given)
#
# __version__ is what the running service logs, Chart appVersion is the image
# tag the chart deploys, and k8s/deployment.yaml pins the image for the static
# manifests -- a release that bumps one and not the others ships a mismatch.

set -euo pipefail
cd "$(dirname "$0")/.."

pyproject=$(sed -nE 's/^version = "([^"]+)"/\1/p' pyproject.toml)
package=$(sed -nE 's/^__version__ = "([^"]+)"/\1/p' src/floodgate/__init__.py)
chart=$(sed -nE 's/^appVersion: "([^"]+)"/\1/p' charts/floodgate/Chart.yaml)
manifest=$(sed -nE 's#^ *image: ghcr\.io/eric-becker/floodgate:(.+)$#\1#p' k8s/deployment.yaml)

status=0
for pair in "src/floodgate/__init__.py:$package" "charts/floodgate/Chart.yaml appVersion:$chart" \
            "k8s/deployment.yaml image:$manifest"; do
    if [ "${pair##*:}" != "$pyproject" ]; then
        echo "MISMATCH: ${pair%:*} is '${pair##*:}', pyproject.toml is '$pyproject'"
        status=1
    fi
done
if [ $# -gt 0 ] && [ "${1#v}" != "$pyproject" ]; then
    echo "MISMATCH: tag is '$1', pyproject.toml is '$pyproject'"
    status=1
fi
[ $status -eq 0 ] && echo "Version $pyproject is consistent${1:+ with tag $1}"
exit $status
