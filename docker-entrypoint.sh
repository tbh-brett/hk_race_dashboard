#!/usr/bin/env bash
# Seed and wire up the persistent volume, then exec the app.
#
# dashboard.py resolves every data path from BASE = Path(__file__).parent, so
# the app insists on reading and writing inside its own directory. Fly volumes
# mount elsewhere. Bridge the two: keep the real data on /data (the volume) and
# replace each in-tree data path with a symlink into it.
#
# First boot copies the image's committed snapshot into the empty volume.
# Every later boot leaves the volume alone — the volume is the source of
# truth, so a deploy shipping an older committed hkjc.db can never roll back
# live data (the regression fixed in commit b14d292).
set -euo pipefail

DATA_DIR="${DATA_DIR:-/data}"
# Where the app actually lives. Derived from this script's own location rather
# than hardcoded to /app, because that is exactly how dashboard.py resolves
# BASE — the two can never drift apart.
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

# Runtime-written paths, split by kind so we never have to guess from the
# filename whether something is a directory.
PERSIST_DIRS=(
    "reports"
    "cache"
    "racecards"
    "models"
    "running_position_photos"
)
PERSIST_FILES=(
    "blackbook.json"
    "hkjc.db"
    "hkjc_results_updated.xlsx"
    "horse_ability_analysis_v3.xlsx"
    "expected_time_references_v4.xlsx"
)

if [ ! -d "$DATA_DIR" ]; then
    echo "[entrypoint] WARNING: $DATA_DIR is not mounted — running with ephemeral storage."
    echo "[entrypoint] WARNING: data written this boot will be LOST on restart."
    exec "$@"
fi

# Seed one path into the volume, if and only if it isn't there yet.
#
# The copy goes to a temp name and is renamed into place, so $dest exists only
# once the copy finished. Without that, an interrupted first boot leaves a
# half-written directory that looks seeded to every later boot — and the app
# then symlinks to a partial (or empty) data set with no error anywhere.
seed_path() {
    local path="$1" kind="$2"
    local src="$APP_DIR/$path"
    local dest="$DATA_DIR/$path"
    local tmp="$DATA_DIR/.seeding-$path.$$"

    if [ -e "$dest" ]; then
        return 0
    fi

    if [ -e "$src" ]; then
        echo "[entrypoint] seeding $path"
        rm -rf "$tmp"
        # cp -a handles files and directories alike, and is coreutils — no
        # dependency on rsync being present in the image.
        cp -a "$src" "$tmp"
        mv "$tmp" "$dest"
    elif [ "$kind" = "dir" ]; then
        # Nothing committed to seed from; the app still needs somewhere to
        # write, so give it an empty directory.
        echo "[entrypoint] creating empty $path"
        mkdir -p "$dest"
    fi
}

# Point the in-tree path at the volume. Redone every boot because each deploy
# ships a fresh $APP_DIR.
link_path() {
    local path="$1"
    local src="$APP_DIR/$path"
    local dest="$DATA_DIR/$path"

    [ -e "$dest" ] || return 0
    rm -rf "$src"
    ln -s "$dest" "$src"
}

for d in "${PERSIST_DIRS[@]}"; do
    seed_path "$d" dir
    link_path "$d"
done
for f in "${PERSIST_FILES[@]}"; do
    seed_path "$f" file
    link_path "$f"
done

# Clear any temp dirs left by a boot that died mid-copy.
rm -rf "$DATA_DIR"/.seeding-* 2>/dev/null || true

echo "[entrypoint] volume ready at $DATA_DIR ($(du -sh "$DATA_DIR" 2>/dev/null | cut -f1) used)"
exec "$@"
