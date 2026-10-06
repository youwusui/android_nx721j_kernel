#!/usr/bin/env bash
set -euo pipefail

kernel_root=${1:?Android kernel checkout directory is required}
audit_dir=${2:?Audit output directory is required}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mkdir -p "$kernel_root" "$audit_dir" "$kernel_root/bin"

echo '2ac9cd05d34edd8dd2240f8642f17efd210f0f4e9020af6734ef599127a07dc1  manifest_12424481.xml' |
  (cd "$script_dir" && sha256sum --check --strict)

curl --fail --location --retry 3 \
  https://storage.googleapis.com/git-repo-downloads/repo -o "$kernel_root/bin/repo"
chmod +x "$kernel_root/bin/repo"
sha256sum "$kernel_root/bin/repo" > "$audit_dir/repo-launcher-sha256.txt"
export PATH="$kernel_root/bin:$PATH"
export GIT_TERMINAL_PROMPT=0
git config --global user.name 'Z60U kernel build'
git config --global user.email 'kernel-build@users.noreply.github.com'

cd "$kernel_root"
repo init -u https://android.googlesource.com/kernel/manifest \
  -b common-android14-6.1-2024-08 --depth=1 --no-clone-bundle
# The 2024 manifest's branch hints have since been removed upstream. Keep every
# project SHA intact, but fetch those commits instead of nonexistent branches.
python3 - "$script_dir/manifest_12424481.xml" .repo/manifests/manifest_12424481.xml <<'PY'
import sys
import xml.etree.ElementTree as ET

tree = ET.parse(sys.argv[1])
root = tree.getroot()
for project in root.findall("project"):
    assert len(project.attrib["revision"]) == 40
    project.attrib.pop("upstream", None)
    project.attrib.pop("dest-branch", None)
    project.set("clone-depth", "1")
for superproject in root.findall("superproject"):
    root.remove(superproject)
tree.write(sys.argv[2], encoding="UTF-8", xml_declaration=True)
PY
cp .repo/manifests/manifest_12424481.xml "$audit_dir/effective-manifest.xml"
repo init -m manifest_12424481.xml --depth=1 --no-clone-bundle
repo sync -c -j4 --no-clone-bundle --no-tags --fail-fast 2>&1 | tee "$audit_dir/repo-sync.log"

test "$(git -C common rev-parse HEAD)" = 6f645aac97064a41a0bdcb18f1646427fd7ad6b9
test "$(git -C build/kernel rev-parse HEAD)" = 560e3751ab4d1d96e0db51e860f6437b41786c28
test "$(git -C prebuilts/clang/host/linux-x86 rev-parse HEAD)" = 7775eb113f960bc69a780b621d03a715914d4bca
repo manifest -r -o "$audit_dir/resolved-manifest.xml"
git -C common show HEAD:android/abi_gki_aarch64.stg > "$audit_dir/stock-abi.stg"
sha256sum common/android/abi_gki_aarch64.stg > "$audit_dir/stock-abi-sha256.txt"
df -h | tee "$audit_dir/disk-after-sync.txt"
du -sh .repo common prebuilts | tee "$audit_dir/source-sizes.txt"
free_kb=$(df --output=avail -k . | tail -n 1)
if (( free_kb < 12 * 1024 * 1024 )); then
  echo 'Less than 12 GiB left for build output; stopping before compilation.' >&2
  exit 1
fi
