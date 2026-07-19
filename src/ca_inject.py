"""把 mitmproxy CA 证书注入 MuMu 系统 CA 存储。

Android 12+ 的 /system 是只读 ext4, 这里采用 tmpfs 覆盖挂载 /system/etc/security/cacerts
的方案 -- 把原证书 + mitmproxy CA 一起放到 tmpfs 里, 实现运行时注入而不破坏分区。
重启后 tmpfs 消失, 证书自动还原 (调用方需重新 setup)。
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from .config import Config
from .mumu_detect import MuMuInstance, _run_adb


def _android_subject_hash_old(cert: x509.Certificate) -> str:
    """计算 OpenSSL subject_hash_old (Android cacerts 命名规则)."""
    subject_der = cert.subject.public_bytes()
    md5 = hashlib.md5(subject_der).digest()
    h = int.from_bytes(md5[:4], "little")
    return format(h & 0xFFFFFFFF, "x")


def _build_android_cert_file(pem_path: str, cert: x509.Certificate) -> tuple[str, bytes]:
    name = _android_subject_hash_old(cert) + ".0"
    pem_text = cert.public_bytes(Encoding.PEM).decode()
    info_lines = [
        "Certificate:",
        "    Data:",
        f"        Version: 3 (0x2)",
        f"        Serial Number: {cert.serial_number}",
        f"        Signature Algorithm: {cert.signature_algorithm_oid._name}",
        f"        Issuer: {cert.issuer.rfc4514_string()}",
        "        Validity:",
        f"            Not Before: {getattr(cert, 'not_valid_before_utc', cert.not_valid_before)}",
        f"            Not After:  {getattr(cert, 'not_valid_after_utc', cert.not_valid_after)}",
        f"        Subject: {cert.subject.rfc4514_string()}",
    ]
    content = (pem_text + "\n" + "\n".join(info_lines) + "\n").encode()
    return name, content


def install_mitm_ca(inst: MuMuInstance, cfg: Config) -> bool:
    pem_path = cfg.mitm_ca_pem
    if not pem_path or not os.path.exists(pem_path):
        print(f"[ca_inject] mitmproxy CA 不存在: {pem_path}")
        print("            请先运行一次 mitmdump 以生成 ~/.mitmproxy/mitmproxy-ca-cert.pem")
        return False

    with open(pem_path, "rb") as f:
        cert = x509.load_pem_x509_certificate(f.read())
    name, content = _build_android_cert_file(pem_path, cert)

    # 写临时文件并 push
    tmp_local = os.path.join(os.path.dirname(pem_path), name)
    with open(tmp_local, "wb") as f:
        f.write(content)

    rc, _ = _run_adb(inst.adb_path, inst.serial, ["push", tmp_local, "/data/local/tmp/mitm_ca.0"])
    if rc != 0:
        print(f"[ca_inject] push 失败 rc={rc}")
        return False

    if not inst.rooted:
        print("[ca_inject] MuMu 未 root, 无法注入系统 CA")
        return False

    # tmpfs 覆盖挂载方案
    script = (
        "mkdir -p /data/local/tmp/cacerts_backup && "
        "cp /system/etc/security/cacerts/* /data/local/tmp/cacerts_backup/ 2>/dev/null; "
        "cp /data/local/tmp/mitm_ca.0 /data/local/tmp/cacerts_backup/" + name + " && "
        "chmod 644 /data/local/tmp/cacerts_backup/* && "
        "mount -t tmpfs tmpfs /system/etc/security/cacerts && "
        "cp /data/local/tmp/cacerts_backup/* /system/etc/security/cacerts/ && "
        "chmod 644 /system/etc/security/cacerts/* && "
        "chown root:root /system/etc/security/cacerts/* 2>/dev/null; "
        "restorecon -R /system/etc/security/cacerts/ 2>/dev/null; "
        "ls /system/etc/security/cacerts/" + name + " && echo MITM_CA_INSTALLED"
    )
    rc, out = _run_adb(inst.adb_path, inst.serial, ["shell", f"su -c '{script}'"])
    ok = "MITM_CA_INSTALLED" in out
    if not ok:
        print(f"[ca_inject] 注入脚本执行失败: rc={rc}")
        print(out)
    return ok
