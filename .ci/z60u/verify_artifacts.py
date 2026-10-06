#!/usr/bin/env python3
"""Check Z60U kernel outputs; does not replace Kleaf's ABI comparison.

Output names are from kernel/build 560e3751ab4d1d96e0db51e860f6437b41786c28:
  kleaf/constants.bzl:29-39 and impl/constants.bzl:33-37 (Image, symvers)
  kleaf/common_kernels.bzl:851-899 (modules_prepare archive in flat dist)
  kleaf/impl/kernel_build.bzl:578-582 (modules_prepare_outdir.tar.gz)
  build_utils.sh:800-805 (kernel_release in gki-info.txt)
The archive is read in memory, never extracted to the filesystem.
"""

import argparse
import hashlib
import json
import re
import struct
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from module_trust import verify_builtin_trust


EXPECTED_RELEASE = re.compile(r"6\.1\.90-android14-11(?:-[A-Za-z0-9_.+]+)*")
EXPECTED_STOCK_SYMBOLS = 7782
MAX_TEXT_BYTES = 4 * 1024 * 1024
REQUIRED_CONFIG = {
    "CONFIG_KSU": "y",
    "CONFIG_KSU_SUSFS": "y",
    "CONFIG_LTO_NONE": "y",
    "CONFIG_CFI_CLANG": "y",
    "CONFIG_MODVERSIONS": "y",
    "CONFIG_ARM64": "y",
    "CONFIG_ARM64_4K_PAGES": "y",
    "CONFIG_MODULES": "y",
    "CONFIG_MODULE_SIG": "y",
    "CONFIG_MODULE_SIG_PROTECT": "y",
    "CONFIG_MODULE_SIG_FORCE": "n",
    "CONFIG_MODULE_SIG_KEY": '"certs/signing_key.pem"',
    "CONFIG_SYSTEM_TRUSTED_KEYRING": "y",
    "CONFIG_SYSTEM_TRUSTED_KEYS": '"certs/z60u-stock-gki.pem"',
}


def read_text(path):
    size = path.stat().st_size
    if not 0 < size <= MAX_TEXT_BYTES:
        raise ValueError(f"{path.name}: empty or oversized text artifact ({size} bytes)")
    return path.read_text(encoding="utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_config(text):
    config = {}
    for number, line in enumerate(text.splitlines(), 1):
        enabled = re.fullmatch(r"(CONFIG_[A-Za-z0-9_]+)=(.*)", line)
        disabled = re.fullmatch(r"# (CONFIG_[A-Za-z0-9_]+) is not set", line)
        if enabled:
            key, value = enabled.groups()
        elif disabled:
            key, value = disabled.group(1), "n"
        elif line.startswith("CONFIG_"):
            raise ValueError(f"malformed config assignment at line {number}")
        else:
            continue
        if key in config:
            raise ValueError(f"duplicate config assignment: {key} at line {number}")
        config[key] = value
    if not config:
        raise ValueError("no kernel configuration entries")
    return config


def read_archive_config(path):
    # modules_prepare.bzl packages OUT_DIR with `tar ... -C OUT_DIR .`.
    with tarfile.open(path, "r:gz") as archive:
        matches = [entry for entry in archive if entry.name in (".config", "./.config")]
        if len(matches) != 1:
            raise ValueError("modules_prepare_outdir.tar.gz must contain exactly one .config")
        entry = matches[0]
        if not entry.isfile() or not 0 < entry.size <= MAX_TEXT_BYTES:
            raise ValueError("archive .config is not a nonempty regular text file")
        with archive.extractfile(entry) as source:
            return source.read(MAX_TEXT_BYTES + 1).decode("utf-8")


def compare_config(candidate, running):
    errors = []
    for key, expected in REQUIRED_CONFIG.items():
        actual = candidate.get(key, "n")
        if actual != expected:
            errors.append(f"{key}: expected {expected}, got {actual}")
        if key not in ("CONFIG_KSU", "CONFIG_KSU_SUSFS", "CONFIG_SYSTEM_TRUSTED_KEYS"):
            stock_value = running.get(key, "n")
            if stock_value != expected:
                errors.append(f"running configuration {key}: expected {expected}, got {stock_value}")
    changed = sorted(key for key in candidate.keys() | running.keys()
                     if candidate.get(key, "n") != running.get(key, "n"))
    return {
        "required": {key: candidate.get(key, "n") for key in REQUIRED_CONFIG},
        "changed_config_names": changed,
        "errors": errors,
    }


def parse_symvers(text):
    symbols = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = line.split()
        if (len(fields) not in (4, 5)
                or not re.fullmatch(r"0x[0-9a-fA-F]{8}", fields[0])
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", fields[1])
                or not fields[3].startswith("EXPORT_SYMBOL")):
            raise ValueError(f"malformed symvers row at line {number}")
        symbol = fields[1]
        if symbol in symbols:
            raise ValueError(f"duplicate symbol: {symbol} at line {number}")
        symbols[symbol] = fields[0].lower()
    if not symbols:
        raise ValueError("symvers contains no symbols")
    return symbols


def compare_symbols(stock, candidate):
    missing = sorted(stock.keys() - candidate.keys())
    changed = [{"symbol": name, "stock_crc": stock[name], "candidate_crc": candidate[name]}
               for name in sorted(stock.keys() & candidate.keys())
               if stock[name] != candidate[name]]
    extra = sorted(candidate.keys() - stock.keys())
    return {"stock_count": len(stock), "candidate_count": len(candidate),
            "missing": missing, "crc_mismatches": changed, "extra_exports": extra}


def check_image(path):
    size = path.stat().st_size
    with path.open("rb") as source:
        header = source.read(64)
    # common/Documentation/arm64/booting.rst: 64-byte LE header, magic at 56.
    if size <= 64 or len(header) != 64 or header[56:60] != b"ARM\x64":
        raise ValueError("Image is empty, truncated, or lacks the ARM64 Image header magic")
    image_size, flags = struct.unpack_from("<QQ", header, 16)
    if image_size < 64:
        raise ValueError("Image header has an invalid effective image size")
    # Effective size includes memory-only sections; it need not equal file size.
    return {"bytes": size, "effective_image_bytes": image_size, "flags": flags,
            "sha256": sha256(path)}


def check_release(text):
    releases = re.findall(r"^kernel_release=(\S+)\s*$", text, flags=re.MULTILINE)
    if len(releases) != 1 or not EXPECTED_RELEASE.fullmatch(releases[0]):
        raise ValueError("gki-info.txt must contain one kernel_release matching 6.1.90-android14-11")
    return {"kernel_release": releases[0]}


def verify(dist, stock_path, running_path, required_path=None):
    summary = {
        "schema_version": 1,
        "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "artifact integrity and selected compatibility checks; Kleaf ABI success is separately required",
        "status": "failed", "checks": {}, "errors": [],
    }

    def run_check(name, action):
        try:
            result = action()
            summary["checks"][name] = result
            summary["errors"].extend(f"{name}: {error}" for error in result.get("errors", []))
        except (OSError, ValueError, UnicodeError, tarfile.TarError, EOFError) as error:
            # Record the failure even when other outputs are missing or malformed.
            summary["checks"][name] = {"status": "failed", "error": str(error)}
            summary["errors"].append(f"{name}: {error}")

    def config_check():
        candidate = parse_config(read_archive_config(dist / "modules_prepare_outdir.tar.gz"))
        running = parse_config(read_text(running_path))
        top_level = dist / ".config"
        if top_level.exists() and parse_config(read_text(top_level)) != candidate:
            raise ValueError("top-level .config conflicts with modules_prepare archive .config")
        result = compare_config(candidate, running)
        if required_path is not None:
            required = parse_config(read_text(required_path))
            result["integration_required"] = required
            for key, expected in required.items():
                actual = candidate.get(key, "n")
                if actual != expected:
                    result["errors"].append(f"{key}: expected {expected}, got {actual}")
        result["source"] = "modules_prepare_outdir.tar.gz:.config"
        result["running_config_sha256"] = sha256(running_path)
        return result

    def symbols_check():
        stock = parse_symvers(read_text(stock_path))
        if len(stock) != EXPECTED_STOCK_SYMBOLS:
            raise ValueError(f"expected {EXPECTED_STOCK_SYMBOLS} official stock symbols, got {len(stock)}")
        candidate_path = dist / "vmlinux.symvers"
        result = compare_symbols(stock, parse_symvers(read_text(candidate_path)))
        result["stock_sha256"] = sha256(stock_path)
        result["candidate_sha256"] = sha256(candidate_path)
        result["errors"] = []
        if result["missing"]:
            result["errors"].append(f"{len(result['missing'])} stock exports missing")
        if result["crc_mismatches"]:
            result["errors"].append(f"{len(result['crc_mismatches'])} stock export CRCs changed")
        return result

    run_check("Image", lambda: check_image(dist / "Image"))
    run_check("kernel_release", lambda: check_release(read_text(dist / "gki-info.txt")))
    run_check("config", config_check)
    run_check("symbols", symbols_check)
    run_check("module_trust", lambda: verify_builtin_trust(dist / "vmlinux"))
    if not summary["errors"]:
        summary["status"] = "passed"
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--stock-symvers", required=True, type=Path)
    parser.add_argument("--running-config", required=True, type=Path)
    parser.add_argument("--required-config", type=Path)
    args = parser.parse_args()
    if not args.dist.is_dir():
        parser.error("--dist must be an existing build output directory")
    summary = verify(args.dist, args.stock_symvers, args.running_config, args.required_config)
    output = args.dist / "verification.json"
    try:
        output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"Cannot write verification.json: {error}", file=sys.stderr)
        return 1
    print(f"Artifact verification: {summary['status']} ({len(summary['errors'])} errors)")
    for error in summary["errors"]:
        print(error, file=sys.stderr)
    print(f"Summary: {output}")
    return 0 if summary["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
