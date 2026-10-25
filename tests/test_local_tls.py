import asyncio
import importlib.util
import os
import signal
import socket
import ssl
import sys
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID
from pydantic import ValidationError

from deadline_assistant.config import Settings

SPEC = importlib.util.spec_from_file_location(
    "local_tls_generator", Path(__file__).resolve().parents[1] / "scripts/generate_local_tls.py"
)
GENERATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GENERATOR)


def test_local_certificate_is_server_only_and_reused(tmp_path):
    path = GENERATOR.generate(tmp_path / "tls")
    original = path.read_bytes()
    assert GENERATOR.generate(path.parent).read_bytes() == original
    certificate = x509.load_pem_x509_certificate(original)
    assert not certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not certificate.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign
    assert certificate.extensions.get_extension_for_class(
        x509.ExtendedKeyUsage
    ).value == x509.ExtendedKeyUsage(
        [
            ExtendedKeyUsageOID.SERVER_AUTH,
        ]
    )
    assert certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName) == ["localhost"]
    assert (path.parent / "localhost.key").stat().st_mode & 0o777 == 0o600


def test_tls_configuration_requires_both_paths(settings):
    with pytest.raises(ValidationError, match="задаются вместе"):
        Settings(_env_file=None, **{**settings.model_dump(), "tls_cert_file": "/missing.crt"})


async def test_real_https_process_with_verified_local_certificate(database, settings, tmp_path):
    path = await asyncio.to_thread(GENERATOR.generate, tmp_path / "tls")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    environment = {
        **os.environ,
        "APP_ENV": "development",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "PUBLIC_BASE_URL": f"https://localhost:{port}",
        "DATABASE_URL": settings.database_url.get_secret_value(),
        "TLS_CERT_FILE": str(path),
        "TLS_KEY_FILE": str(path.parent / "localhost.key"),
        "TELEGRAM_BOT_TOKEN": "",
        "BOT_TOKEN": "",
        "LOWROUTER_API_KEY": "",
        "LLM_API_KEY": "",
        "TELEGRAM_API_ID": "",
        "TELEGRAM_API_HASH": "",
        "SESSION_ENCRYPTION_KEY": "",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "deadline_bot",
        "serve",
        env=environment,
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"https://localhost:{port}",
            trust_env=False,
            verify=ssl.create_default_context(cafile=path),
        ) as client:
            async with asyncio.timeout(10):
                while True:
                    try:
                        response = await client.get("/health/ready")
                        if response.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.05)
            assert (await client.get("/connect")).status_code == 200
            response = await client.post("/api/auth/password", json={"password": "synthetic"})
            assert response.status_code == 401
        # A client without explicit trust must reject the local self-signed leaf.
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(httpx.ConnectError):
                await client.get(f"https://localhost:{port}/health/ready")
        process.send_signal(signal.SIGTERM)
        output, _ = await asyncio.wait_for(process.communicate(), 10)
        assert process.returncode in (0, -signal.SIGTERM)
        assert "Приложение остановлено" in output.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
