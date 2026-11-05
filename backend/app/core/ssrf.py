import ipaddress
import socket
import urllib.parse
from typing import List, Optional, Set, Tuple
import httpcore
import httpx

from app.core.config import settings
from app.core.exceptions import AgentStudioException
from app.core.logging import logger


class SSRFBlockedException(AgentStudioException):
    """Exception raised when an outbound HTTP request targets a prohibited IP or host."""
    def __init__(self, detail: str = "Access to the requested network address is prohibited by security policy."):
        super().__init__(status_code=400, detail=detail)


# Known cloud metadata IPv4 and IPv6 addresses
CLOUD_METADATA_IPS: Set[ipaddress.IPv4Address | ipaddress.IPv6Address] = {
    ipaddress.ip_address("169.254.169.254"),   # AWS, GCP, Azure, DigitalOcean, OpenStack
    ipaddress.ip_address("100.100.100.200"),   # Alibaba Cloud ECS metadata
    ipaddress.ip_address("fd00:ec2::254"),     # AWS EC2 IPv6 metadata
}

# RFC 2544 benchmark / TUN proxy Fake-IP pools (198.18.0.0/15 and 2001:2::/48).
# These are mapped by transparent local/corporate developer proxy tools (e.g. Clash, Sing-box)
# for outbound public internet domains. They do not represent internal intranet hosts.
FAKE_IP_V4 = ipaddress.IPv4Network("198.18.0.0/15")
FAKE_IP_V6 = ipaddress.IPv6Network("2001:2::/48")

# Forbidden metadata hostnames
CLOUD_METADATA_HOSTS: Set[str] = {
    "metadata.google.internal",
    "metadata.google.internal.",
    "metadata",
    "instance-data",
}


def is_ip_allowed(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    allow_local: bool = False,
) -> bool:
    """
    Evaluate whether a destination IP is permissible under current deployment mode.

    - Local Mode (allow_local=True):
      Permits loopback (127.0.0.1, ::1) and private RFC1918/RFC4193 subnets
      for developer access to local Ollama / vLLM.
      Still strictly blocks cloud metadata and multicast/unspecified.

    - Server Mode (allow_local=False):
      Strictly forbids loopback, RFC1918 private subnets, link-local,
      cloud metadata, multicast, unspecified (0.0.0.0, ::), and reserved ranges.
    """
    # Unwrap IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        return is_ip_allowed(ip.ipv4_mapped, allow_local=allow_local)

    # Cloud metadata is NEVER permitted in any mode
    if ip in CLOUD_METADATA_IPS:
        return False

    # Check loopback first (note: ::1 is marked as both is_loopback and is_reserved in Python ipaddress)
    if ip.is_loopback:
        return allow_local

    # Block link-local (169.254.0.0/16, fe80::/10), multicast, unspecified, reserved
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return False

    # Check fake-IP proxy pools (RFC 2544 benchmark range used for transparent outbound DNS)
    # Permitted only in Local mode for developer convenience with transparent local TUN proxies;
    # strictly blocked by default in Server mode to prevent unverified destination IP connections.
    if (isinstance(ip, ipaddress.IPv4Address) and ip in FAKE_IP_V4) or (
        isinstance(ip, ipaddress.IPv6Address) and ip in FAKE_IP_V6
    ):
        return allow_local

    # Check private subnets (RFC 1918 / RFC 4193)
    if ip.is_private:
        return allow_local

    return True


def validate_url_ssrf(
    url: str,
    allow_local: Optional[bool] = None,
    trusted_hosts: Optional[Set[str]] = None,
) -> Tuple[str, List[str]]:
    """
    Strict URL parsing, scheme checking, and DNS pre-resolution SSRF validation.

    Parameters:
        url: The candidate URL string to probe or connect to.
        allow_local: If None, defaults to (settings.DEPLOYMENT_MODE == "local").
        trusted_hosts: Optional set of explicit hostnames or host:port allowed for internal server services.

    Returns:
        Tuple of (clean_url, list_of_resolved_ip_strings).

    Raises:
        SSRFBlockedException if URL format, scheme, hostname, or resolved IPs violate security policy.
    """
    if allow_local is None:
        allow_local = (settings.DEPLOYMENT_MODE == "local")

    if not url or not isinstance(url, str):
        raise SSRFBlockedException("Base URL cannot be empty.")

    clean_url = url.strip()
    try:
        parsed = urllib.parse.urlsplit(clean_url)
    except Exception as exc:
        raise SSRFBlockedException(f"Invalid URL structure: {exc}")

    # 1. Scheme validation: HTTP and HTTPS only
    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise SSRFBlockedException(
            f"Unsupported protocol scheme '{scheme}'. Only HTTP and HTTPS are permitted."
        )

    # 2. Host validation
    hostname = parsed.hostname
    if not hostname:
        raise SSRFBlockedException("URL must specify a valid target hostname or IP address.")

    hostname_clean = hostname.lower().strip("[]")

    if hostname_clean in CLOUD_METADATA_HOSTS:
        raise SSRFBlockedException("Access to cloud metadata endpoints is strictly forbidden.")

    # 3. Port validation
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    if not (1 <= port <= 65535):
        raise SSRFBlockedException(f"Port number {port} is out of valid range (1-65535).")

    # Check explicit trusted_hosts whitelist
    is_trusted = False
    if trusted_hosts:
        normalized_trusted = {h.strip().lower() for h in trusted_hosts if h.strip()}
        is_trusted = (
            f"{hostname_clean}:{port}" in normalized_trusted
            or hostname_clean in normalized_trusted
        )

    # 4. DNS resolution and address validation
    resolved_ips: List[str] = []
    try:
        # Resolve both IPv4 and IPv6 addresses
        addr_info = socket.getaddrinfo(hostname_clean, port, proto=socket.IPPROTO_TCP)
        for _, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            if ip_str not in resolved_ips:
                resolved_ips.append(ip_str)
    except socket.gaierror as exc:
        raise SSRFBlockedException(f"DNS resolution failed for host '{hostname}': {exc}")
    except Exception as exc:
        raise SSRFBlockedException(f"Failed to inspect host '{hostname}': {exc}")

    if not resolved_ips:
        raise SSRFBlockedException(f"No routable IP addresses found for host '{hostname}'.")

    # 5. Validate every resolved IP
    for ip_str in resolved_ips:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            raise SSRFBlockedException(f"Invalid resolved IP format: {ip_str}")

        # Cloud metadata, link-local, multicast and unspecified are NEVER permitted
        if ip in CLOUD_METADATA_IPS:
            raise SSRFBlockedException(
                f"Security Policy Restriction: Destination address '{ip}' is not permitted "
                "(Access to cloud metadata endpoints is strictly forbidden)."
            )
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise SSRFBlockedException(f"Access to link-local/multicast IP {ip} is strictly forbidden.")

        if not is_trusted and not is_ip_allowed(ip, allow_local=allow_local):
            mode_desc = "Server" if not allow_local else "Local"
            logger.warning(f"SSRF blocked connection to {ip} for host {hostname} (mode: {mode_desc})")
            raise SSRFBlockedException(
                f"Security Policy Restriction: Destination address '{ip}' is not permitted "
                f"in {mode_desc} deployment mode."
            )

    return clean_url, resolved_ips


class SSRFSafeBackend(httpcore.AnyIOBackend):
    """
    Custom httpcore network backend that intercepts socket-level TCP connections.
    Mitigates DNS Rebinding attacks by:
    1. Re-validating every resolved IP at the exact moment connect_tcp is invoked.
    2. Passing the verified destination IP directly to the underlying connect_tcp call,
       ensuring that no subsequent re-resolution can divert the connection to a private/internal IP.
    3. Preserves TLS SNI and HTTP Host headers via httpcore's origin-based TLS and header mechanisms.
    """
    def __init__(self, allow_local: bool = False, trusted_hosts: Optional[Set[str]] = None, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.allow_local = allow_local
        self.trusted_hosts = {h.strip().lower() for h in trusted_hosts if h.strip()} if trusted_hosts else set()

    async def connect_tcp(self, host: str, port: int, *args, **kwargs):
        clean_host = host.lower().strip("[]")
        if clean_host in CLOUD_METADATA_HOSTS:
            raise SSRFBlockedException("Access to cloud metadata endpoints is strictly forbidden.")

        is_trusted = (
            f"{clean_host}:{port}" in self.trusted_hosts
            or clean_host in self.trusted_hosts
        )

        try:
            infos = socket.getaddrinfo(clean_host, port, proto=socket.IPPROTO_TCP)
        except Exception as exc:
            raise SSRFBlockedException(f"TCP connect DNS lookup failed for {host}: {exc}")

        verified_ip: Optional[str] = None
        for _, _, _, _, sockaddr in infos:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                raise SSRFBlockedException(f"Invalid IP address during connect: {ip_str}")

            if ip in CLOUD_METADATA_IPS:
                raise SSRFBlockedException("Access to cloud metadata endpoints is strictly forbidden.")
            if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
                raise SSRFBlockedException(f"Access to link-local/multicast IP {ip} is strictly forbidden.")

            if not is_trusted and not is_ip_allowed(ip, allow_local=self.allow_local):
                mode_desc = "Server" if not self.allow_local else "Local"
                raise SSRFBlockedException(
                    f"SSRF Socket Intercept: Prohibited connection to {ip} for target {host} (mode: {mode_desc})."
                )
            if verified_ip is None:
                verified_ip = ip_str

        if not verified_ip:
            raise SSRFBlockedException(f"No routable IP address resolved for {host}")

        # CRUCIAL: Connect directly using the verified IP address instead of original host.
        # This guarantees anyio/OS socket layer connects to the validated IP without re-resolution,
        # completely preventing DNS Rebinding attacks.
        return await super().connect_tcp(verified_ip, port, *args, **kwargs)


class SSRFSafeTransport(httpx.AsyncHTTPTransport):
    """
    httpx Transport equipped with SSRFSafeBackend to protect against DNS rebinding
    and socket-level redirects to private or sensitive IP ranges.
    """
    def __init__(self, allow_local: bool = False, trusted_hosts: Optional[Set[str]] = None, *args, **kwargs):
        kwargs.setdefault("trust_env", False)
        super().__init__(*args, **kwargs)
        self.allow_local = allow_local
        self.trusted_hosts = trusted_hosts
        orig_ssl_context = getattr(self._pool, "_ssl_context", None)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=orig_ssl_context,
            network_backend=SSRFSafeBackend(allow_local=allow_local, trusted_hosts=trusted_hosts),
        )


def get_ssrf_safe_async_client(
    timeout: float = 10.0,
    allow_local: Optional[bool] = None,
    trusted_hosts: Optional[Set[str]] = None,
) -> httpx.AsyncClient:
    """
    Construct a hardened httpx.AsyncClient with socket-level SSRF interceptor
    and redirect target verification.
    """
    if allow_local is None:
        allow_local = (settings.DEPLOYMENT_MODE == "local")

    transport = SSRFSafeTransport(allow_local=allow_local, trusted_hosts=trusted_hosts, trust_env=False)

    async def _on_response(response: httpx.Response):
        # If response is a redirect, validate the Location target before following
        if response.is_redirect and "location" in response.headers:
            loc = response.headers["location"]
            target_url = str(response.url.join(loc))
            validate_url_ssrf(target_url, allow_local=allow_local, trusted_hosts=trusted_hosts)

    return httpx.AsyncClient(
        transport=transport,
        timeout=timeout,
        follow_redirects=True,
        event_hooks={"response": [_on_response]},
        trust_env=False,
    )
