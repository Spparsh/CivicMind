"""
CivicMind — public-source URL safety layer.

Every URL an admin asks TinyFish to fetch (or sends the Agent to), and every
URL a fetch was redirected to, passes through `normalize_public_url`.

Accepted: http / https URLs whose host is a public DNS name or a globally
routable IP address. Refused, with a typed reason:

  * other schemes (file:, ftp:, javascript:, data:, gopher: ...);
  * credentials in the URL (user:pass@host);
  * localhost, single-label hosts and internal suffixes (.local, .internal ...);
  * literal private / loopback / link-local / reserved / multicast addresses,
    including IPv4-mapped IPv6 and "numeric" host spellings (2130706433,
    0x7f.1, 127.1) that some resolvers turn into loopback;
  * names that RESOLVE to any non-public address (DNS check, on by default).

TinyFish performs the actual HTTP request on its own infrastructure, so this
is defence in depth: no admin input can point the platform at internal
infrastructure or cloud metadata services, and no redirect target outside
the public internet is accepted as source content.
"""
import ipaddress
import re
import socket
from urllib.parse import urlsplit, urlunsplit

MAX_URL_LENGTH = 2048
_BLOCKED_HOSTS = {"localhost", "metadata", "metadata.google.internal", "instance-data",
                  "instance-data.ec2.internal"}
_BLOCKED_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".intranet", ".lan",
                     ".home", ".corp", ".private", ".arpa", ".test", ".invalid", ".onion")
_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?)+$")
_NUMERIC_LABEL_RE = re.compile(r"^(?:0x[0-9a-f]*|\d+)$")
_CONTROL_RE = re.compile(r"[\x00-\x20\x7f]")
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


class UnsafeURL(ValueError):
    """A URL that must not be fetched. `code` is stable; `message` is safe to show."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def _system_resolver(host):
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return {info[4][0] for info in infos}


# Tests replace this to avoid real DNS lookups.
resolver = _system_resolver


def _check_ip(ip):
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if (not ip.is_global or ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified
            or (isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED_ADDRESS_SPACE)):
        raise UnsafeURL("private_address", "The address is private, local or reserved and cannot be used.")


def _parse_ip(host):
    try:
        return ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return None


def normalize_public_url(raw, resolve=False):
    """Return a normalized public http(s) URL or raise UnsafeURL."""
    if not isinstance(raw, str):
        raise UnsafeURL("invalid_url", "The URL must be text.")
    raw = raw.strip()
    if not raw:
        raise UnsafeURL("invalid_url", "The URL is empty.")
    if len(raw) > MAX_URL_LENGTH:
        raise UnsafeURL("invalid_url", "The URL is too long.")
    if _CONTROL_RE.search(raw):
        raise UnsafeURL("invalid_url", "The URL contains spaces or control characters.")
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        raise UnsafeURL("invalid_url", "The URL could not be parsed.") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise UnsafeURL("unsupported_scheme", "Only http and https URLs are allowed.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeURL("credentials_in_url", "URLs with embedded credentials are not allowed.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise UnsafeURL("invalid_url", "The URL has no host.")
    ip = _parse_ip(host)
    if ip is not None:
        if "%" in host:
            raise UnsafeURL("private_address", "Scoped IPv6 addresses are not allowed.")
        _check_ip(ip)
        host = ip.compressed
    else:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            raise UnsafeURL("invalid_host", "The host name is not valid.") from None
        labels = host.split(".")
        if all(_NUMERIC_LABEL_RE.match(lbl) for lbl in labels) or labels[-1].isdigit():
            raise UnsafeURL("numeric_host", "Numeric host spellings are not allowed; use a domain name.")
        if len(labels) < 2:
            raise UnsafeURL("internal_host", "Single-label and local host names are not allowed.")
        if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
            raise UnsafeURL("internal_host", "Local or internal host names are not allowed.")
        if not _HOST_RE.match(host):
            raise UnsafeURL("invalid_host", "The host name is not valid.")
        if resolve:
            try:
                addresses = resolver(host)
            except (OSError, UnicodeError):
                raise UnsafeURL("unresolvable_host", "The host name could not be resolved.") from None
            if not addresses:
                raise UnsafeURL("unresolvable_host", "The host name could not be resolved.")
            for addr in addresses:
                parsed = _parse_ip(addr)
                if parsed is None:
                    raise UnsafeURL("unresolvable_host", "The host name could not be resolved.")
                _check_ip(parsed)

    netloc = f"[{host}]" if ip is not None and ip.version == 6 else host
    if port is not None:
        if not 1 <= port <= 65535:
            raise UnsafeURL("invalid_url", "The port is not valid.")
        if not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
            netloc += f":{port}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def host_of(url):
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _bare(host):
    return host[4:] if host.startswith("www.") else host


def same_site(a_host, b_host):
    """True when one host equals or is a subdomain of the other (ignoring www.)."""
    a, b = _bare(a_host or ""), _bare(b_host or "")
    if not a or not b:
        return False
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def domain_allowed(host, allowed_domains):
    host = _bare((host or "").lower())
    return any(host == d or host.endswith("." + d) for d in allowed_domains if d)
