#!/bin/bash
# Install one agent harness at its pinned version into a tools directory, for
# the GHA prep job (eval.yml, egress-audit.yml). The agent job, which has no
# GitHub and no npm access, gets the directory as an artifact.
#
#   ci/install-harness.sh <pi|omp|opencode> <version> <tools-dir>
#
# The version comes from config/harnesses.toml (the matrix's harness_version);
# the adapter refuses to run if the installed harness reports anything else.
# Needs node and npm on PATH (actions/setup-node), and curl and python3.
set -euo pipefail

harness="$1"
version="$2"
tools="$3"
mkdir -p "$tools/bin"

case "$harness" in
  pi)
    # pi's installer always installs the latest release (HARNESS.md, pi), so
    # use npm at the exact version, as on 2026-11-16. This pins the package,
    # not its dependency tree.
    npm install --prefix "$tools/pi" --no-fund --no-audit --loglevel=error \
      "@earendil-works/pi-coding-agent@${version}"
    ln -sf ../pi/node_modules/.bin/pi "$tools/bin/pi"
    ;;
  opencode)
    # OpenCode's own CI example installs the npm package; same, at the exact
    # version, into a prefix (HARNESS.md, OpenCode).
    npm install --prefix "$tools/opencode" --no-fund --no-audit --loglevel=error \
      "@opencode/cli@${version}"
    ln -sf ../opencode/node_modules/.bin/opencode "$tools/bin/opencode"
    # OpenCode's grep and glob tools use `rg` from PATH, and download it from
    # GitHub when it isn't there, which the agent job blocks. Install the
    # ripgrep release OpenCode itself would fetch, with the SHA-256 it pins
    # (packages/core/src/ripgrep/binary.ts in the OpenCode release).
    case "$version" in
      2.2.6) rg_version=15.1.0 rg_sha=1c9297be4a084eea7ecaedf93eb03d058d6faae29bbc57ecdaf5063921491599 ;;
      *) echo "::error::no pinned ripgrep for OpenCode ${version}; add it to ci/install-harness.sh"; exit 1 ;;
    esac
    rg_name="ripgrep-${rg_version}-x86_64-unknown-linux-musl"
    curl -fsSL --max-time 120 \
      "https://github.com/BurntSushi/ripgrep/releases/download/${rg_version}/${rg_name}.tar.gz" \
      -o "$tools/${rg_name}.tar.gz"
    got="$(sha256sum "$tools/${rg_name}.tar.gz" | cut -d' ' -f1)"
    if [ "$got" != "$rg_sha" ]; then
      echo "::error::ripgrep ${rg_version} download has sha256 ${got}, pinned ${rg_sha}"
      exit 1
    fi
    tar -xzf "$tools/${rg_name}.tar.gz" -C "$tools" "${rg_name}/rg"
    mv -f "$tools/${rg_name}/rg" "$tools/bin/rg"
    rm -rf "${tools:?}/${rg_name}" "${tools:?}/${rg_name}.tar.gz"
    echo "rg: $("$tools/bin/rg" --version | head -1)"
    ;;
  omp)
    # omp's prebuilt binary from build.stencil.so, the way its installer
    # (https://omp.sh/install --binary --ref v<version>) fetches it, but with
    # the binary's SHA-256 pinned here rather than taken from the same server.
    # Add a line when bumping omp in config/harnesses.toml:
    #   curl -fsSL https://build.stencil.so/api/products/omp/versions/<v>/linux-x86_64
    # and copy file.sha256.
    case "$version" in
      18.19.0) want=b44fda2ae5c9cce2bbe124d1672cba532a136d9e1eef68599b16efcd3bc5c626 ;;
      *) echo "::error::no pinned sha256 for omp ${version}; add it to ci/install-harness.sh"; exit 1 ;;
    esac
    meta="$(curl -fsSL --max-time 60 "https://build.stencil.so/api/products/omp/versions/${version}/linux-x86_64")"
    url="$(printf '%s' "$meta" | python3 -c 'import json,sys; print(json.load(sys.stdin)["download"])')"
    served="$(printf '%s' "$meta" | python3 -c 'import json,sys; print(json.load(sys.stdin)["file"]["sha256"])')"
    if [ "$served" != "$want" ]; then
      echo "::error::build.stencil.so lists sha256 ${served} for omp ${version}; pinned ${want}"
      exit 1
    fi
    case "$url" in http*) ;; *) url="https://build.stencil.so${url}" ;; esac
    curl -fsSL --max-time 900 "$url" -o "$tools/bin/omp.download"
    got="$(sha256sum "$tools/bin/omp.download" | cut -d' ' -f1)"
    if [ "$got" != "$want" ]; then
      echo "::error::omp ${version} download has sha256 ${got}, pinned ${want}"
      exit 1
    fi
    chmod +x "$tools/bin/omp.download"
    mv -f "$tools/bin/omp.download" "$tools/bin/omp"
    ;;
  *)
    echo "::error::unknown harness ${harness}"
    exit 1
    ;;
esac

reported="$("$tools/bin/$harness" --version 2>&1 | head -1)"
echo "$harness: $reported"
case "$reported" in
  *"$version"*) ;;
  *) echo "::error::$harness reports '$reported', pinned $version"; exit 1 ;;
esac
