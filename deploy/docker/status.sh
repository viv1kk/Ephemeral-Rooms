#!/usr/bin/env bash
#
# Answer one question: is what is RUNNING what was PUBLISHED?
#
# Continuous deployment is a chain of places a change can stall - CI, the
# registry, Watchtower's poll, the container itself - and "the site looks fine"
# distinguishes none of them. This prints the state of each link, so a stalled
# deployment localises instead of reading as "it didn't work".
#
# Usage, from the directory holding docker-compose.yml:
#
#     ./deploy/docker/status.sh
#
# Exit status is 0 when every watched service is current and 1 when something is
# stale, so it also works as a check in a script.
#
#   !!  COMPARE :latest TO :latest, NEVER TO A :sha- TAG. The promote job
#   !!  re-points :latest with `imagetools create`, which wraps the image in a
#   !!  new manifest INDEX - so :latest and :sha-<commit> report different
#   !!  digests for byte-identical content. Comparing across the two produces a
#   !!  confident, entirely wrong "STALE".

set -euo pipefail

APP_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$APP_DIR"

bold=$(printf '\033[1m'); dim=$(printf '\033[2m'); off=$(printf '\033[0m')
head() { printf '\n%s==> %s%s\n' "$bold" "$1" "$off"; }
fail() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ -f docker-compose.yml ] || fail "no docker-compose.yml in $APP_DIR"
[ -f .env ]               || fail "no .env in $APP_DIR - see .env.example"

# Same daemon-access dance as update.sh: a deploy user is often in the docker
# group, and often is not.
DOCKER=(docker)
if ! docker info >/dev/null 2>&1; then
  if sudo -n docker info >/dev/null 2>&1; then DOCKER=(sudo docker)
  else fail "cannot talk to the Docker daemon as $(id -un), with or without sudo"; fi
fi
compose() { "${DOCKER[@]}" compose "$@"; }

ns=$(sed -n 's/^DOCKERHUB_NAMESPACE=\(.*\)$/\1/p' .env | tail -1)
[ -n "$ns" ] || fail "DOCKERHUB_NAMESPACE is not set in .env"

# ------------------------------------------------------------------- images
#
# The local side is the RepoDigest of the image the container was created from
# - which is what was actually pulled, not merely what the tag means today. The
# remote side is what the tag resolves to right now. Equal means current.

stale=0

head "Images"
printf '  %-9s %-12s %s\n' "SERVICE" "STATE" "DIGEST"
for svc in backend web; do
  repo="${ns}/ephemeral-rooms-${svc}"

  cid=$(compose ps -q "$svc" 2>/dev/null || true)
  if [ -z "$cid" ]; then
    printf '  %-9s %-12s %s\n' "$svc" "NOT RUNNING" "-"
    stale=1
    continue
  fi

  img=$("${DOCKER[@]}" inspect -f '{{.Image}}' "$cid")
  local_digest=$("${DOCKER[@]}" image inspect -f '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$img" 2>/dev/null | cut -d@ -f2)

  # A container built locally rather than pulled has no RepoDigest at all.
  # That is not "stale", it is "not from the registry", and saying so is more
  # useful than printing an empty comparison.
  if [ -z "$local_digest" ]; then
    printf '  %-9s %-12s %s\n' "$svc" "LOCAL BUILD" "not pulled from a registry"
    stale=1
    continue
  fi

  remote_digest=$("${DOCKER[@]}" buildx imagetools inspect "$repo:latest" 2>/dev/null | awk '/^Digest:/{print $2; exit}' || true)
  if [ -z "$remote_digest" ]; then
    printf '  %-9s %-12s %s\n' "$svc" "NO TAG" "$repo:latest is not in the registry"
    stale=1
    continue
  fi

  if [ "$local_digest" = "$remote_digest" ]; then
    printf '  %-9s %-12s %s\n' "$svc" "current" "${local_digest:0:26}…"
  else
    printf '  %-9s %-12s %s\n' "$svc" "STALE" "running  ${local_digest:0:26}…"
    printf '  %-9s %-12s %s\n' ""     ""      "registry ${remote_digest:0:26}…"
    stale=1
  fi
done

# ------------------------------------------------------------------ runtime

head "Containers"
compose ps --format '  {{.Name}}\t{{.Image}}\t{{.Status}}' 2>/dev/null || compose ps

# ---------------------------------------------------------------- watchtower
#
# Presence is not the interesting part - scope is. `scanned` must equal the
# number of containers carrying the enable label, which is `web` alone. If that
# number ever includes the backend, an unattended update would destroy every
# live room.

head "Watchtower"
if [ -z "$(compose ps -q watchtower 2>/dev/null || true)" ]; then
  echo "  not running - start it with: docker compose --profile watchtower up -d"
else
  labelled=$(
    for c in $(compose ps -q); do
      "${DOCKER[@]}" inspect -f '{{index .Config.Labels "com.centurylinklabs.watchtower.enable"}}' "$c"
    done | grep -c '^true$' || true
  )
  echo "  running; ${labelled} container(s) carry the enable label (expected: 1, web)"
  compose logs --tail 3 watchtower 2>/dev/null | sed 's/^/  /' || true
  echo "${dim}  Restarting the service does NOT force a check - it resets the timer."
  echo "  To check now:  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock \\"
  echo "                   nickfedor/watchtower:1.22.1 --label-enable --monitor-only --run-once${off}"
fi

# -------------------------------------------------------------- what is served
#
# The end of the chain, and the only check a user would notice. Vite hashes the
# bundle filenames, so these change whenever the frontend content does - which
# makes them the honest before/after for a frontend deploy. Fetched with curl
# rather than a browser deliberately: a browser can serve you a cached page and
# hide a deployment that did land.

head "Bundle being served"
origin=$(sed -n 's/^PUBLIC_ORIGIN=\(.*\)$/\1/p' .env | tail -1)
port=$(sed -n 's/^HTTP_PORT=\([0-9]\+\).*/\1/p' .env | tail -1)
for url in "${origin:-}" "http://127.0.0.1:${port:-8080}"; do
  [ -n "$url" ] || continue
  assets=$(curl -fsS --max-time 10 "$url/" 2>/dev/null | grep -o '/assets/[^"]*' | tr '\n' ' ' || true)
  if [ -n "$assets" ]; then
    printf '  %-34s %s\n' "$url" "$assets"
  else
    printf '  %-34s %s\n' "$url" "(no /assets/ reference - unreachable, or not the app)"
  fi
done

if [ "$stale" -ne 0 ]; then
  head "Result"
  echo "  Something is not current. If Watchtower is running, web catches up on its"
  echo "  next poll. The backend never updates itself - promote it deliberately:"
  echo "      docker compose pull backend && docker compose up -d backend"
  exit 1
fi

head "Result"
echo "  Everything watched is running the published image."
