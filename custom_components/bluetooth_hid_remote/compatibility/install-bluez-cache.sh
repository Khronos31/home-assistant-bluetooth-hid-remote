#!/bin/sh
set -eu

SOURCE=$1
ADAPTER=$2
DEVICE=$3

is_address() {
    case "$1" in
    [0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F]:[0-9A-F][0-9A-F])
        return 0
        ;;
    *)
        return 1
        ;;
    esac
}

if ! is_address "$ADAPTER" || ! is_address "$DEVICE"; then
        echo "Invalid Bluetooth address" >&2
        exit 2
fi

DEST_DIR="/var/lib/bluetooth/$ADAPTER/cache"
DEST="$DEST_DIR/$DEVICE"
BACKUP="$DEST.bluetooth-hid-remote.bak"

test -f "$SOURCE"
test -d "$DEST_DIR"

if test -f "$DEST" && ! test -e "$BACKUP"; then
    cp "$DEST" "$BACKUP"
    chmod 600 "$BACKUP"
fi

cp "$SOURCE" "$DEST"
chmod 600 "$DEST"
