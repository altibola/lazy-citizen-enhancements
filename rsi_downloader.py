"""RSI Launcher API authentication + manifest-based file download.

Downloads only the files the pipeline needs from the RSI CDN:
  - Data/Localization/english/global.ini
  - Data/Game2.dcb

AUTH FLOW (reverse-engineered from RSI Launcher):
  POST /api/launcher/v3/signin           → session token
  POST /api/launcher/v3/signin/multiStep → (if MFA required)
  POST /api/launcher/v3/games/claims     → game claims
  POST /api/launcher/v3/games/library    → find game/channel IDs
  POST /api/launcher/v3/games/release    → signed CDN URLs + manifest URL

MANIFEST (binary P4K-MANI\x01 format, SC 4.7+):
  Parsed to find per-file CDN content hashes.
  The hash section maps file-index → 16-byte CDN object identifier.

CDN OBJECTS:
  URL: {objects_url}/{path_prefix}{hash}?{objects_sigs}
  The path_prefix is discovered by decoding the CloudFront Policy
  embedded in objects_sigs (base64-encoded JSON with Resource field).
  Falls back to trying common prefixes if Policy is absent.

SESSION CACHING:
  Successful login is saved to .auth/session.json (gitignored).
  Subsequent runs reuse the cached token, skipping sign-in + MFA.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import struct
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger(__name__)

# ── RSI API ───────────────────────────────────────────────────────────────────

_RSI_API_BASE = "https://robertsspaceindustries.com/api/launcher/v3"
_LAUNCHER_VER = "2.0.0"
_USER_AGENT   = "RSI Launcher/2.0.0"
_ORIGIN       = "https://robertsspaceindustries.com"

_ERR_MFA_REQUIRED = "ErrMultiStepRequired"
_ERR_CAPTCHA      = "ErrCaptchaRequiredLauncher"

_CHANNEL_ALIASES: dict[str, set[str]] = {
    "LIVE": {"LIVE", "LIVE RELEASE", "LIVE GAME"},
    "PTU":  {"PTU", "PUBLIC TEST UNIVERSE", "PUBLIC TEST"},
    "EPTU": {"EPTU", "EVOCATI", "EVOCATI TEST UNIVERSE"},
}

# ── Auth token cache ──────────────────────────────────────────────────────────

_AUTH_DIR  = Path(__file__).parent / ".auth"
_AUTH_FILE = _AUTH_DIR / "session.json"

# ── Manifest targets ──────────────────────────────────────────────────────────

# Maps local output filename → list of candidate trie paths (case-insensitive,
# slash-normalised to backslash).  The first match wins; we stop as soon as
# every local name is found.
#
# RSI CDN manifests can use either the full P4K-internal path *or* just the
# bare filename when the file is a CDN-only object (SC 4.7+).  Both forms are
# listed here so the parser works regardless of manifest version.
_MANIFEST_TARGETS: dict[str, list[str]] = {
    "global.ini": [
        "global.ini",                              # CDN-only (bare filename)
        "Data/Localization/english/global.ini",    # P4K-internal path
    ],
    "Game2.dcb": [
        "Game2.dcb",                               # CDN-only (bare filename)
        "Data/Game2.dcb",                          # P4K-internal path
    ],
}


def _norm_path(p: str) -> str:
    return p.replace("/", "\\").lower()


# ── Exceptions ────────────────────────────────────────────────────────────────

class AuthError(Exception):
    """RSI authentication or API error."""

class MFARequiredError(Exception):
    """Raised by sign_in() when 2FA is required.
    args[0] = partial _Session; args[1] = _Device | None.
    """

# ── Data types ────────────────────────────────────────────────────────────────

class _Session(NamedTuple):
    header_key: str    # e.g. "X-Rsi-Token"
    header_value: str  # the token value

class _Device(NamedTuple):
    header_key: str
    header_value: str

class BuildInfo(NamedTuple):
    version:      str
    p4k_url:      str   # signed base P4K URL (legacy)
    p4k_size:     int
    manifest_url: str   # fully-signed manifest URL
    objects_url:  str   # CDN base URL for per-object downloads
    objects_sigs: str   # CloudFront auth query params (no leading "?")

class _ManifestEntry(NamedTuple):
    local_name:      str
    hash:            str  # 32-char hex CDN object identifier
    size:            int  # uncompressed (0 = unknown)
    compressed_size: int  # compressed  (0 = unknown)
    compression:     str  # "zstd" | ""

# ── Session cache ─────────────────────────────────────────────────────────────

def _load_cached_session() -> _Session | None:
    try:
        obj = json.loads(_AUTH_FILE.read_text(encoding="utf-8"))
        key, val = obj.get("header_key", ""), obj.get("header_value", "")
        if key and val:
            logger.debug("Loaded cached RSI session.")
            return _Session(key, val)
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Could not load cached session (%s).", exc)
    return None

def _save_cached_session(session: _Session) -> None:
    try:
        _AUTH_DIR.mkdir(parents=True, exist_ok=True)
        _AUTH_FILE.write_text(
            json.dumps({"header_key": session.header_key,
                        "header_value": session.header_value}, indent=2),
            encoding="utf-8",
        )
        logger.debug("Saved RSI session to %s", _AUTH_FILE)
    except Exception as exc:
        logger.warning("Could not save session (%s).", exc)

def _clear_cached_session() -> None:
    try:
        _AUTH_FILE.unlink(missing_ok=True)
    except Exception:
        pass

# ── RSI API helpers ───────────────────────────────────────────────────────────

def _rsi_post(endpoint: str, payload: dict | None = None,
              session: _Session | None = None,
              device: _Device | None = None) -> dict:
    url  = f"{_RSI_API_BASE}/{endpoint}"
    body = json.dumps(payload or {}).encode()
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent":   _USER_AGENT,
        "Accept":       "application/json",
        "Origin":       _ORIGIN,
    }
    if session:
        headers[session.header_key] = session.header_value
    if device:
        headers[device.header_key] = device.header_value

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            detail: object = json.loads(raw)
        except Exception:
            detail = raw.decode("utf-8", errors="replace")
        raise AuthError(f"HTTP {exc.code} from {endpoint}: {detail}") from exc

def _require_success(resp: dict, context: str) -> object:
    if resp.get("success") != 1:
        code = resp.get("code", "?")
        msg  = resp.get("msg", resp.get("message", ""))
        raise AuthError(f"{context} failed (code={code}): {msg}")
    return resp.get("data")

def _make_session(data: dict) -> _Session:
    name  = data.get("session_name", "Rsi-Token") if isinstance(data, dict) else "Rsi-Token"
    value = data.get("session_id", "")            if isinstance(data, dict) else ""
    return _Session(header_key=f"X-{name}", header_value=value)

# ── Auth steps ────────────────────────────────────────────────────────────────

def sign_in(username: str, password: str) -> _Session:
    logger.info("Signing in to RSI...")
    resp = _rsi_post("signin", {
        "username": username, "password": password,
        "remember": True, "captcha": None, "launcherVersion": _LAUNCHER_VER,
    })

    if str(resp.get("code", "")) == _ERR_CAPTCHA:
        raise AuthError(
            "RSI requires CAPTCHA verification. Log in via the RSI Launcher "
            "or website once to clear it, then retry."
        )

    if str(resp.get("code", "")) == _ERR_MFA_REQUIRED:
        data = resp.get("data") or {}
        partial = _make_session(data)
        dev_key = data.get("device_header") if isinstance(data, dict) else None
        dev_val = data.get("device_id")     if isinstance(data, dict) else None
        device  = _Device(dev_key, dev_val) if dev_key and dev_val else None
        raise MFARequiredError(partial, device)

    data = _require_success(resp, "signin")
    return _make_session(data if isinstance(data, dict) else {})

def sign_in_mfa(username: str, mfa_code: str, session: _Session,
                device: _Device | None = None) -> _Session:
    logger.info("Submitting MFA code...")
    resp = _rsi_post("signin/multiStep", {
        "code": mfa_code, "device_name": "lazy-citizen-enhancements",
        "device_type": "computer", "duration": "session",
    }, session=session, device=device)
    data = _require_success(resp, "signin/multiStep")
    return _make_session(data if isinstance(data, dict) else {})

def get_game_claims(session: _Session) -> object:
    logger.info("Fetching game claims...")
    resp = _rsi_post("games/claims", session=session)
    return _require_success(resp, "games/claims")

def _iter_channels(game: dict):
    raw = game.get("channels") or {}
    if isinstance(raw, dict):
        yield from raw.items()
    elif isinstance(raw, list):
        for ch in raw:
            yield ch.get("id", ""), ch

def get_p4k_url(session: _Session, claims: object, channel: str = "LIVE") -> BuildInfo:
    logger.info("Fetching game library...")
    resp = _rsi_post("games/library", {"claims": claims}, session=session)
    lib  = _require_success(resp, "games/library")
    games = lib.get("games", []) if isinstance(lib, dict) else []

    chan_upper = channel.upper()
    aliases = _CHANNEL_ALIASES.get(chan_upper, set()) | {chan_upper}
    game_id = channel_id = None
    for game in games:
        for ch_id, ch in _iter_channels(game):
            name_up = str(ch.get("name", "")).upper()
            if name_up in aliases or chan_upper in name_up:
                game_id, channel_id = game.get("id"), ch_id
                logger.info("Matched channel %r for request %r", ch.get("name"), channel)
                break
        if game_id:
            break

    if not game_id or not channel_id:
        available = [ch.get("name") for g in games for _, ch in _iter_channels(g)]
        raise AuthError(
            f"Channel {channel!r} not found. Available: {available}"
        )

    logger.info("Fetching release info...")
    resp = _rsi_post("games/release", {
        "claims": claims, "gameId": game_id, "channelId": channel_id,
    }, session=session)
    rel = _require_success(resp, "games/release")
    if not isinstance(rel, dict):
        raise AuthError(f"games/release returned unexpected type: {type(rel)}")

    version = str(rel.get("versionLabel", "unknown"))

    def _entry(key: str) -> dict:
        v = rel.get(key)
        return v if isinstance(v, dict) else {}

    def _signed(key: str) -> str:
        e = _entry(key)
        url  = e.get("url", "")
        sigs = e.get("signatures", "")
        return f"{url}?{sigs}" if url and sigs else url

    manifest_full = _signed("manifest")
    objects_raw   = _entry("objects")
    objects_url   = objects_raw.get("url", "").rstrip("/")
    objects_sigs  = objects_raw.get("signatures", "")
    p4k_full      = _signed("p4kBase")

    logger.info("version=%s  manifest=%s  objects_base=%s",
                version,
                manifest_full.split("?")[0] if manifest_full else "(none)",
                objects_url or "(none)")

    if not p4k_full:
        raise AuthError(f"No p4kBase.url in release response. Keys: {list(rel.keys())}")

    return BuildInfo(
        version=version, p4k_url=p4k_full, p4k_size=0,
        manifest_url=manifest_full,
        objects_url=objects_url, objects_sigs=objects_sigs,
    )

# ── Authenticate (with session caching) ──────────────────────────────────────

def authenticate(username: str, password: str, channel: str = "LIVE",
                 mfa_code: str | None = None) -> BuildInfo:
    """Full auth flow. Reuses .auth/session.json when still valid."""

    # 1. Try cached session
    cached = _load_cached_session()
    if cached is not None:
        logger.info("Using cached RSI session (skipping sign-in).")
        try:
            claims = get_game_claims(cached)
            return get_p4k_url(cached, claims, channel)
        except AuthError as exc:
            logger.info("Cached session expired (%s) — re-authenticating.", exc)
            _clear_cached_session()

    # 2. Fresh sign-in
    try:
        session = sign_in(username, password)
    except MFARequiredError as exc:
        partial: _Session     = exc.args[0]
        device:  _Device | None = exc.args[1] if len(exc.args) > 1 else None
        if mfa_code is None:
            try:
                mfa_code = input("MFA code (authenticator app): ").strip()
            except (EOFError, KeyboardInterrupt):
                raise MFARequiredError(
                    "MFA required — pass --rsi-mfa-code or enter interactively."
                ) from None
        session = sign_in_mfa(username, mfa_code, partial, device)

    _save_cached_session(session)
    claims = get_game_claims(session)
    return get_p4k_url(session, claims, channel)

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _http_get(url: str, extra_headers: dict | None = None) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, **(extra_headers or {})},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()

def _http_range(url: str, start: int, end: int) -> bytes:
    logger.debug("Range %d-%d (%d B)", start, end, end - start + 1)
    return _http_get(url, {"Range": f"bytes={start}-{end}"})

# ── CloudFront Policy decoder ─────────────────────────────────────────────────

def _decode_cf_policy(objects_sigs: str) -> str | None:
    """Decode the CloudFront Policy from objects_sigs and return the path prefix.

    CloudFront custom-policy sigs look like:
        Policy=<base64url-json>&Signature=<b64>&Key-Pair-Id=<id>

    The JSON looks like:
        {"Statement":[{"Resource":"https://cdn.example.com/gamedata/*",...}]}

    We extract the path component of the Resource URL (e.g. "/gamedata/")
    and strip the trailing wildcard/glob chars. Returns None if no Policy is
    present (canned policy uses Expires= instead).

    CloudFront URL-safe base64 uses: - → +, _ → /, ~ → =
    """
    try:
        params = dict(urllib.parse.parse_qsl(objects_sigs, keep_blank_values=True))
        raw_b64 = params.get("Policy", "")
        if not raw_b64:
            return None

        # Fix CloudFront's non-standard base64 chars; ~ is their padding substitute
        padded = raw_b64.replace("-", "+").replace("_", "/").replace("~", "=")
        padded += "=" * (-len(padded) % 4)   # ensure correct padding length
        policy = json.loads(base64.b64decode(padded))

        resource = policy["Statement"][0]["Resource"]
        path = urllib.parse.urlparse(resource).path

        # Strip trailing glob: "/gamedata/*" → "/gamedata/"
        prefix = path.rstrip("*").rstrip("?")
        # Guarantee a trailing "/" so the hash appends cleanly
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        logger.info("CloudFront Policy decoded → Resource path prefix: %r", prefix)
        return prefix

    except Exception as exc:
        logger.debug("Could not decode CloudFront Policy: %s", exc)
        return None

def _decode_url_prefix(objects_sigs: str) -> str | None:
    """Extract path prefix from a CloudFront canned-policy URLPrefix param.

    RSI's canned-policy sigs look like:
        URLPrefix=<value>&Expires=<ts>&Signature=<b64>&KeyName=<id>

    URLPrefix may be:
      - A plain URL: https://cdn.../gamedata/ (parse_qsl already URL-decodes it)
      - A base64url-encoded URL (CloudFront custom variant)

    Returns the path component (e.g. "/gamedata/"), or None if undecodable.
    """
    try:
        params = dict(urllib.parse.parse_qsl(objects_sigs, keep_blank_values=True))
        raw = params.get("URLPrefix", "")
        if not raw:
            return None

        logger.info("URLPrefix raw value (first 120 chars): %r", raw[:120])

        # Try 1: treat as a plain URL (parse_qsl already URL-decodes %xx)
        parsed = urllib.parse.urlparse(raw)
        if parsed.scheme in ("http", "https") and parsed.path:
            path = parsed.path
            if not path.endswith("/"):
                path += "/"
            logger.info("CloudFront URLPrefix (plain URL) → path prefix: %r", path)
            return path

        # Try 2: base64url-decode (CloudFront uses - → +, _ → /, ~ → =)
        padded = raw.replace("-", "+").replace("_", "/").replace("~", "=")
        padded += "=" * (-len(padded) % 4)
        decoded = base64.b64decode(padded).decode("utf-8", errors="replace")
        parsed2 = urllib.parse.urlparse(decoded)
        if parsed2.scheme in ("http", "https") and parsed2.path:
            path = parsed2.path
            if not path.endswith("/"):
                path += "/"
            logger.info("CloudFront URLPrefix (base64url) → path prefix: %r", path)
            return path

        logger.info("URLPrefix not recognisable as URL (plain or b64): %r", raw[:80])
        return None
    except Exception as exc:
        logger.debug("Could not decode URLPrefix: %s", exc)
        return None


def _cf_path_candidates(objects_sigs: str, h16: str) -> list[str]:
    """Return ordered list of URL path strings to try for a CDN object hash.

    Every path starts with "/" so it appends cleanly to the base URL
    (which has no trailing slash).

    Tries in order:
      1. Path from CloudFront custom-policy (Policy= param)
      2. Path from CloudFront canned-policy URLPrefix= param
      3. Common RSI CDN path pattern fallbacks
    """
    candidates: list[str] = []

    # 1. Custom-policy: Policy= param carries the Resource URL pattern
    prefix = _decode_cf_policy(objects_sigs)
    if prefix:
        for h in (h16, h16.lower()):
            c = f"{prefix}{h}"
            if c not in candidates:
                candidates.append(c)

    # 2. Canned-policy: URLPrefix= is a base64url-encoded URL prefix
    if not candidates:
        prefix = _decode_url_prefix(objects_sigs)
        if prefix:
            for h in (h16, h16.lower()):
                c = f"{prefix}{h}"
                if c not in candidates:
                    candidates.append(c)

    # 3. Common RSI CDN path patterns, all with a leading "/"
    for pfx in ("/", "/gamedata/", "/objects/", "/sc/", "/data/"):
        for h in (h16, h16.lower()):
            path = f"{pfx}{h}"
            if path not in candidates:
                candidates.append(path)

    return candidates

# ── P4K-MANI manifest parser ──────────────────────────────────────────────────

_P4KMANI_MAGIC  = b"P4K-MANI\x01"
_P4KMANI_HS     = 0x28   # header size = 40 bytes

def _parse_p4kmani(data: bytes) -> list[_ManifestEntry]:
    """Parse a binary P4K-MANI\x01 manifest (RSI 4.7+).

    Tree entries: [f0:u32][nlen:u32][f2:u32][f3:u32][name:nlen bytes]
      DIR  (f0=0xFFFFFFFF): f2=rel_child, f3=rel_sibling
      FILE (f0=file_index): f3=rel_sibling
    Relative → absolute: abs = rel + _P4KMANI_HS

    Hash section (at HS+tree_size, stride=40):
      [h16:16][comp:u64][uncomp:u64][other:u64]
    """
    HS         = _P4KMANI_HS
    tree_size  = struct.unpack_from("<Q", data, 0x18)[0]
    file_count = struct.unpack_from("<Q", data, 0x20)[0]
    HASH_BASE  = HS + tree_size
    STRIDE     = 40
    _MAX_SIZE  = 10 ** 9   # >1 GB → not a real byte count

    logger.info("P4K-MANI: tree=%d B  files=%d  hash_base=0x%x",
                tree_size, file_count, HASH_BASE)

    # Build lookup: normalised trie path → local output name.
    # Each local name may have multiple candidate paths; we accept any of them
    # but stop as soon as every local name has been found (dedup by local name).
    path_to_local: dict[str, str] = {}
    for local_name, candidates in _MANIFEST_TARGETS.items():
        for c in candidates:
            path_to_local[_norm_path(c)] = local_name

    found: list[_ManifestEntry] = []
    found_names: set[str] = set()   # local names already collected

    def _hash_entry(f0: int) -> tuple[str, int, int]:
        off = HASH_BASE + f0 * STRIDE
        if off + STRIDE > len(data):
            return ("", 0, 0)
        h16    = data[off:off + 16].hex().upper()
        comp   = struct.unpack_from("<Q", data, off + 16)[0]
        uncomp = struct.unpack_from("<Q", data, off + 24)[0]
        if comp   > _MAX_SIZE: comp   = 0
        if uncomp > _MAX_SIZE: uncomp = 0
        return h16, comp, uncomp

    def _walk(off: int, parent_path: str) -> None:
        while True:
            if len(found_names) == len(_MANIFEST_TARGETS):
                return   # all targets found — stop early
            if off < HS or off + 16 > HS + tree_size:
                break
            f0, nlen, f2, f3 = struct.unpack_from("<IIII", data, off)
            if nlen == 0 or nlen > 512 or off + 16 + nlen > HS + tree_size:
                break

            name = data[off + 16: off + 16 + nlen].decode("ascii", errors="replace")
            full = parent_path + name

            if f0 == 0xFFFFFFFF:
                if f2 != 0xFFFFFFFF:
                    _walk(f2 + HS, full)
            else:
                norm = full.lower()
                local = path_to_local.get(norm)
                if local and local not in found_names:
                    found_names.add(local)
                    h16, comp, uncomp = _hash_entry(f0)
                    logger.info("P4K-MANI match: %r  path=%r  f0=%d  h16=%s  comp=%d  uncomp=%d",
                                local, full, f0, h16, comp, uncomp)
                    found.append(_ManifestEntry(
                        local_name=local, hash=h16, size=uncomp,
                        compressed_size=comp, compression="zstd",
                    ))

            if f3 == 0xFFFFFFFF:
                break
            off = f3 + HS

    # Root sentinel at HS: f0=0, nlen=0xDEAD0000, f2=rel→first real entry
    _, _nlen, root_f2, _ = struct.unpack_from("<IIII", data, HS)
    if root_f2 != 0xFFFFFFFF:
        _walk(root_f2 + HS, "")
    else:
        logger.warning("P4K-MANI: root has f2=0xFFFFFFFF — tree empty?")

    missing = set(_MANIFEST_TARGETS) - {e.local_name for e in found}
    if missing:
        logger.warning("P4K-MANI: targets not found: %s", missing)

    return found

# ── Manifest download + parse ─────────────────────────────────────────────────

def _decompress_manifest(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        import gzip
        return gzip.decompress(data)
    if data[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard as zstd
        except ImportError:
            raise RuntimeError("Manifest is zstd-compressed but 'zstandard' not installed.")
        return zstd.ZstdDecompressor().decompress(data)
    return data

def _download_manifest(build: BuildInfo, cache_dir: Path | None = None) -> bytes:
    if not build.manifest_url:
        raise RuntimeError("No manifest URL in BuildInfo")

    hash_part = build.manifest_url.split("?")[0].rsplit("/", 1)[-1]
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"manifest_{hash_part}.bin"
        if cache_file.exists():
            logger.info("Manifest from cache: %s", cache_file.name)
            return cache_file.read_bytes()

    logger.info("Downloading manifest (%s)...", hash_part[:16])
    data = _http_get(build.manifest_url)
    logger.info("Manifest: %d bytes", len(data))
    if cache_dir:
        cache_file.write_bytes(data)
        logger.info("Manifest cached: %s", cache_file)
    return data

def _parse_manifest(data: bytes) -> list[_ManifestEntry]:
    logger.info("Manifest probe: %s | %r",
                data[:32].hex(), data[:64].decode("utf-8", errors="replace"))

    if data[:len(_P4KMANI_MAGIC)] == _P4KMANI_MAGIC:
        logger.info("Binary P4K-MANI format detected.")
        return _parse_p4kmani(data)

    data = _decompress_manifest(data)
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Manifest is not valid JSON: {exc}") from exc

    file_list = (payload.get("files") or payload.get("entries") or []
                 if isinstance(payload, dict) else payload)
    logger.info("JSON manifest: %d entries", len(file_list))

    # Build lookup: normalised path → local name (multiple candidates per name)
    path_to_local: dict[str, str] = {}
    for local_name, candidates in _MANIFEST_TARGETS.items():
        for c in candidates:
            path_to_local[_norm_path(c)] = local_name

    found: list[_ManifestEntry] = []
    found_names: set[str] = set()
    for entry in file_list:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("localPath") or entry.get("path") or entry.get("name") or ""
        norm = _norm_path(raw)
        for prefix in ("starcitizen\\live\\", "starcitizen\\ptu\\", "starcitizen\\"):
            if norm.startswith(prefix):
                norm = norm[len(prefix):]
                break
        local = path_to_local.get(norm)
        if local and local not in found_names:
            found_names.add(local)
            found.append(_ManifestEntry(
                local_name=local,
                hash=str(entry.get("hash") or entry.get("sha256") or ""),
                size=int(entry.get("size") or 0),
                compressed_size=int(entry.get("compressedSize") or 0),
                compression=str(entry.get("compression") or ""),
            ))
            logger.info("JSON manifest match: %r hash=%s", local, found[-1].hash[:16])
    return found

# ── CDN object download ───────────────────────────────────────────────────────

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

def _download_object(build: BuildInfo, file_hash: str,
                     compressed_size: int, uncomp_size: int,
                     compression: str) -> bytes:
    """Download one content-addressed object, trying multiple URL path formats.

    The correct path prefix is discovered from the CloudFront Policy embedded
    in build.objects_sigs. Falls back to common prefixes if the Policy is absent.
    """
    base = build.objects_url.rstrip("/")
    sigs = build.objects_sigs
    candidates = _cf_path_candidates(sigs, file_hash)

    last_exc: Exception | None = None
    for path in candidates:
        url = f"{base}{path}"
        if sigs:
            url = f"{url}?{sigs}"
        logger.info("Trying CDN: %s%s", base, path)
        try:
            data = _http_get(url)
            logger.info("  ✓ %d bytes", len(data))
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                logger.warning("  403 Forbidden — CloudFront signatures may have expired.")
                raise AuthError(
                    "CDN returned 403 Forbidden. The CloudFront signatures in this "
                    "session have expired. Re-run to get fresh auth."
                ) from exc
            if exc.code == 404:
                logger.debug("  404 for path %r — trying next format.", path)
                last_exc = exc
                continue
            raise
        except Exception as exc:
            last_exc = exc
            continue
    else:
        paths_tried = "\n".join(f"  {base}{p}" for p in candidates)
        raise RuntimeError(
            f"Could not download CDN object {file_hash[:16]}:\n"
            f"All {len(candidates)} URL formats returned 404.\n"
            f"Paths tried:\n{paths_tried}\n\n"
            f"Last error: {last_exc}"
        )

    # Decompress
    comp = compression.lower()
    if not comp or comp in ("none", "store"):
        return data
    if comp in ("zstd", "zstandard"):
        if data[:4] != _ZSTD_MAGIC:
            logger.warning("compression='zstd' but data starts with %s — returning raw.",
                           data[:4].hex())
            return data
        try:
            import zstandard as zstd_mod
        except ImportError:
            raise RuntimeError("'zstandard' package required. Run bootstrap.sh.")
        return zstd_mod.ZstdDecompressor().decompress(data, max_length=uncomp_size or -1)
    if comp in ("deflate", "zlib"):
        return zlib.decompress(data)
    raise ValueError(f"Unknown compression: {compression!r}")

# ── Public API ────────────────────────────────────────────────────────────────

def download_pipeline_inputs(
    username: str,
    password: str,
    out_dir: Path,
    channel: str = "LIVE",
    mfa_code: str | None = None,
) -> tuple[str, Path, Path | None]:
    """Authenticate and download global.ini + Game2.dcb from RSI CDN.

    Returns (game_version, path_to_global_ini, path_to_game2_dcb_or_None).
    Game2.dcb is optional — if absent, returns None (caller must handle).
    """
    build = authenticate(username, password, channel, mfa_code)
    out_dir.mkdir(parents=True, exist_ok=True)

    logger.info("BuildInfo: version=%s  manifest=%s  objects=%s",
                build.version,
                build.manifest_url.split("?")[0] if build.manifest_url else "(none)",
                build.objects_url or "(none)")

    # Log CloudFront Policy so we can diagnose URL issues
    if build.objects_sigs:
        prefix = _decode_cf_policy(build.objects_sigs)
        if prefix is None:
            prefix = _decode_url_prefix(build.objects_sigs)
        if prefix is None:
            sigs_params = dict(urllib.parse.parse_qsl(build.objects_sigs))
            logger.info("objects_sigs keys: %s  (Expires=%s)",
                        list(sigs_params.keys()),
                        sigs_params.get("Expires", "n/a"))

    manifest_data = _download_manifest(build, cache_dir=out_dir)
    entries = _parse_manifest(manifest_data)

    if not entries:
        raise FileNotFoundError(
            "None of the target files found in the manifest.\n"
            "Check log above for what paths the manifest contains."
        )

    results: dict[str, Path] = {}
    errors:  dict[str, str]  = {}

    for entry in entries:
        if not entry.hash:
            logger.warning("No hash for %r — skipping.", entry.local_name)
            continue
        try:
            raw = _download_object(build, entry.hash,
                                   entry.compressed_size, entry.size,
                                   entry.compression)
            dest = out_dir / entry.local_name
            dest.write_bytes(raw)
            logger.info("Saved %s → %s (%d B)", entry.local_name, dest, len(raw))
            results[entry.local_name] = dest
        except Exception as exc:
            logger.error("Failed to download %r: %s", entry.local_name, exc)
            errors[entry.local_name] = str(exc)

    if "global.ini" not in results:
        err = errors.get("global.ini", "not found in manifest")
        raise FileNotFoundError(f"Could not obtain global.ini: {err}")

    if "Game2.dcb" not in results:
        logger.warning("Game2.dcb not downloaded — continuing without it.")

    return build.version, results["global.ini"], results.get("Game2.dcb")
