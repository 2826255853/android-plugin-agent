#!/bin/sh
# Build the trusted runtime from Alpine 3.23 packages on an Alpine x86_64 machine.
set -eu
[ "$#" = 1 ] || { echo 'Usage: sh prepare-stage-c4.sh NEW_OUTPUT_DIRECTORY' >&2; exit 2; }
[ "$(uname -m)" = x86_64 ]
[ "$(id -u)" = 0 ]
output=$1
temp=$(mktemp -d)
trap 'rm -rf "$temp"' EXIT
root=$temp/root
mkdir -p "$root/etc/apk/keys"
cp /etc/apk/keys/* "$root/etc/apk/keys/"
mirror=${ALPINE_C4_MIRROR:-https://dl-cdn.alpinelinux.org/alpine/v3.23}
printf '%s\n' "$mirror/main" "$mirror/community" > "$temp/repositories"
apk --root "$root" --initdb --no-cache --keys-dir /etc/apk/keys --repositories-file "$temp/repositories" \
    add alpine-baselayout busybox busybox-binsh python3 nodejs chromium font-dejavu linux-virt

test -x "$root/usr/bin/chromium-browser"
cp "$root/boot/vmlinuz-virt" "$temp/vmlinuz"
rm -rf "$root/boot" "$root/lib/modules" "$root/var/cache" "$root/dev" "$root/proc" "$root/sys" "$root/tmp"
mkdir -p "$root/dev" "$root/proc" "$root/sys/fs/cgroup" "$root/input" "$root/sandbox" "$root/work" "$root/scratch"
mknod "$root/dev/console" c 5 1
chmod 0600 "$root/dev/console"
mknod -m 0666 "$root/dev/null" c 1 3
mknod -m 0666 "$root/dev/urandom" c 1 9
ln -s /work/.tmp "$root/tmp"
(cd "$root" && find . -print0 | cpio -0 -o -H newc) > "$temp/rootfs.cpio"
gzip -6 "$temp/rootfs.cpio"
kernel=$(sha256sum "$temp/vmlinuz")
rootfs=$(sha256sum "$temp/rootfs.cpio.gz")
printf '{"vmlinuz":"%s", "rootfs.cpio.gz":"%s"}\n' "${kernel%% *}" "${rootfs%% *}" > "$temp/image.json"
mkdir "$output"
cp "$temp/vmlinuz" "$temp/rootfs.cpio.gz" "$temp/image.json" "$output/"
