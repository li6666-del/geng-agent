from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from pathlib import Path
from urllib.parse import urljoin, urlparse


class UnsafePdfUrl(ValueError):
    pass


def _public_addresses(hostname: str, port: int) -> set[str]:
    addresses: set[str] = set()
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafePdfUrl("PDF 地址无法解析") from exc
    for info in infos:
        raw = info[4][0]
        ip = ipaddress.ip_address(raw)
        if not ip.is_global:
            raise UnsafePdfUrl("PDF 地址不能指向内网、本机或保留地址")
        addresses.add(raw)
    if not addresses:
        raise UnsafePdfUrl("PDF 地址没有可用的公网 IP")
    return addresses


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, pinned_ip: str, port: int, timeout: float) -> None:
        super().__init__(host=host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw = socket.create_connection((self._pinned_ip, self.port), self.timeout, self.source_address)
        self.sock = self._context.wrap_socket(raw, server_hostname=self.host)


def download_pdf(url: str, destination: Path, max_bytes: int, *, max_redirects: int = 3) -> None:
    current = url.strip()
    for _ in range(max_redirects + 1):
        parsed = urlparse(current)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise UnsafePdfUrl("只允许不含凭据的 HTTPS PDF 地址")
        port = parsed.port or 443
        addresses = _public_addresses(parsed.hostname, port)
        pinned = sorted(addresses)[0]
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        connection = _PinnedHTTPSConnection(parsed.hostname, pinned, port, timeout=30)
        try:
            connection.request("GET", target, headers={"Host": parsed.hostname, "User-Agent": "geng-agent-web/1.0"})
            response = connection.getresponse()
            peer = ipaddress.ip_address(connection.sock.getpeername()[0]) if connection.sock else None
            if peer is None or str(peer) not in addresses or not peer.is_global:
                raise UnsafePdfUrl("PDF 地址在连接时发生了 DNS 变化")
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise UnsafePdfUrl("PDF 地址返回了无目标的重定向")
                current = urljoin(current, location)
                continue
            if response.status != 200:
                raise UnsafePdfUrl(f"PDF 下载失败（HTTP {response.status}）")
            content_length = response.getheader("Content-Length")
            if content_length and int(content_length) > max_bytes:
                raise UnsafePdfUrl("PDF 文件超过大小限制")
            destination.parent.mkdir(parents=True, exist_ok=True)
            total = 0
            with destination.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise UnsafePdfUrl("PDF 文件超过大小限制")
                    handle.write(chunk)
            with destination.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise UnsafePdfUrl("下载内容不是有效 PDF")
            return
        finally:
            connection.close()
    raise UnsafePdfUrl("PDF 地址重定向次数过多")
