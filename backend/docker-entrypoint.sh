#!/bin/sh
#
# Mounts the room storage, then hands over to Uvicorn as an unprivileged user.
#
# The point of all this is that DATA_ROOT must be a filesystem of a FIXED size,
# because that is the only kind of limit the application can see. Everything it
# does about disk space - the figure in the UI, admitting an upload, the
# mid-transfer re-check - comes from statvfs on this directory (spec section
# 17). A cap that is invisible to statvfs, such as a project quota, would let
# the server accept an upload it cannot finish and fail it partway through,
# which is the exact failure the reservation ledger exists to prevent.
#
# Docker cannot mount this for us. Its `local` volume driver calls mount(2),
# which has no loop handling - `-o loop` is a mount(8) userspace feature - and
# pointing a volume at a pre-attached /dev/loopN leaves a pointer to a kernel
# object that does not survive a reboot: the volume then fails to mount, the
# container never starts, and a restart policy does not retry a container that
# never started. Doing it here instead means the whole setup is re-done from
# scratch on every single start, which is exactly what makes it survive a
# reboot unattended.
#
# Runs as root only long enough to mount. Uvicorn is exec'd as `ephemeral`
# with no capabilities.

set -eu

IMG="${ROOM_DISK_IMAGE:-/backing/rooms.img}"
SIZE="${ROOM_DISK_SIZE:-10G}"
MOUNT="${DATA_ROOT:-/var/lib/ephemeral-rooms}"

log() { echo "entrypoint: $*"; }

# mount(8) needs a /dev/loopN node to hand to the kernel, and a container's
# /dev is a private, nearly empty devtmpfs. The device-cgroup rule in
# docker-compose.yml is what makes creating them permissible; without it these
# fail and the mount below reports "failed to setup loop device".
i=0
while [ "$i" -lt 16 ]; do
    [ -e "/dev/loop$i" ] || mknod "/dev/loop$i" b 7 "$i" 2>/dev/null || true
    i=$((i + 1))
done

if [ ! -f "$IMG" ]; then
    log "creating a $SIZE image at $IMG"
    mkdir -p "$(dirname "$IMG")"
    # Sparse: it claims $SIZE immediately but occupies only what is written, so
    # the ceiling is real while the footprint on the host grows with use.
    truncate -s "$SIZE" "$IMG"
    # -m 0 because ext4 otherwise reserves 5% for root, and on a volume that
    # holds nothing but room files that is 500 MB of the budget spent on
    # nothing. -F because the target is a file rather than a block device.
    mkfs.ext4 -q -F -m 0 "$IMG"
fi

# A loop device left over from a previous container. `mount -o loop` sets
# LO_FLAGS_AUTOCLEAR so this is normally already gone, but a container killed
# rather than stopped can leave one behind - and mounting the same filesystem
# through a second loop device would corrupt it. Safe because exactly one
# backend runs; see the single-worker warning in app/main.py.
for stale in $(losetup -j "$IMG" 2>/dev/null | cut -d: -f1); do
    log "detaching stale loop device $stale"
    losetup -d "$stale" 2>/dev/null || true
done

mkdir -p "$MOUNT"
mount -o loop "$IMG" "$MOUNT"

# The fresh ext4 root belongs to root; the application does not.
chown ephemeral:ephemeral "$MOUNT"
chmod 0700 "$MOUNT"

log "$(df -h "$MOUNT" | tail -1)"

# setpriv rather than su/gosu: no extra package, no intermediate process left
# supervising, and --inh-caps -all makes sure nothing that follows can regain
# the SYS_ADMIN this script needed. Uvicorn ends up as PID 1's direct
# replacement, so it still receives SIGTERM on shutdown.
exec setpriv --reuid ephemeral --regid ephemeral --clear-groups --inh-caps=-all "$@"
