#!/usr/bin/env bash
# Integrate pinned KernelSU/SUSFS into the exact ab12424481 common tree.
# This script does not build, package, or flash a boot image.
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "Usage: $0 ANDROID_REPO_ROOT AUDIT_OUTPUT_DIR" >&2
  exit 2
fi

ANDROID_ROOT=$(realpath "$1")
mkdir -p "$2"
AUDIT_DIR=$(realpath "$2")
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
COMMON="$ANDROID_ROOT/common"
COMMON_SHA=6f645aac97064a41a0bdcb18f1646427fd7ad6b9
KSU_SHA=932014ab5b2c9b74a3d11e2ec4d17dd10fc9442e
SUSFS_SHA=5727f79e3a7175cfb0e1a754fc2ed78eaf866237
KSU_URL=https://github.com/tiann/KernelSU.git
SUSFS_URL=https://gitlab.com/simonpunk/susfs4ksu.git
STAGE="$ANDROID_ROOT/.z60u-integration"

[[ -d "$COMMON/drivers" && -f "$COMMON/BUILD.bazel" ]]
[[ $(git -C "$COMMON" rev-parse HEAD) == "$COMMON_SHA" ]]
git -C "$COMMON" diff --quiet HEAD --
[[ ! -e "$COMMON/drivers/kernelsu" && ! -e "$COMMON/fs/susfs.c" ]]
[[ ! -e "$STAGE" ]]
mkdir "$STAGE"

# The common tree must keep its release/KMI configuration and ABI definition.
(
  cd "$COMMON"
  sha256sum Makefile scripts/setlocalversion arch/arm64/configs/gki_defconfig \
    android/abi_gki_aarch64.stg android/abi_gki_aarch64 > "$AUDIT_DIR/unchanged-inputs.sha256"
)

fetch_commit() {
  local url=$1 sha=$2 destination=$3
  git init --quiet "$destination"
  git -C "$destination" remote add origin "$url"
  git -C "$destination" -c advice.detachedHead=false fetch --quiet --depth=1 origin "$sha"
  git -C "$destination" -c advice.detachedHead=false checkout --quiet --detach FETCH_HEAD
  [[ $(git -C "$destination" rev-parse HEAD) == "$sha" ]]
}

fetch_commit "$KSU_URL" "$KSU_SHA" "$STAGE/KernelSU"
fetch_commit "$SUSFS_URL" "$SUSFS_SHA" "$STAGE/susfs4ksu"
KSU_PATCH="$STAGE/susfs4ksu/kernel_patches/KernelSU/10_enable_susfs_for_ksu.patch"

apply_checked() {
  local directory=$1 patchfile=$2
  git -C "$directory" apply --check "$patchfile"
  git -C "$directory" apply "$patchfile"
}

# Official SUSFS v2.3.0 explicitly targets official KernelSU v3.3.0.
apply_checked "$STAGE/KernelSU" "$KSU_PATCH"
apply_checked "$STAGE/KernelSU" "$SCRIPT_DIR/patches/0003-ksu-pinned-version-for-kleaf.patch"

# Same upstream common hooks, rebased to 6f645aac without fuzz. The only
# semantic port is VMA_PAD_START(vma) -> this baseline's existing vma->vm_end.
apply_checked "$COMMON" "$SCRIPT_DIR/patches/0001-susfs-2.3.0-common-6.1.90.patch"
cp "$STAGE/susfs4ksu/kernel_patches/fs/susfs.c" "$COMMON/fs/susfs.c"
cp "$STAGE/susfs4ksu/kernel_patches/include/linux/susfs.h" "$COMMON/include/linux/susfs.h"
cp "$STAGE/susfs4ksu/kernel_patches/include/linux/susfs_def.h" "$COMMON/include/linux/susfs_def.h"
# Backport upstream e9983b93254111f2b74391435a6fe3824c26fd8a's missing header.
apply_checked "$COMMON" "$SCRIPT_DIR/patches/0002-susfs-old-gki-security-header.patch"

# Stock system_dlkm modules are signed by the certificate embedded in the
# original ab12424481 kernel. Trust that public certificate in addition to the
# new build's own module key; do not disable GKI protected-symbol enforcement.
# A Kconfig default keeps the canonical gki_defconfig/check_defconfig intact.
python3 "$SCRIPT_DIR/module_trust.py" --certificate "$SCRIPT_DIR/stock-gki.pem"
[[ ! -e "$COMMON/certs/z60u-stock-gki.pem" ]]
cp "$SCRIPT_DIR/stock-gki.pem" "$COMMON/certs/z60u-stock-gki.pem"
apply_checked "$COMMON" "$SCRIPT_DIR/patches/0004-stock-gki-module-trust.patch"

# Use ordinary source files within the common Bazel package. Do not rely on
# a symlink reaching outside the package or on Git/network inside the sandbox.
mkdir "$COMMON/drivers/kernelsu"
cp -aL "$STAGE/KernelSU/kernel/." "$COMMON/drivers/kernelsu/"
test -s "$COMMON/drivers/kernelsu/include/uapi/app_profile.h"
if find -L "$COMMON/drivers/kernelsu" -type l -print -quit | grep -q .; then
  echo "KernelSU contains a dangling source symlink; stopping before compilation." >&2
  exit 1
fi
python3 - "$COMMON" <<'PY'
import pathlib
import re
import sys

common = pathlib.Path(sys.argv[1])
ksu = common / "drivers/kernelsu"
uapi_headers = set()
for source in ksu.rglob("*"):
    if source.suffix in (".c", ".h"):
        uapi_headers.update(re.findall(r'^\s*#\s*include\s+"(uapi/[^\"]+)"', source.read_text(), re.M))
assert uapi_headers, "No KernelSU UAPI includes discovered"
missing = [header for header in sorted(uapi_headers) if not (ksu / "include" / header).is_file()]
assert not missing, f"Missing KernelSU UAPI headers: {missing}"
print(f"Verified {len(uapi_headers)} KernelSU UAPI header dependencies before compilation")
makefile = common / "drivers/Makefile"
kconfig = common / "drivers/Kconfig"
make_text = makefile.read_text()
config_text = kconfig.read_text()
assert "kernelsu" not in make_text
assert "drivers/kernelsu/Kconfig" not in config_text
assert config_text.count("\nendmenu\n") == 1
makefile.write_text(make_text + '\nobj-$(CONFIG_KSU) += kernelsu/\n')
kconfig.write_text(config_text.replace('\nendmenu\n', '\nsource "drivers/kernelsu/Kconfig"\n\nendmenu\n'))
PY

# These options already default to y in the pinned upstream Kconfig. Keeping
# gki_defconfig canonical preserves the existing check_defconfig validation.
# The workflow's verifier must inspect the *built* .config for these values.
cat > "$AUDIT_DIR/required-config.txt" <<'EOF'
CONFIG_KSU=y
CONFIG_KSU_SUSFS=y
CONFIG_KSU_SUSFS_SUS_PATH=y
CONFIG_KSU_SUSFS_SUS_MOUNT=y
CONFIG_KSU_SUSFS_SUS_KSTAT=y
CONFIG_KSU_SUSFS_SPOOF_UNAME=y
CONFIG_KSU_SUSFS_ENABLE_LOG=y
CONFIG_KSU_SUSFS_HIDE_KSU_SUSFS_SYMBOLS=y
CONFIG_KSU_SUSFS_SPOOF_CMDLINE_OR_BOOTCONFIG=y
CONFIG_KSU_SUSFS_OPEN_REDIRECT=y
CONFIG_KSU_SUSFS_SUS_MAP=y
CONFIG_MODULE_SIG=y
CONFIG_MODULE_SIG_PROTECT=y
# CONFIG_MODULE_SIG_FORCE is not set
CONFIG_MODULE_SIG_KEY="certs/signing_key.pem"
CONFIG_SYSTEM_TRUSTED_KEYRING=y
CONFIG_SYSTEM_TRUSTED_KEYS="certs/z60u-stock-gki.pem"
EOF

if find "$COMMON" "$STAGE/KernelSU" -name '*.rej' -print -quit | grep -q .; then
  echo "Rejected patch files exist; stopping." >&2
  exit 1
fi
(
  cd "$COMMON"
  sha256sum --check "$AUDIT_DIR/unchanged-inputs.sha256"
)
git -C "$COMMON" diff --binary > "$AUDIT_DIR/common-integration.diff"
git -C "$STAGE/KernelSU" diff --binary > "$AUDIT_DIR/kernelsu-integration.diff"
cp "$KSU_PATCH" "$AUDIT_DIR/upstream-ksu-susfs.patch"
python3 - "$AUDIT_DIR" "$SCRIPT_DIR" "$KSU_PATCH" <<'PY'
import hashlib
import json
import pathlib
import sys

audit, scripts, upstream_patch = map(pathlib.Path, sys.argv[1:])
patches = list(sorted((scripts / "patches").glob("*.patch"))) + [upstream_patch]
lock = {
    "common": {"url": "https://android.googlesource.com/kernel/common", "commit": "6f645aac97064a41a0bdcb18f1646427fd7ad6b9", "build_id": "12424481"},
    "kernelsu": {"url": "https://github.com/tiann/KernelSU", "commit": "932014ab5b2c9b74a3d11e2ec4d17dd10fc9442e", "tag": "v3.3.0", "version_code": 32601, "uapi": 2, "manager": "official KernelSU v3.3.0"},
    "susfs": {"url": "https://gitlab.com/simonpunk/susfs4ksu", "commit": "5727f79e3a7175cfb0e1a754fc2ed78eaf866237", "version": "2.3.0"},
    "backports": ["simonpunk/susfs4ksu@e9983b93254111f2b74391435a6fe3824c26fd8a: include linux/security.h"],
    "patch_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in patches},
    "kernel_release_modified": False,
    "abi_check_disabled": False,
    "init_boot_modified": False,
    "stock_module_trust": {
        "certificate_der_sha256": "33300657d6627381fd3fd53c6348a984685075f5d80901ef71ebbffc8ea1c4aa",
        "certificate_pem_sha256": hashlib.sha256((scripts / "stock-gki.pem").read_bytes()).hexdigest(),
        "destination": "certs/z60u-stock-gki.pem",
        "mechanism": "CONFIG_SYSTEM_TRUSTED_KEYS; additional built-in public certificate",
        "module_sig_protect_disabled": False,
        "private_key_imported": False,
    },
}
(audit / "dependencies.lock.json").write_text(json.dumps(lock, indent=2) + "\n")
PY

echo "Pinned KernelSU 3.3.0 + SUSFS 2.3.0 integration complete; build/ABI checks are still required."
