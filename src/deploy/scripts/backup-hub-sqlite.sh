#!/usr/bin/env bash
# QwenPaw hub SQLite backup (EP-2-5, A8): online-consistent
# snapshots via sqlite3 ".backup" (safe under WAL while the hub is
# running), with rotation and a sha256 manifest.
#
# Usage:
#   backup-hub-sqlite.sh <hub-root> <backup-dir> [keep]
#
#   hub-root    directory holding the SQLite databases
#               (K8s: the hub PVC mount, e.g. /var/lib/qwenpaw)
#   backup-dir  destination for timestamped snapshots
#   keep        how many snapshot directories to retain (default 7)
set -euo pipefail

HUB_ROOT="${1:?usage: backup-hub-sqlite.sh <hub-root> <backup-dir> [keep]}"
BACKUP_DIR="${2:?usage: backup-hub-sqlite.sh <hub-root> <backup-dir> [keep]}"
KEEP="${3:-7}"

DATABASES=(control.db knowledge.db sessions.db)

command -v sqlite3 >/dev/null || {
    echo "sqlite3 not found in PATH" >&2
    exit 1
}
[ -d "$HUB_ROOT" ] || {
    echo "hub root not a directory: $HUB_ROOT" >&2
    exit 1
}

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="$BACKUP_DIR/$STAMP"
mkdir -p "$TARGET"

copied=0
for name in "${DATABASES[@]}"; do
    source_db="$HUB_ROOT/$name"
    [ -f "$source_db" ] || continue
    # ".backup" takes the SQLite-level consistent snapshot: it opens
    # a read transaction and copies pages, so a concurrently writing
    # hub (WAL) cannot tear the snapshot.
    sqlite3 "$source_db" ".backup '$TARGET/$name'"
    copied=$((copied + 1))
done

# Secrets (Fernet vault material) are plain files — copy verbatim.
if [ -d "$HUB_ROOT/secrets" ]; then
    cp -R "$HUB_ROOT/secrets" "$TARGET/secrets"
    chmod -R go-rwx "$TARGET/secrets"
    copied=$((copied + 1))
fi

if [ "$copied" -eq 0 ]; then
    echo "nothing to back up under $HUB_ROOT" >&2
    rmdir "$TARGET"
    exit 1
fi

if command -v sha256sum >/dev/null; then
    HASH="sha256sum"
else
    HASH="shasum -a 256"
fi
( cd "$TARGET" && find . -type f -exec $HASH {} + ) >"$TARGET/SHA256SUMS"

echo "backed up $copied item(s) -> $TARGET"

# Rotate: keep the newest $KEEP snapshot directories.
ls -1 "$BACKUP_DIR" | grep -E '^[0-9]{8}T[0-9]{6}Z$' | sort -r \
    | tail -n +"$((KEEP + 1))" \
    | while read -r old; do
        rm -rf "$BACKUP_DIR/$old"
        echo "rotated away $old"
    done
