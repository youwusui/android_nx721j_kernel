#!/usr/bin/env python3
"""Verify the pinned stock public certificate in the built-in kernel keyring.

Only standard-library parsing is used in CI. Certificate provenance and stock
module PKCS#7 verification are recorded in module-trust/verification.json in the
device audit. No private signing key is imported or replaced.
"""
import argparse
import hashlib
import json
import ssl
import struct
from pathlib import Path


STOCK_CERT_SHA256 = "33300657d6627381fd3fd53c6348a984685075f5d80901ef71ebbffc8ea1c4aa"
STOCK_CERT_BYTES = 1357
CERTIFICATE_PATH = Path(__file__).with_name("stock-gki.pem")
SYMBOL_NAMES = {"system_certificate_list", "system_certificate_list_size", "module_cert_size"}


def public_certificate(path=CERTIFICATE_PATH):
    text = path.read_text(encoding="ascii")
    if (text.count("-----BEGIN CERTIFICATE-----") != 1
            or text.count("-----END CERTIFICATE-----") != 1
            or "PRIVATE KEY" in text):
        raise ValueError("expected one public X.509 certificate, without private key material")
    der = ssl.PEM_cert_to_DER_cert(text)
    if len(der) != STOCK_CERT_BYTES or hashlib.sha256(der).hexdigest() != STOCK_CERT_SHA256:
        raise ValueError("stock module certificate does not match the pinned device evidence")
    return der


def builtin_certificates(path):
    """Read certs/system_certificates.S symbols from an AArch64 ELF64 vmlinux.

    Symbols can have size zero in assembly; the two u64 size symbols supply
    authoritative list boundaries. This does not search arbitrary Image bytes.
    """
    file_size = path.stat().st_size
    with path.open("rb") as source:
        def read(offset, size):
            if offset < 0 or size < 0 or offset + size > file_size:
                raise ValueError("ELF range exceeds file boundaries")
            source.seek(offset)
            value = source.read(size)
            if len(value) != size:
                raise ValueError("truncated ELF data")
            return value

        header = read(0, 64)
        if header[:6] != b"\x7fELF\x02\x01" or struct.unpack_from("<H", header, 18)[0] != 183:
            raise ValueError("vmlinux must be little-endian AArch64 ELF64")
        shoff = struct.unpack_from("<Q", header, 40)[0]
        shentsize, shnum = struct.unpack_from("<HH", header, 58)
        if shentsize != 64 or not 0 < shnum < 4096:
            raise ValueError("unsupported ELF section table")
        sections = [struct.unpack("<IIQQQQIIQQ", read(shoff + index * 64, 64))
                    for index in range(shnum)]
        symbols = {}
        for section in sections:
            if section[1] != 2:  # SHT_SYMTAB
                continue
            if section[9] != 24 or section[5] % 24 or section[6] >= shnum:
                raise ValueError("invalid ELF symbol table")
            if section[5] > 64 * 1024 * 1024:
                raise ValueError("oversized ELF symbol table")
            strings_section = sections[section[6]]
            if strings_section[1] != 3 or strings_section[5] > 64 * 1024 * 1024:
                raise ValueError("invalid ELF symbol string table")
            strings = read(strings_section[4], strings_section[5])
            table = read(section[4], section[5])
            for at in range(0, len(table), 24):
                name_offset, _, _, shndx, address, _ = struct.unpack_from("<IBBHQQ", table, at)
                if name_offset >= len(strings):
                    raise ValueError("ELF symbol name exceeds string table")
                end = strings.find(b"\0", name_offset)
                if end < 0:
                    raise ValueError("unterminated ELF symbol name")
                name = strings[name_offset:end].decode("ascii", errors="replace")
                if name in SYMBOL_NAMES:
                    if name in symbols or not 0 < shndx < shnum:
                        raise ValueError("duplicate or undefined certificate-list symbol")
                    symbols[name] = (sections[shndx], address)
        if symbols.keys() != SYMBOL_NAMES:
            raise ValueError("vmlinux lacks required built-in certificate-list symbols")

        def symbol_data(name, size):
            section, address = symbols[name]
            relative = address - section[3]
            if section[1] == 8 or relative < 0 or relative + size > section[5]:
                raise ValueError("certificate-list symbol is outside its file-backed section")
            return read(section[4] + relative, size)

        total = struct.unpack("<Q", symbol_data("system_certificate_list_size", 8))[0]
        module_size = struct.unpack("<Q", symbol_data("module_cert_size", 8))[0]
        if not 0 < module_size <= total <= 1024 * 1024:
            raise ValueError("invalid compiled-in certificate-list sizes")
        return symbol_data("system_certificate_list", total), module_size


def verify_builtin_trust(vmlinux, certificate=CERTIFICATE_PATH):
    der = public_certificate(certificate)
    cert_list, module_size = builtin_certificates(vmlinux)
    # Retain the independently generated candidate module key; add stock trust
    # through CONFIG_SYSTEM_TRUSTED_KEYS, after the module signing certificate.
    additional = cert_list[module_size:]
    if additional.count(der) != 1:
        raise ValueError("stock certificate missing or duplicated in additional system certificate list")
    if der in cert_list[:module_size]:
        raise ValueError("stock certificate replaced the candidate module-signing certificate")
    return {"stock_certificate_sha256": STOCK_CERT_SHA256,
            "stock_certificate_bytes": len(der),
            "compiled_certificate_list_bytes": len(cert_list),
            "candidate_signing_certificate_bytes": module_size,
            "additional_certificate_bytes": len(additional),
            "stock_certificate_in_additional_builtin_keyring": True,
            "errors": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--certificate", type=Path, default=CERTIFICATE_PATH)
    parser.add_argument("--vmlinux", type=Path)
    args = parser.parse_args()
    if args.vmlinux:
        report = verify_builtin_trust(args.vmlinux, args.certificate)
    else:
        der = public_certificate(args.certificate)
        report = {"public_certificate_sha256": hashlib.sha256(der).hexdigest(), "bytes": len(der)}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
