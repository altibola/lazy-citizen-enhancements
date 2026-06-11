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
    p4k_url:  str   # signed CDN URL for the full p4k
    p4k_size: int

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

    def _entry(key: str) -> dict:
        v = rel.get(key)
        return v if isinstance(v, dict) else {}

    def _signed(key: str) -> str:
        e = _entry(key)
        url  = e.get("url", "")
        sigs = e.get("signatures", "")
        return f"{url}?{sigs}" if url and sigs else url

    p4k_full = _signed("p4kBase")

    if not p4k_full:
        raise AuthError(f"No p4kBase.url in release response. Keys: {list(rel.keys())}")

    p4k_path = p4k_full.split("?")[0]
    logger.info("version=%s  p4k=%s", version, p4k_path)

    return BuildInfo(version=version, p4k_url=p4k_full, p4k_size=0)

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

# ── ZIP/p4k range extraction ──────────────────────────────────────────────────

_ZIP_EOCD_SIG   = 0x06054b50
_ZIP64_EOCD_SIG = 0x06064b50
_ZIP64_LOC_SIG  = 0x07064b50
_ZIP_CD_SIG     = 0x02014b50
_ZIP_LOCAL_SIG  = 0x04034b50
_ZSTD_MAGIC     = b"\x28\xb5\x2f\xfd"


def _find_eocd(tail: bytes) -> int:
    """Find the EOCD record offset within tail bytes (scan backwards)."""
    sig = _ZIP_EOCD_SIG.to_bytes(4, "little")
    pos = len(tail) - 22
    while pos >= 0:
        if tail[pos : pos + 4] == sig:
            comment_len = struct.unpack_from("<H", tail, pos + 20)[0]
            if pos + 22 + comment_len == len(tail):
                return pos
        pos -= 1
    raise RuntimeError(
        "ZIP End-of-Central-Directory not found in last 64 KB of p4k. "
        "The file may be incomplete or not a valid ZIP/p4k."
    )


def _p4k_extract(p4k_url: str, out_dir: Path) -> dict[str, Path]:
    """Extract target files from a remote p4k via HTTP Range requests.

    The p4k format is ZIP-compatible (RSI uses zstd or deflate for entries).
    We only download what we need:
      1. HEAD → total file size
      2. Last 64 KB → EOCD (+ ZIP64 structures)
      3. Central Directory → file offsets
      4. Each file's compressed data → decompress → write
    """
    TAIL = 65536 + 22   # enough for EOCD + max ZIP comment

    # 1. Total size
    total = _http_head(p4k_url)
    if not total:
        raise RuntimeError(
            "p4k server returned no Content-Length. "
            "Cannot do range-based extraction without knowing file size."
        )
    logger.info("p4k total size: %d bytes (%.2f GB)", total, total / 1e9)

    # 2. Read tail, find EOCD
    tail_start = max(0, total - TAIL)
    tail = _http_range(p4k_url, tail_start, total - 1)
    eocd_pos = _find_eocd(tail)
    eocd = tail[eocd_pos:]

    cd_size   = struct.unpack_from("<I", eocd, 12)[0]
    cd_offset = struct.unpack_from("<I", eocd, 16)[0]

    # ZIP64: values == 0xFFFFFFFF → read from ZIP64 EOCD
    if cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        loc_pos = eocd_pos - 20
        if loc_pos >= 0 and tail[loc_pos : loc_pos + 4] == _ZIP64_LOC_SIG.to_bytes(4, "little"):
            z64_off = struct.unpack_from("<Q", tail, loc_pos + 8)[0]
            z64_hdr = _http_range(p4k_url, z64_off, z64_off + 55)
            if struct.unpack_from("<I", z64_hdr, 0)[0] == _ZIP64_EOCD_SIG:
                cd_size   = struct.unpack_from("<Q", z64_hdr, 40)[0]
                cd_offset = struct.unpack_from("<Q", z64_hdr, 48)[0]
                logger.info("ZIP64 EOCD: cd_offset=%d cd_size=%d", cd_offset, cd_size)

    logger.info("Central directory: offset=%d size=%d (%.1f MB)",
                cd_offset, cd_size, cd_size / 1e6)

    # 3. Fetch central directory
    cd = _http_range(p4k_url, cd_offset, cd_offset + cd_size - 1)

    # Build normalised-path → local-name lookup
    path_to_local: dict[str, str] = {}
    for local_name, candidates in _MANIFEST_TARGETS.items():
        for c in candidates:
            path_to_local[_norm_path(c)] = local_name

    # 4. Parse CD entries
    FileEntry = tuple[int, int, int, int]   # (local_off, comp_size, uncomp_size, method)
    file_entries: dict[str, FileEntry] = {}

    pos = 0
    while pos + 46 <= len(cd):
        if struct.unpack_from("<I", cd, pos)[0] != _ZIP_CD_SIG:
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
        logger.warning("Files not found in p4k CD: %s", missing)
    if not file_entries:
        raise FileNotFoundError(
            "None of the target files found in the p4k central directory. "
            "The p4k may use a different internal path layout."
        )

    # 5. Download + decompress each file
    results: dict[str, Path] = {}
    for local_name, (local_off, comp_size, uncomp_size, method) in file_entries.items():
        logger.info(
            "Downloading %s from p4k (comp=%d B / uncomp=%d B)...",
            local_name, comp_size, uncomp_size,
        )

        # Local file header: fixed 30 bytes + variable fname + extra
        lhdr = _http_range(p4k_url, local_off, local_off + 29)
        lh_fname_len = struct.unpack_from("<H", lhdr, 26)[0]
        lh_extra_len = struct.unpack_from("<H", lhdr, 28)[0]
        data_start   = local_off + 30 + lh_fname_len + lh_extra_len

        raw = _http_range(p4k_url, data_start, data_start + comp_size - 1)
        logger.info("  fetched %d B", len(raw))

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

# ── Public API ────────────────────────────────────────────────────────────────

def download_pipeline_inputs(
    username: str,
    password: str,
    out_dir: Path,
    channel: str = "LIVE",
    mfa_code: str | None = None,
) -> tuple[str, Path, Path | None]:
    """Authenticate and download global.ini + Game2.dcb from the RSI CDN p4k.

    Uses HTTP Range requests to extract only the needed files without
    downloading the full archive (which is 50+ GB).

    Returns (game_version, path_to_global_ini, path_to_game2_dcb_or_None).
    Game2.dcb is optional — if absent from the p4k, returns None.
    """
    build = authenticate(username, password, channel, mfa_code)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = _p4k_extract(build.p4k_url, out_dir)

    if "global.ini" not in results:
        err_msg = "global.ini not found in p4k central directory"
        raise FileNotFoundError(err_msg)

    if "Game2.dcb" not in results:
        logger.warning("Game2.dcb not found in p4k — continuing without it.")

    return build.version, results["global.ini"], results.get("Game2.dcb")
