"""局域网设备配对、TLS 身份和 mDNS 发现管理。"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import socket
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from .database import Database, DomainError, utc_now

# DNS-SD 的服务类型标签（不含开头下划线）最多 15 字节。
# `component-library` 超出限制，因此协议统一使用较短的 componentlib。
MDNS_SERVICE_TYPE = "_componentlib._tcp.local."


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class ConnectionManager:
    """管理手机等外部设备的身份，不持有或复制库存业务规则。"""

    def __init__(self, database: Database, directory: Path, port: int = 17636) -> None:
        self.database = database
        self.directory = directory / "connection"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.port = port
        self.server_id = self._load_server_id()
        self.service_name = f"component-library-{self.server_id[:8]}.local"
        self.ca_path = self.directory / "ca.pem"
        self.cert_path = self.directory / "server.pem"
        self.key_path = self.directory / "server-key.pem"
        self._pairing_code = ""
        self._pairing_expires = datetime.now(UTC)
        self._lock = threading.RLock()
        self._zeroconf: Any = None
        self._service_info: Any = None
        self.ensure_certificate()
        self.refresh_pairing()

    def _load_server_id(self) -> str:
        path = self.directory / "server-id"
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
        value = str(uuid.uuid4())
        path.write_text(value, encoding="utf-8")
        return value

    @staticmethod
    def addresses() -> list[str]:
        """返回适合展示和写入证书的非回环 IPv4 地址。"""

        found: set[str] = set()
        try:
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                address = str(item[4][0])
                if not ipaddress.ip_address(address).is_loopback:
                    found.add(address)
        except OSError:
            pass
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("192.0.2.1", 9))
            found.add(probe.getsockname()[0])
            probe.close()
        except OSError:
            pass
        return sorted(found)

    def ensure_certificate(self) -> None:
        """创建持久本地 CA，并为当前局域网地址签发服务器证书。"""

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        now = datetime.now(UTC)
        ca_key_path = self.directory / "ca-key.pem"
        if not self.ca_path.exists() or not ca_key_path.exists():
            ca_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Component Library Local CA")])
            ca_cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(subject)
                .public_key(ca_key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(days=1))
                .not_valid_after(now + timedelta(days=3650))
                .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                .sign(ca_key, hashes.SHA256())
            )
            ca_key_path.write_bytes(
                ca_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            self.ca_path.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
        loaded_ca_key = cast(
            rsa.RSAPrivateKey,
            serialization.load_pem_private_key(ca_key_path.read_bytes(), password=None),
        )
        ca_cert = x509.load_pem_x509_certificate(self.ca_path.read_bytes())
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        sans: list[x509.GeneralName] = [
            x509.DNSName(self.service_name),
            x509.DNSName("localhost"),
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        ]
        sans.extend(x509.IPAddress(ipaddress.ip_address(address)) for address in self.addresses())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, self.service_name)])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=397))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(loaded_ca_key, hashes.SHA256())
        )
        self.key_path.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        ca_key_path.chmod(0o600)
        self.key_path.chmod(0o600)
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    def refresh_pairing(self) -> dict[str, Any]:
        """生成十分钟有效的一次性配对码及二维码载荷。"""

        with self._lock:
            self._pairing_code = f"{secrets.randbelow(1_000_000):06d}"
            self._pairing_expires = datetime.now(UTC) + timedelta(minutes=10)
        return self.pairing_payload()

    def pairing_payload(self) -> dict[str, Any]:
        """返回紧凑配对载荷，避免把完整 CA 塞入高密度二维码。"""

        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        ca_bytes = self.ca_path.read_bytes()
        server_bytes = self.cert_path.read_bytes()
        ca_der = x509.load_pem_x509_certificate(ca_bytes).public_bytes(serialization.Encoding.DER)
        server_der = x509.load_pem_x509_certificate(server_bytes).public_bytes(
            serialization.Encoding.DER
        )
        return {
            "scheme": "component-library-pairing",
            "protocol": 2,
            "server_id": self.server_id,
            "service_name": self.service_name,
            "addresses": self.addresses(),
            "port": self.port,
            "pairing_code": self._pairing_code,
            "expires_at": self._pairing_expires.isoformat(),
            "ca_sha256": hashlib.sha256(ca_der).hexdigest(),
            "server_cert_sha256": hashlib.sha256(server_der).hexdigest(),
        }

    def pairing_qr_png(self) -> bytes:
        """把配对载荷编码为 PNG，供桌面设置页直接展示。"""

        import io

        import qrcode

        image = qrcode.make(json.dumps(self.pairing_payload(), ensure_ascii=False, separators=(",", ":")))
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def pair(self, code: str, name: str, device_type: str, client_version: str) -> dict[str, Any]:
        """消费一次性配对码并返回只展示一次的设备令牌。"""

        with self._lock:
            if datetime.now(UTC) > self._pairing_expires or not secrets.compare_digest(
                code, self._pairing_code
            ):
                raise DomainError("UNAUTHORIZED", "配对码无效或已过期")
            self._pairing_code = ""
        token = secrets.token_urlsafe(32)
        device_id = str(uuid.uuid4())
        with self.database.write() as connection:
            connection.execute(
                """INSERT INTO paired_devices(
                id,name,device_type,token_hash,client_version,created_at,last_seen_at)
                VALUES(?,?,?,?,?,?,?)""",
                (device_id, name.strip(), device_type, _token_hash(token), client_version, utc_now(), utc_now()),
            )
        return {
            "device_id": device_id,
            "device_token": token,
            "server_id": self.server_id,
            "service_name": self.service_name,
            "ca_pem": base64.b64encode(self.ca_path.read_bytes()).decode("ascii"),
        }

    def authenticate(self, token: str) -> str | None:
        """验证仍存在的设备令牌并更新最后在线时间。"""

        with self.database.write() as connection:
            row = connection.execute(
                "SELECT id FROM paired_devices WHERE token_hash=? AND revoked_at IS NULL",
                (_token_hash(token),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE paired_devices SET last_seen_at=? WHERE id=?", (utc_now(), row["id"])
            )
            return str(row["id"])

    def list_devices(self) -> list[dict[str, Any]]:
        with self.database.read() as connection:
            devices = [
                dict(row)
                for row in connection.execute(
                    """SELECT id,name,device_type,client_version,created_at,last_seen_at,revoked_at
                    FROM paired_devices WHERE revoked_at IS NULL ORDER BY created_at DESC"""
                )
            ]
        now = datetime.now(UTC)
        for device in devices:
            last_seen = device.get("last_seen_at")
            try:
                seen_at = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
                recently_seen = now - seen_at <= timedelta(seconds=15)
            except (TypeError, ValueError):
                recently_seen = False
            device["online"] = bool(recently_seen and not device.get("revoked_at"))
        return devices

    def available_android_devices(self) -> list[dict[str, Any]]:
        """返回当前有心跳的 Android 摄像头终端。"""

        return [
            device
            for device in self.list_devices()
            if device["device_type"] == "ANDROID_PHONE" and device["online"]
        ]

    def update_client_version(self, device_id: str, client_version: str) -> None:
        """心跳时刷新已配对设备版本，升级 APK 不需要重新配对。"""

        version = client_version.strip()[:32]
        if not version:
            return
        with self.database.write() as connection:
            connection.execute(
                "UPDATE paired_devices SET client_version=?,last_seen_at=? "
                "WHERE id=? AND revoked_at IS NULL",
                (version, utc_now(), device_id),
            )

    def delete(self, device_id: str) -> None:
        """永久删除配对设备及其令牌；删除后手机必须重新配对。"""

        with self.database.write() as connection:
            if not connection.execute(
                "DELETE FROM paired_devices WHERE id=?",
                (device_id,),
            ).rowcount:
                raise DomainError("NOT_FOUND", "设备不存在或已经删除")

    def start_discovery(self) -> None:
        """广播 HTTPS 服务；缺少可用局域网地址时保持 API 可用但不广播。"""

        addresses = self.addresses()
        if not addresses:
            return
        from zeroconf import ServiceInfo, Zeroconf

        self._zeroconf = Zeroconf()
        self._service_info = ServiceInfo(
            MDNS_SERVICE_TYPE,
            f"{self.server_id}.{MDNS_SERVICE_TYPE}",
            addresses=[socket.inet_aton(address) for address in addresses],
            port=self.port,
            properties={"server_id": self.server_id, "api_major": "1"},
            server=f"{self.service_name}.",
        )
        self._zeroconf.register_service(self._service_info)

    def stop_discovery(self) -> None:
        if self._zeroconf and self._service_info:
            self._zeroconf.unregister_service(self._service_info)
        if self._zeroconf:
            self._zeroconf.close()
        self._zeroconf = None
        self._service_info = None
