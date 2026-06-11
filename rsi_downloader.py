"""RSI Launcher API authentication + p4k range extraction.

Downloads only the files the pipeline needs from the RSI CDN p4k archive:
  - Data/Localization/english/global.ini
  - Data/Game2.dcb

AUTH FLOW (reverse-engineered from RSI Launcher):
  POST /api/launcher/v3/signin           → session token
  POST /api/launcher/v3/signin/multiStep → (if MFA required)
  POST /api/launcher/v3/games/claims     → game claims
  POST /api/launcher/v3/games/library    → find game/channel IDs
  POST /api/launcher/v3/games/release    → signed CDN URLs

DOWNLOAD STRATEGY:
  The full p4k URL is a signed CloudFront URL — always works after auth.
  Instead of downloading the entire p4k (50+ GB), we use HTTP Range requests:
    1. HEAD to get total file size
    2. Fetch last 64 KB → find ZIP/ZIP64 End-of-Central-Directory
    3. Fetch Central Directory (CD) → find offsets of target files
    4. Fetch each file's compressed data only
    5. Decompress in-memory (zstd or deflate)

SESSION CACHING:
  Successful login is saved to .auth/session.json (gitignored).
  Subsequent runs reuse the cached token, skipping sign-in + MFA.
"""
from __future__ import annotations

import base64
import json
import logging
import struct
import time
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

# Maps local output filename → list of candidate paths inside the p4k
# (case-insensitive, slash-normalised to backslash for comparison).
_MANIFEST_TARGETS: dict[str, list[str]] = {
    "global.ini": [
        "Data/Localization/english/global.ini",
        "global.ini",
    ],
    "Game2.dcb": [
        "Data/Game2.dcb",
        "Game2.dcb",
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
    header_key: str
    header_value: str

class _Device(NamedTuple):
    header_key: str
    header_value: str

class BuildInfo(NamedTuple):
    version:  str
    p4k_url:  str   # signed CDN URL for the base p4k archive
    p4k_size: int
    manifest_url: str = ""   # fully-signed P4K-MANI manifest URL
    objects_url:  str = ""   # CDN base URL for per-object content-addressed downloads
    objects_sigs: str = ""   # CloudFront auth query params (no leading "?")
    raw_release:  dict = {}  # the full games/release `data` dict (for diagnostics)


class _ManifestEntry(NamedTuple):
    local_name:      str   # output filename (e.g. "global.ini")
    hash:            str   # hex CDN object identifier (32-byte content hash)
    size:            int   # uncompressed size (0 = unknown)
    compressed_size: int
    compression:     str
    alt_hashes:      tuple = ()   # fallback hash candidates at other record offsets

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

def sign_in(username: str, password: str, retries: int = 4) -> _Session:
    """Sign in to RSI, retrying on transient 403/5xx (Cloudflare edge blocks)."""
    payload = {
        "username": username, "password": password,
        "remember": True, "captcha": None, "launcherVersion": _LAUNCHER_VER,
    }
    last_exc: Exception | None = None
    for attempt in range(retries):
        if attempt:
            delay = min(4 ** attempt, 30)  # 4, 16, 30 s
            logger.warning("sign_in attempt %d/%d failed — retrying in %ds...",
                           attempt, retries, delay)
            time.sleep(delay)
        logger.info("Signing in to RSI... (attempt %d/%d)", attempt + 1, retries)
        try:
            resp = _rsi_post("signin", payload)
        except AuthError as exc:
            last_exc = exc
            msg = str(exc)
            if any(f"HTTP {c}" in msg for c in ("403", "429", "500", "502", "503", "504")):
                continue
            raise
        else:
            last_exc = None
            break
    if last_exc is not None:
        raise last_exc

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

    # Dump the full release structure once — this is the single most useful
    # diagnostic for figuring out the CDN object-URL layout. Signature values
    # are long and sensitive, so they're truncated; keys and structure are not.
    _dump_release_structure(rel)

    def _entry(key: str) -> dict:
        v = rel.get(key)
        return v if isinstance(v, dict) else {}

    def _signed(key: str) -> str:
        e = _entry(key)
        url  = e.get("url", "")
        sigs = e.get("signatures", "")
        return f"{url}?{sigs}" if url and sigs else url

    p4k_full      = _signed("p4kBase")
    manifest_full = _signed("manifest")
    objects_raw   = _entry("objects")
    objects_url   = objects_raw.get("url", "").rstrip("/")
    objects_sigs  = objects_raw.get("signatures", "")

    if not p4k_full and not manifest_full:
        raise AuthError(
            f"No p4kBase.url or manifest.url in release response. "
            f"Keys: {list(rel.keys())}"
        )

    logger.info(
        "version=%s  p4k=%s  manifest=%s  objects_base=%s",
        version,
        p4k_full.split("?")[0] if p4k_full else "(none)",
        manifest_full.split("?")[0] if manifest_full else "(none)",
        objects_url or "(none)",
    )

    return BuildInfo(
        version=version, p4k_url=p4k_full, p4k_size=0,
        manifest_url=manifest_full,
        objects_url=objects_url, objects_sigs=objects_sigs,
        raw_release=rel,
    )


def _dump_release_structure(rel: dict, _max_val: int = 180) -> None:
    """Log the complete shape of the games/release `data` dict.

    Recursively prints keys, value types and (truncated) values so we can see
    exactly how `objects`, `manifest`, `p4kBase` and any patch entries are
    structured — without committing to a guessed URL format first.
    """
    def _short(v: object) -> str:
        s = v if isinstance(v, str) else json.dumps(v, default=str)
        return s if len(s) <= _max_val else f"{s[:_max_val]}… ({len(s)} chars)"

    def _walk(obj: object, prefix: str) -> None:
        if isinstance(obj, dict):
            logger.info("release%s = <dict keys=%s>", prefix, list(obj.keys()))
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    _walk(v, f"{prefix}.{k}")
                else:
                    logger.info("release%s.%s : %s = %s",
                                prefix, k, type(v).__name__, _short(v))
        elif isinstance(obj, list):
            logger.info("release%s = <list len=%d>", prefix, len(obj))
            for i, v in enumerate(obj[:5]):
                _walk(v, f"{prefix}[{i}]") if isinstance(v, (dict, list)) \
                    else logger.info("release%s[%d] : %s = %s",
                                     prefix, i, type(v).__name__, _short(v))

    logger.info("───── games/release structure dump ─────")
    try:
        _walk(rel, "")
    except Exception as exc:  # diagnostics must never break the run
        logger.warning("release-structure dump failed: %s", exc)
    logger.info("──────────────────────────────────────")

# ── Authenticate (with session caching) ──────────────────────────────────────

def authenticate(username: str, password: str, channel: str = "LIVE",
                 mfa_code: str | None = None) -> BuildInfo:
    """Full auth flow. Reuses .auth/session.json when still valid."""

    cached = _load_cached_session()
    if cached is not None:
        logger.info("Using cached RSI session (skipping sign-in).")
        try:
            claims = get_game_claims(cached)
            return get_p4k_url(cached, claims, channel)
        except AuthError as exc:
            logger.info("Cached session expired (%s) — re-authenticating.", exc)
            _clear_cached_session()

    try:
        session = sign_in(username, password)
    except MFARequiredError as exc:
        partial: _Session       = exc.args[0]
        device: _Device | None  = exc.args[1] if len(exc.args) > 1 else None
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

def _http_head(url: str) -> int:
    """Return Content-Length from a HEAD request (0 if unknown)."""
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": _USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return int(resp.headers.get("Content-Length") or 0)

def _http_range(url: str, start: int, end: int) -> bytes:
    """Download bytes [start, end] inclusive."""
    size = end - start + 1
    logger.debug("Range %d-%d (%d B)", start, end, size)
    req = urllib.request.Request(
        url, headers={"User-Agent": _USER_AGENT, "Range": f"bytes={start}-{end}"},
    )
    with urllib.request.urlopen(req, timeout=600) as resp:
        return resp.read()

# ── CloudFront signed-URL path decoding ───────────────────────────────────────
# The `objects` CDN URL is CloudFront-signed. The signature's policy defines
# the valid URL space; an object request must fall inside it. We decode the
# policy to learn the path prefix the object hash should hang off of.

def _decode_cf_policy(objects_sigs: str) -> str | None:
    """Return the path prefix from a CloudFront custom-policy (Policy= param)."""
    try:
        params = dict(urllib.parse.parse_qsl(objects_sigs, keep_blank_values=True))
        raw_b64 = params.get("Policy", "")
        if not raw_b64:
            return None
        padded = raw_b64.replace("-", "+").replace("_", "/").replace("~", "=")
        padded += "=" * (-len(padded) % 4)
        policy = json.loads(base64.b64decode(padded))
        resource = policy["Statement"][0]["Resource"]
        path = urllib.parse.urlparse(resource).path
        prefix = path.rstrip("*").rstrip("?")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        logger.info("CloudFront Policy → Resource=%r  path prefix=%r", resource, prefix)
        return prefix
    except Exception as exc:
        logger.debug("Could not decode CloudFront Policy: %s", exc)
        return None

def _decode_url_prefix(objects_sigs: str) -> str | None:
    """Return the path prefix from a CloudFront canned-policy (URLPrefix= param)."""
    try:
        params = dict(urllib.parse.parse_qsl(objects_sigs, keep_blank_values=True))
        raw = params.get("URLPrefix", "")
        if not raw:
            return None
        logger.info("URLPrefix raw value (first 160 chars): %r", raw[:160])
        # May be a plain URL (parse_qsl already %xx-decoded) or base64url-encoded.
        for candidate in (raw, _maybe_b64url(raw)):
            if not candidate:
                continue
            parsed = urllib.parse.urlparse(candidate)
            if parsed.scheme in ("http", "https") and parsed.path:
                path = parsed.path
                if not path.endswith("/"):
                    path += "/"
                logger.info("CloudFront URLPrefix → path prefix: %r", path)
                return path
        logger.info("URLPrefix not recognisable as URL: %r", raw[:80])
        return None
    except Exception as exc:
        logger.debug("Could not decode URLPrefix: %s", exc)
        return None

def _maybe_b64url(raw: str) -> str | None:
    try:
        padded = raw.replace("-", "+").replace("_", "/").replace("~", "=")
        padded += "=" * (-len(padded) % 4)
        return base64.b64decode(padded).decode("utf-8", errors="replace")
    except Exception:
        return None

def _cf_object_prefix(objects_sigs: str) -> str | None:
    """Best available path prefix for content-addressed objects (or None)."""
    return _decode_cf_policy(objects_sigs) or _decode_url_prefix(objects_sigs)

def _hash_path_forms(hash_hex: str) -> list[str]:
    """Candidate path *suffixes* for a content hash (relative to the CDN root).

    The manifest's own URL is ``{domain}/{64-hex}`` (flat, at the domain root),
    so the primary form is the bare hash; case + a little sharding are kept as
    cheap insurance.
    """
    h = hash_hex
    forms = [
        h, h.lower(),
        f"{h[:2]}/{h}", f"{h[:2].lower()}/{h.lower()}",
    ]
    seen: set[str] = set()
    return [f for f in forms if not (f in seen or seen.add(f))]

# ── ZIP/p4k range extraction ──────────────────────────────────────────────────

# RSI p4k files are CryEngine pak archives (ZIP-based) with a simple XOR
# cipher applied to every byte.  The key (5033620A repeated) is the same one
# used by the open-source unp4k tool.  The cipher is positional: byte at
# absolute file offset `i` is XOR'd with key[i % 4].
_P4K_XOR_KEY    = bytes([0x50, 0x33, 0x62, 0x0A])

# Plain ZIP signatures (pre-XOR)
_ZIP_EOCD_SIG   = 0x06054b50   # PK\x05\x06
_ZIP64_EOCD_SIG = 0x06064b50   # PK\x06\x06
_ZIP64_LOC_SIG  = 0x07064b50   # PK\x06\x07
_ZIP_CD_SIG     = 0x02014b50   # PK\x01\x02
_ZIP_LOCAL_SIG  = 0x04034b50   # PK\x03\x04

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _p4k_xor(data: bytes, file_offset: int) -> bytes:
    """Apply (or remove) the p4k XOR cipher at the given absolute file offset."""
    key = _P4K_XOR_KEY
    return bytes(b ^ key[(file_offset + i) % 4] for i, b in enumerate(data))


def _detect_p4k_xor(header8: bytes) -> bool:
    """Return True if the first 8 file bytes look like XOR-encrypted ZIP."""
    # PK\x03\x04 (local file header) or PK\x06\x06 (ZIP64 EOCD) after XOR
    decrypted = _p4k_xor(header8[:4], 0)
    plain_sig = decrypted == b"PK\x03\x04" or decrypted == b"PK\x05\x06"
    logger.info(
        "p4k first 8 bytes (raw): %s  →  XOR-decoded first 4: %s  (XOR=%s)",
        header8[:8].hex().upper(),
        decrypted.hex().upper(),
        plain_sig,
    )
    return plain_sig


def _find_eocd(tail: bytes, xor_offset: int, use_xor: bool) -> int:
    """Scan backwards in tail for the EOCD record, decoding XOR if needed."""
    sig = _ZIP_EOCD_SIG.to_bytes(4, "little")
    pos = len(tail) - 22
    while pos >= 0:
        chunk = tail[pos : pos + 4]
        if use_xor:
            chunk = _p4k_xor(chunk, xor_offset + pos)
        if chunk == sig:
            clen_raw = tail[pos + 20 : pos + 22]
            if use_xor:
                clen_raw = _p4k_xor(clen_raw, xor_offset + pos + 20)
            comment_len = struct.unpack("<H", clen_raw)[0]
            if pos + 22 + comment_len <= len(tail):
                return pos
        pos -= 1
    raise RuntimeError(
        "p4k: End-of-Central-Directory not found in last 64 KB. "
        f"XOR tried: {use_xor}. "
        "The p4k may use a format we don't yet support."
    )


def _p4k_extract(p4k_url: str, out_dir: Path) -> dict[str, Path]:
    """Extract target files from a remote p4k via HTTP Range requests.

    RSI p4k = CryEngine pak = XOR-encrypted ZIP (key: 5033620A repeated).
    We only download what we need:
      1. HEAD → total file size
      2. First 8 bytes → detect XOR
      3. Last 64 KB → EOCD (+ ZIP64 structures)
      4. Central Directory → file offsets
      5. Each file's compressed data → decrypt → decompress → write
    """
    TAIL = 65536 + 22

    # 1. Total size
    total = _http_head(p4k_url)
    if not total:
        raise RuntimeError(
            "p4k server returned no Content-Length; "
            "cannot do range extraction without file size."
        )
    logger.info("p4k total size: %d bytes (%.2f GB)", total, total / 1e9)

    # 2. Detect XOR
    header8  = _http_range(p4k_url, 0, 7)
    use_xor  = _detect_p4k_xor(header8)

    # 3. Fetch tail, find EOCD
    tail_start = max(0, total - TAIL)
    tail_raw   = _http_range(p4k_url, tail_start, total - 1)
    eocd_pos   = _find_eocd(tail_raw, tail_start, use_xor)

    def _read_tail(off: int, size: int) -> bytes:
        raw = tail_raw[off : off + size]
        return _p4k_xor(raw, tail_start + off) if use_xor else raw

    eocd = _read_tail(eocd_pos, 22)
    cd_size   = struct.unpack_from("<I", eocd, 12)[0]
    cd_offset = struct.unpack_from("<I", eocd, 16)[0]

    # ZIP64
    if cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        loc_pos = eocd_pos - 20
        if loc_pos >= 0:
            loc = _read_tail(loc_pos, 4)
            if struct.unpack_from("<I", loc, 0)[0] == _ZIP64_LOC_SIG:
                loc20 = _read_tail(loc_pos, 20)
                z64_off = struct.unpack_from("<Q", loc20, 8)[0]
                z64_raw = _http_range(p4k_url, z64_off, z64_off + 55)
                z64 = _p4k_xor(z64_raw, z64_off) if use_xor else z64_raw
                if struct.unpack_from("<I", z64, 0)[0] == _ZIP64_EOCD_SIG:
                    cd_size   = struct.unpack_from("<Q", z64, 40)[0]
                    cd_offset = struct.unpack_from("<Q", z64, 48)[0]
                    logger.info("ZIP64 EOCD: cd_offset=%d cd_size=%d", cd_offset, cd_size)

    logger.info("Central directory: offset=%d size=%d (%.1f MB)",
                cd_offset, cd_size, cd_size / 1e6)

    # 4. Fetch + decode central directory
    cd_raw = _http_range(p4k_url, cd_offset, cd_offset + cd_size - 1)
    cd = _p4k_xor(cd_raw, cd_offset) if use_xor else cd_raw

    # Build normalised-path → local-name lookup
    path_to_local: dict[str, str] = {}
    for local_name, candidates in _MANIFEST_TARGETS.items():
        for c in candidates:
            path_to_local[_norm_path(c)] = local_name

    # Parse CD entries
    FileEntry = tuple[int, int, int, int]   # (local_off, comp_size, uncomp_size, method)
    file_entries: dict[str, FileEntry] = {}
    _logged_samples = 0   # log first 20 paths to diagnose structure

    pos = 0
    while pos + 46 <= len(cd):
        if struct.unpack_from("<I", cd, pos)[0] != _ZIP_CD_SIG:
            logger.warning(
                "CD parse stopped at pos=%d (sig=0x%08x, expected 0x%08x)",
                pos, struct.unpack_from("<I", cd, pos)[0], _ZIP_CD_SIG,
            )
            break

        method      = struct.unpack_from("<H", cd, pos + 10)[0]
        comp_size   = struct.unpack_from("<I", cd, pos + 20)[0]
        uncomp_size = struct.unpack_from("<I", cd, pos + 24)[0]
        fname_len   = struct.unpack_from("<H", cd, pos + 28)[0]
        extra_len   = struct.unpack_from("<H", cd, pos + 30)[0]
        comment_len = struct.unpack_from("<H", cd, pos + 32)[0]
        local_off   = struct.unpack_from("<I", cd, pos + 42)[0]

        fname_raw = cd[pos + 46 : pos + 46 + fname_len]
        try:
            fname = fname_raw.decode("utf-8")
        except UnicodeDecodeError:
            fname = fname_raw.decode("latin-1")

        if _logged_samples < 20:
            logger.info("CD[%d] %r", _logged_samples, fname)
            _logged_samples += 1

        # Resolve ZIP64 extended info if any field is 0xFFFFFFFF
        if comp_size == 0xFFFFFFFF or uncomp_size == 0xFFFFFFFF or local_off == 0xFFFFFFFF:
            extra = cd[pos + 46 + fname_len : pos + 46 + fname_len + extra_len]
            epos = 0
            while epos + 4 <= len(extra):
                eid  = struct.unpack_from("<H", extra, epos)[0]
                elen = struct.unpack_from("<H", extra, epos + 2)[0]
                if eid == 0x0001:
                    vals = [
                        struct.unpack_from("<Q", extra, epos + 4 + i * 8)[0]
                        for i in range(elen // 8)
                    ]
                    vi = 0
                    if uncomp_size == 0xFFFFFFFF and vi < len(vals):
                        uncomp_size = vals[vi]; vi += 1
                    if comp_size == 0xFFFFFFFF and vi < len(vals):
                        comp_size = vals[vi]; vi += 1
                    if local_off == 0xFFFFFFFF and vi < len(vals):
                        local_off = vals[vi]; vi += 1
                    break
                epos += 4 + elen

        norm = _norm_path(fname)
        local_name = path_to_local.get(norm)
        if local_name and local_name not in file_entries:
            logger.info(
                "CD entry: %r → %r  local_off=%d  comp=%d  uncomp=%d  method=%d",
                fname, local_name, local_off, comp_size, uncomp_size, method,
            )
            file_entries[local_name] = (local_off, comp_size, uncomp_size, method)
            if len(file_entries) == len(_MANIFEST_TARGETS):
                break

        pos += 46 + fname_len + extra_len + comment_len

    missing = set(_MANIFEST_TARGETS) - set(file_entries)
    if missing:
        logger.warning("Files not found in base p4k CD: %s", missing)
    if not file_entries:
        # The base p4k is keyed by major release and frequently lacks the
        # current localization / DataForge files (those ship via the manifest
        # object layer). Return empty so the caller can fall back.
        logger.warning(
            "No target files in the base p4k central directory — "
            "the manifest object layer is required for this build."
        )
        return {}

    # 5. Download + decrypt + decompress each file
    results: dict[str, Path] = {}
    for local_name, (local_off, comp_size, uncomp_size, method) in file_entries.items():
        logger.info(
            "Downloading %s from p4k (comp=%d B / uncomp=%d B)...",
            local_name, comp_size, uncomp_size,
        )

        # Read local file header (30 fixed bytes) to get variable-length fields
        lhdr_raw = _http_range(p4k_url, local_off, local_off + 29)
        lhdr = _p4k_xor(lhdr_raw, local_off) if use_xor else lhdr_raw
        lh_fname_len = struct.unpack_from("<H", lhdr, 26)[0]
        lh_extra_len = struct.unpack_from("<H", lhdr, 28)[0]
        data_start   = local_off + 30 + lh_fname_len + lh_extra_len

        raw_enc = _http_range(p4k_url, data_start, data_start + comp_size - 1)
        raw = _p4k_xor(raw_enc, data_start) if use_xor else raw_enc
        logger.info("  fetched %d B (XOR decrypted=%s)", len(raw), use_xor)

        # Decompress — check zstd magic first (RSI uses it regardless of method code)
        if raw[:4] == _ZSTD_MAGIC:
            try:
                import zstandard as zstd_mod
            except ImportError:
                raise RuntimeError(
                    "'zstandard' package is required to decompress p4k entries. "
                    "Run bootstrap.sh to install it."
                )
            decompressed = zstd_mod.ZstdDecompressor().decompress(
                raw, max_length=uncomp_size or -1,
            )
        elif method == 0:
            decompressed = raw
        elif method == 8:
            decompressed = zlib.decompress(raw, -15)
        else:
            raise ValueError(
                f"Unknown compression for {local_name}: method={method} "
                f"(first 4 bytes: {raw[:4].hex()})"
            )

        dest = out_dir / local_name
        dest.write_bytes(decompressed)
        logger.info("  saved %s (%d B)", dest, len(decompressed))
        results[local_name] = dest

    return results

# ── P4K-MANI manifest parsing ─────────────────────────────────────────────────
# RSI Launcher 2.0 builds ship a binary "P4K-MANI\x01" manifest describing the
# current build's file tree. Each file references a content hash; the actual
# bytes live as content-addressed objects on the `objects` CDN. This is where
# frequently-updated files (global.ini, Game2.dcb) come from — not the base p4k.

_P4KMANI_MAGIC = b"P4K-MANI\x01"
_P4KMANI_HS    = 0x28   # header size = 40 bytes

def _decompress_manifest(data: bytes) -> bytes:
    if data[:2] == b"\x1f\x8b":
        import gzip
        return gzip.decompress(data)
    if data[:4] == _ZSTD_MAGIC:
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
    logger.info("Downloading manifest (%s)...", hash_part[:24])
    data = _http_get(build.manifest_url)
    logger.info("Manifest: %d bytes (first 16: %s)", len(data), data[:16].hex().upper())
    if cache_dir:
        cache_file.write_bytes(data)
    return data

def _parse_p4kmani(data: bytes) -> list[_ManifestEntry]:
    """Parse a binary P4K-MANI\\x01 manifest; return matched target entries.

    The hash section is a flat array of fixed-size records, one per file, indexed
    by the file index (f0) stored in the tree. Each object is fetched from the
    CDN as ``{objects.url}/{record_hash_hex}`` — the same 32-byte content-hash
    scheme the manifest itself uses (its URL is a 64-hex hash at the domain root).

    The record stride is derived from the section size so we don't hard-code a
    guess: (manifest_size - header - tree) / file_count. For the observed builds
    that is exactly 204 bytes/record, with the 32-byte content hash at offset 0.
    """
    HS        = _P4KMANI_HS
    tree_size = struct.unpack_from("<Q", data, 0x18)[0]
    file_cnt  = struct.unpack_from("<Q", data, 0x20)[0]
    HASH_BASE = HS + tree_size
    hash_bytes = len(data) - HASH_BASE
    STRIDE    = hash_bytes // file_cnt if file_cnt else 0

    logger.info("P4K-MANI: tree=%d B  files=%d  hash_base=0x%x  total=%d B",
                tree_size, file_cnt, HASH_BASE, len(data))
    logger.info("P4K-MANI hash section: %d B / %d files = stride %d B (rem %d)",
                hash_bytes, file_cnt, STRIDE, hash_bytes % file_cnt if file_cnt else 0)
    logger.info("P4K-MANI hash section first 64 bytes: %s",
                data[HASH_BASE:HASH_BASE + 64].hex().upper())
    if STRIDE < 32:
        logger.warning("P4K-MANI: derived stride %d < 32 — falling back to 40.", STRIDE)
        STRIDE = 40

    path_to_local: dict[str, str] = {}
    for local_name, candidates in _MANIFEST_TARGETS.items():
        for c in candidates:
            path_to_local[_norm_path(c)] = local_name

    found: list[_ManifestEntry] = []
    found_names: set[str] = set()

    def _record(f0: int) -> bytes:
        off = HASH_BASE + f0 * STRIDE
        end = off + STRIDE
        return data[off:end] if end <= len(data) else b""

    def _walk(off: int, parent: str) -> None:
        while True:
            if len(found_names) == len(_MANIFEST_TARGETS):
                return
            if off < HS or off + 16 > HS + tree_size:
                break
            f0, nlen, f2, f3 = struct.unpack_from("<IIII", data, off)
            if nlen == 0 or nlen > 512 or off + 16 + nlen > HS + tree_size:
                break
            name = data[off + 16:off + 16 + nlen].decode("ascii", errors="replace")
            full = parent + name
            if f0 == 0xFFFFFFFF:
                if f2 != 0xFFFFFFFF:
                    _walk(f2 + HS, full)
            else:
                local = path_to_local.get(full.lower())
                if local and local not in found_names:
                    found_names.add(local)
                    rec = _record(f0)
                    primary = rec[:32].hex().upper()
                    # Candidate hashes at a few leading offsets, in case the
                    # record carries a small prefix before the content hash.
                    alts = tuple(
                        rec[o:o + 32].hex().upper()
                        for o in (0, 4, 8, 16)
                        if len(rec) >= o + 32 and rec[o:o + 32].hex().upper() != primary
                    )
                    logger.info(
                        "P4K-MANI match: %r path=%r f0=%d stride=%d\n"
                        "    record[0:96]=%s\n    hash(off0)=%s",
                        local, full, f0, STRIDE, rec[:96].hex().upper(), primary,
                    )
                    found.append(_ManifestEntry(
                        local_name=local, hash=primary,
                        size=0, compressed_size=0, compression="zstd",
                        alt_hashes=alts,
                    ))
            if f3 == 0xFFFFFFFF:
                break
            off = f3 + HS

    _, _nlen, root_f2, _ = struct.unpack_from("<IIII", data, HS)
    if root_f2 != 0xFFFFFFFF:
        _walk(root_f2 + HS, "")
    else:
        logger.warning("P4K-MANI: root f2=0xFFFFFFFF — tree empty?")
    return found

def _parse_json_manifest(data: bytes) -> list[_ManifestEntry]:
    data = _decompress_manifest(data)
    payload = json.loads(data)
    file_list = (payload.get("files") or payload.get("entries") or []
                 if isinstance(payload, dict) else payload)
    logger.info("JSON manifest: %d entries", len(file_list))
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
            logger.info("JSON manifest match: %r hash=%s", local, found[-1].hash[:24])
    return found

def _parse_manifest(data: bytes) -> list[_ManifestEntry]:
    logger.info("Manifest probe: magic=%s ascii=%r",
                data[:16].hex().upper(), data[:16].decode("latin-1", errors="replace"))
    if data[:len(_P4KMANI_MAGIC)] == _P4KMANI_MAGIC:
        logger.info("Binary P4K-MANI manifest detected.")
        return _parse_p4kmani(data)
    logger.info("Non-P4K-MANI manifest — trying JSON.")
    return _parse_json_manifest(data)

# ── CDN content-addressed object download ─────────────────────────────────────

def _download_object(build: BuildInfo, entry: _ManifestEntry) -> bytes:
    """Download one content-addressed object, probing URL formats.

    Logs the HTTP status of every candidate so that, even on total failure, the
    run reveals which path layout the CDN expects.
    """
    base = build.objects_url.rstrip("/")
    sigs = build.objects_sigs

    # The manifest URL is {domain}/{64-hex} at the root, and objects.url is the
    # bare domain with a domain-wide signature — so the object is at the root,
    # keyed by its content hash. Probe the primary hash first, then the
    # alt-offset candidates (in case the record has a leading prefix), each as
    # a flat root path plus light sharding insurance.
    all_hashes = [entry.hash, *entry.alt_hashes]
    candidates: list[str] = []
    for h in all_hashes:
        if not h:
            continue
        for suffix in _hash_path_forms(h):
            path = f"/{suffix}"
            if path not in candidates:
                candidates.append(path)
    # Fixed prefixes as a last resort, primary hash only.
    for p in ("objects", "data"):
        path = f"/{p}/{entry.hash}"
        if path not in candidates:
            candidates.append(path)

    last_status = "no attempts"
    data: bytes | None = None
    for path in candidates:
        url = f"{base}{path}"
        if sigs:
            url = f"{url}?{sigs}"
        try:
            data = _http_get(url)
            logger.info("  ✓ 200  %s%s  (%d B)", base, path, len(data))
            break
        except urllib.error.HTTPError as exc:
            last_status = f"HTTP {exc.code}"
            if exc.code == 403:
                logger.warning("  403 %s%s — signature/path mismatch", base, path)
            else:
                logger.info("  %d %s%s", exc.code, base, path)
            continue
        except Exception as exc:
            last_status = str(exc)
            logger.info("  ERR %s%s (%s)", base, path, exc)
            continue

    if data is None:
        tried = "\n".join(f"    {base}{p}" for p in candidates)
        raise RuntimeError(
            f"Could not download object for {entry.local_name} "
            f"(hash={entry.hash[:24]}). Last status: {last_status}.\n"
            f"Tried {len(candidates)} URL forms:\n{tried}"
        )

    comp = (entry.compression or "").lower()
    if data[:4] == _ZSTD_MAGIC or comp in ("zstd", "zstandard"):
        if data[:4] != _ZSTD_MAGIC:
            return data
        import zstandard as zstd_mod
        return zstd_mod.ZstdDecompressor().decompress(data, max_length=entry.size or -1)
    if comp in ("deflate", "zlib"):
        return zlib.decompress(data)
    return data

def _download_via_manifest(build: BuildInfo, out_dir: Path) -> dict[str, Path]:
    """Manifest → per-file content hash → CDN object. Returns what it got."""
    if build.objects_sigs:
        if _cf_object_prefix(build.objects_sigs) is None:
            params = dict(urllib.parse.parse_qsl(build.objects_sigs))
            logger.info("objects_sigs param keys: %s", list(params.keys()))

    manifest = _download_manifest(build, cache_dir=out_dir)
    entries = _parse_manifest(manifest)
    if not entries:
        logger.warning("Manifest parsed but no target files matched.")
        return {}

    results: dict[str, Path] = {}
    for entry in entries:
        if not entry.hash:
            logger.warning("No hash for %r — skipping.", entry.local_name)
            continue
        try:
            raw = _download_object(build, entry)
            dest = out_dir / entry.local_name
            dest.write_bytes(raw)
            logger.info("Saved %s → %s (%d B)", entry.local_name, dest, len(raw))
            results[entry.local_name] = dest
        except Exception as exc:
            logger.error("Object download failed for %r: %s", entry.local_name, exc)
    return results

# ── Public API ────────────────────────────────────────────────────────────────

def download_pipeline_inputs(
    username: str,
    password: str,
    out_dir: Path,
    channel: str = "LIVE",
    mfa_code: str | None = None,
) -> tuple[str, Path, Path | None]:
    """Authenticate and download global.ini + Game2.dcb from the RSI CDN.

    Two strategies, in order:
      1. Manifest object layer (primary) — the current build's files as
         content-addressed CDN objects. This is where global.ini / Game2.dcb
         actually live for modern builds.
      2. Base p4k range extraction (fallback) — works only when the target
         files happen to be in the major-release base archive.

    Returns (game_version, path_to_global_ini, path_to_game2_dcb_or_None).
    """
    build = authenticate(username, password, channel, mfa_code)
    out_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Path] = {}

    # 1. Manifest object layer (primary path for current builds).
    if build.manifest_url:
        logger.info("Attempting manifest object download (primary)...")
        try:
            results = _download_via_manifest(build, out_dir)
        except Exception as exc:
            logger.warning("Manifest download path failed: %s", exc)
    else:
        logger.info("No manifest URL in release — skipping manifest path.")

    # 2. Base p4k range extraction (fallback).
    if "global.ini" not in results and build.p4k_url:
        logger.info("Falling back to base p4k range extraction...")
        try:
            p4k_results = _p4k_extract(build.p4k_url, out_dir)
            for k, v in p4k_results.items():
                results.setdefault(k, v)
        except Exception as exc:
            logger.warning("Base p4k extraction failed: %s", exc)

    if "global.ini" not in results:
        raise FileNotFoundError(
            "Could not obtain global.ini from either the manifest object layer "
            "or the base p4k. See the games/release structure dump and the "
            "per-URL probe results above to determine the correct CDN object "
            "path format."
        )

    if "Game2.dcb" not in results:
        logger.warning("Game2.dcb not obtained — DataForge generation will be skipped.")

    return build.version, results["global.ini"], results.get("Game2.dcb")


if __name__ == "__main__":
    import argparse
    import getpass
    import os

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(
        description="Authenticate to RSI and download global.ini + Game2.dcb, "
                    "or just dump the games/release structure (--diagnose).",
    )
    ap.add_argument("--channel", default="LIVE")
    ap.add_argument("--out", type=Path, default=Path("out/cdn_downloads"))
    ap.add_argument("--rsi-mfa-code", default=None)
    ap.add_argument("--diagnose", action="store_true",
                    help="Authenticate and dump the release/manifest structure "
                         "without downloading game objects.")
    args = ap.parse_args()

    user = os.environ.get("RSI_USERNAME", "").strip()
    pw   = os.environ.get("RSI_PASSWORD", "").strip()
    if not user:
        user = input("RSI username (email): ").strip()
    if not pw:
        pw = getpass.getpass("RSI password: ").strip()

    if args.diagnose:
        info = authenticate(user, pw, args.channel, args.rsi_mfa_code)
        logger.info("Diagnose complete: version=%s", info.version)
        if info.manifest_url:
            try:
                manifest = _download_manifest(info, cache_dir=args.out)
                _parse_manifest(manifest)
            except Exception as exc:
                logger.error("Manifest fetch/parse failed: %s", exc)
    else:
        ver, gi, dcb = download_pipeline_inputs(
            user, pw, args.out, args.channel, args.rsi_mfa_code,
        )
        logger.info("Done: version=%s global.ini=%s dcb=%s", ver, gi, dcb)
