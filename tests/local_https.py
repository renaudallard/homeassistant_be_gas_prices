# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Local servers for the tests that fetch, under names of their own.

The fetch helpers hold every address to https and to a public host, so a
test server on 127.0.0.1 is reached by name: the session resolves the names
below to it and trusts only a certificate authority made for the run.
:func:`device` stands for an appliance on the household's network, a plain
http server that logs every request it gets.
"""

from __future__ import annotations

import datetime as dt
import socket
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import aiohttp
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.test_utils import TestServer
from aiohttp.typedefs import Handler
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# The names a site answers for; the first is the one :meth:`Site.url` uses.
NAMES = ("card.test", "store.test")


@cache
def _pems() -> tuple[str, bytes, bytes]:
    """A certificate authority, and a certificate it signs for NAMES with its
    key, all PEM: the CA as text, the other two as bytes."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "be_gas_prices tests")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NAMES[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name) for name in NAMES]), False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    return (
        ca.public_bytes(serialization.Encoding.PEM).decode("ascii"),
        leaf.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def client_context() -> ssl.SSLContext:
    """A client context that trusts the test authority and nothing else."""
    return ssl.create_default_context(cadata=_pems()[0])


def _server_context(folder: Path) -> ssl.SSLContext:
    # The ssl module loads a certificate and its key from files only.
    _ca, leaf, key = _pems()
    (folder / "leaf.pem").write_bytes(leaf)
    (folder / "leaf.key").write_bytes(key)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(folder / "leaf.pem", folder / "leaf.key")
    return context


class _Names(AbstractResolver):
    """Resolve NAMES to 127.0.0.1 and refuse every other name."""

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        if host not in NAMES:
            raise OSError(f"no such test name: {host}")
        return [
            ResolveResult(
                hostname=host,
                host="127.0.0.1",
                port=port,
                family=socket.AF_INET,
                proto=0,
                flags=socket.AI_NUMERICHOST,
            )
        ]

    async def close(self) -> None:
        return None


@dataclass
class Site:
    """An https server answering for NAMES, a session that reaches it, and
    the request lines it got, as "GET /path?query"."""

    session: aiohttp.ClientSession
    port: int
    asked: list[str] = field(default_factory=list)

    def url(self, path: str, name: str = NAMES[0]) -> str:
        return f"https://{name}:{self.port}{path}"


@asynccontextmanager
async def https_site(app: web.Application, folder: Path) -> AsyncIterator[Site]:
    """Serve ``app`` over https on 127.0.0.1 for NAMES. ``folder`` holds the
    certificate files while it runs."""
    asked: list[str] = []

    @web.middleware
    async def log(request: web.Request, handler: Handler) -> web.StreamResponse:
        asked.append(f"{request.method} {request.path_qs}")
        return await handler(request)

    app.middlewares.append(log)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server(ssl=_server_context(folder))
    connector = aiohttp.TCPConnector(resolver=_Names(), ssl=client_context())
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            assert server.port is not None
            yield Site(session, server.port, asked)
    finally:
        await server.close()


@dataclass
class Device:
    """A plain http server on 127.0.0.1 that logs every request it gets."""

    url: str
    asked: list[str]


@asynccontextmanager
async def device() -> AsyncIterator[Device]:
    asked: list[str] = []

    async def answer(request: web.Request) -> web.Response:
        asked.append(f"{request.method} {request.path_qs}")
        return web.Response(text="LAN-DEVICE-SECRET admin_token=9f2c1e7b4a")

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", answer)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield Device(str(server.make_url("")).rstrip("/"), asked)
    finally:
        await server.close()
