#!/usr/bin/env bash
#
# Shared permission-detection/repair helpers for SPIDQAH's CloudPanel
# persistent storage. Sourced by scripts/deploy-vps.sh and
# scripts/recover.sh - not meant to be run directly.
#
# Past incidents pinned the PostgreSQL and web-container uids by hand
# (e.g. 70:70, 10001:10001) and that drifted when the base images
# changed. These helpers never hardcode a uid/gid: they always ask the
# actual image in use at the time, so they keep working even if the
# postgres/web base image changes later.

# Prints "uid:gid" for a username inside a given image, using a
# throwaway container. Returns non-zero (prints nothing) if it cannot
# be determined - callers must handle that without guessing.
detect_image_uid_gid() {
  local image="$1" user="$2" u g
  u="$(docker run --rm --entrypoint id "$image" -u "$user" 2>/dev/null)" || return 1
  g="$(docker run --rm --entrypoint id "$image" -g "$user" 2>/dev/null)" || return 1
  [[ "$u" =~ ^[0-9]+$ ]] && [[ "$g" =~ ^[0-9]+$ ]] || return 1
  printf '%s:%s\n' "$u" "$g"
}

# Corrects ownership/permissions of the PostgreSQL bind-mounted data
# directory ONLY - never recurses into anything else. Detects the
# postgres image's own "postgres" user dynamically.
fix_postgres_permissions() {
  local data_dir="$1" pg_image="$2" uidgid
  [ -d "$data_dir" ] || return 0
  uidgid="$(detect_image_uid_gid "$pg_image" postgres)" || {
    echo "WARNING: could not detect the postgres user's uid/gid from ${pg_image} - leaving ${data_dir} ownership unchanged." >&2
    return 0
  }
  chown -R "$uidgid" "$data_dir"
  chmod 700 "$data_dir"
  echo "PostgreSQL data directory (${data_dir}) ownership set to ${uidgid} (from ${pg_image})."
}

# Corrects ownership/permissions of the uploads bind-mounted directory
# ONLY. Detects the web image's own "appuser" dynamically.
fix_uploads_permissions() {
  local uploads_dir="$1" web_image="$2" uidgid
  [ -d "$uploads_dir" ] || return 0
  uidgid="$(detect_image_uid_gid "$web_image" appuser)" || {
    echo "WARNING: could not detect appuser's uid/gid from ${web_image} - leaving ${uploads_dir} ownership unchanged." >&2
    return 0
  }
  chown -R "$uidgid" "$uploads_dir"
  chmod 700 "$uploads_dir"
  echo "Uploads directory (${uploads_dir}) ownership set to ${uidgid} (from ${web_image})."
}
