#!/bin/sh
set -eu

MODE=$1
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

if test "$MODE" != "restore" || ! is_address "$ADAPTER" || ! is_address "$DEVICE"; then
    echo "Invalid restore request" >&2
    exit 2
fi

DEST_DIR="/var/lib/bluetooth/$ADAPTER/cache"
DEST="$DEST_DIR/$DEVICE"
BACKUP="$DEST.bluetooth-hid-remote.bak"
MISSING="$DEST.bluetooth-hid-remote.missing"

test -d "$DEST_DIR"

if test -f "$BACKUP"; then
    cp "$BACKUP" "$DEST"
    chmod 600 "$DEST"
    rm -f "$BACKUP" "$MISSING"
elif test -f "$MISSING"; then
    rm -f "$DEST" "$MISSING"
else
    echo "No Bluetooth HID Remote cache snapshot exists" >&2
    exit 3
fi
