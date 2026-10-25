"""A short-lived self-signed server certificate scoped to localhost, never a CA."""

import argparse
import ipaddress
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def generate(directory: Path) -> Path:
    os.umask(0o077)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    certificate_file, key_file = directory / "localhost.crt", directory / "localhost.key"
    now = datetime.now(UTC)
    if certificate_file.exists() and key_file.exists():
        certificate = x509.load_pem_x509_certificate(certificate_file.read_bytes())
        key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
        matching = certificate.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        ) == key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        if not matching:
            raise ValueError("Локальные сертификат и ключ не совпадают")
        if certificate.not_valid_after_utc > now + timedelta(days=7):
            return certificate_file
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Deadline Assistant localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address("::1")),
                ]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    certificate_file.chmod(0o600)
    return certificate_file


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Создать TLS-сертификат только для localhost")
    parser.add_argument("directory", type=Path)
    path = generate(parser.parse_args().directory)
    print(f"Локальный сертификат готов: {path}")
