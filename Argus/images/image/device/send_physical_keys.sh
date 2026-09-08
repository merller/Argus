#!/system/bin/sh
# Emit keys through the emulator's qwerty2 event node. InputReader classifies
# these events as physical/user input, unlike `adb shell input keyevent`.
set -eu

DEVICE=${ARGUS_KEYBOARD_DEVICE:-/dev/input/event13}

for key_code in "$@"; do
    sendevent "$DEVICE" 1 "$key_code" 1
    sendevent "$DEVICE" 0 0 0
    sendevent "$DEVICE" 1 "$key_code" 0
    sendevent "$DEVICE" 0 0 0
done
