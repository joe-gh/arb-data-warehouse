#!/bin/bash
# ============================================================================
# Publish Logo Admin images (UPLOAD_DIR) to the public media server.
#
# SAFETY MODEL - the media box holds the only copy of legacy assets, so this
# script is strictly additive:
#   * --ignore-existing : an existing remote file is NEVER overwritten
#   * no --delete       : nothing is ever removed remotely
#   * dedicated subdir  : we publish ONLY into $MEDIA_REMOTE_DIR (our own
#                         directory, e.g. .../images/logos/warehouse/) and
#                         never touch legacy files outside it
# Filenames are content-hashes, so "same name" implies "same content" -
# skipping existing files is always correct.
#
# Install (warehouse box):
#   sudo cp publish-logo-media.sh /opt/arb-logo-admin/publish-logo-media.sh
#   sudo chmod 755 /opt/arb-logo-admin/publish-logo-media.sh
#   crontab (root): * * * * * /opt/arb-logo-admin/publish-logo-media.sh >> /var/log/arb-logo-admin-publish.log 2>&1
#
# One-time setup:
#   1. Generate a dedicated key on the warehouse:
#        sudo -u arb-logo-admin ssh-keygen -t ed25519 -f /var/lib/arb-logo-admin/.ssh/id_media -N ''
#   2. Append its .pub to the media box user's authorized_keys.
#   3. Obtain the media host key through an authenticated operations channel
#      and install it in /etc/arb-logo-admin/media_known_hosts (root:root 0644).
#   4. Create the remote dir on the media box and make it writable by that user.
#   5. Fill in the config below (or /etc/arb-logo-admin-publish.env).
# ============================================================================
set -u

CONF=/etc/arb-logo-admin-publish.env
[ -f "$CONF" ] && . "$CONF"

UPLOAD_DIR="${UPLOAD_DIR:-/var/lib/arb-logo-admin/uploads}"
MEDIA_SSH_TARGET="${MEDIA_SSH_TARGET:-ubuntu@172.31.1.179}"      # media box, private IP (same VPC)
MEDIA_REMOTE_DIR="${MEDIA_REMOTE_DIR:-/var/www/media/images/logos/warehouse/}"
MEDIA_SSH_KEY="${MEDIA_SSH_KEY:-/var/lib/arb-logo-admin/.ssh/id_media}"
MEDIA_KNOWN_HOSTS="${MEDIA_KNOWN_HOSTS:-/etc/arb-logo-admin/media_known_hosts}"
LOCK=/tmp/arb-logo-media-publish.lock

if [ ! -f "$MEDIA_KNOWN_HOSTS" ] || [ -L "$MEDIA_KNOWN_HOSTS" ] || [ ! -s "$MEDIA_KNOWN_HOSTS" ]; then
    echo "$(date -Is) publish refused: known_hosts must be a non-empty regular file" >&2
    exit 1
fi
if find "$MEDIA_KNOWN_HOSTS" -maxdepth 0 -perm /022 | grep -q .; then
    echo "$(date -Is) publish refused: known_hosts is group/world writable" >&2
    exit 1
fi

# Nothing to do when the upload dir is absent/empty.
[ -d "$UPLOAD_DIR" ] || exit 0
find "$UPLOAD_DIR" -maxdepth 1 -type f -name '[a-f0-9]*.*' | grep -q . || exit 0

exec 9>"$LOCK"
flock -n 9 || exit 0   # a previous publish is still running

rsync -a --ignore-existing --chmod=F644 \
      --include='*.png' --include='*.jpg' --include='*.gif' --include='*.webp' --exclude='*' \
      -e "ssh -i $MEDIA_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=$MEDIA_KNOWN_HOSTS -o ConnectTimeout=10" \
      "$UPLOAD_DIR"/ "$MEDIA_SSH_TARGET:$MEDIA_REMOTE_DIR"
rc=$?
if [ $rc -ne 0 ]; then
    echo "$(date -Is) publish failed rc=$rc" >&2
    exit $rc
fi
exit 0
