#!/system/bin/sh
# Ephemeral deployment for a locked userdebug emulator. Every bind mount is
# removed by a reboot, so the original system image remains untouched.
set -eu

STAGE=/data/local/tmp/argus_restricted

# A bind mount keeps the source inode's SELinux label. Zygote may not read
# shell_data_file, so label staged framework/runtime artifacts as system files.
chcon -R u:object_r:system_file:s0 "$STAGE"

mount_one() {
    src="$1"
    dst="$2"
    if [ ! -f "$src" ] || [ ! -f "$dst" ]; then
        echo "missing bind endpoint: $src -> $dst" >&2
        exit 1
    fi
    mount --bind "$src" "$dst"
    echo "bound $dst"
}

mount_one "$STAGE/framework/framework.jar" /system/framework/framework.jar

for src in "$STAGE"/framework/boot*.vdex; do
    mount_one "$src" "/system/framework/${src##*/}"
done

for src in "$STAGE"/framework/x86_64/*; do
    mount_one "$src" "/system/framework/x86_64/${src##*/}"
done

for src in "$STAGE"/framework/x86/*; do
    mount_one "$src" "/system/framework/x86/${src##*/}"
done

mount_one "$STAGE/lib/libandroid_runtime.so" /system/lib/libandroid_runtime.so
mount_one "$STAGE/lib64/libandroid_runtime.so" /system/lib64/libandroid_runtime.so
mount_one "$STAGE/art/lib/libart.so" /apex/com.android.art/lib/libart.so
mount_one "$STAGE/art/lib64/libart.so" /apex/com.android.art/lib64/libart.so

# Restart Android userspace so Zygote maps the new framework and native code.
stop
start
