"""VPN tunnel for the region-blocked engines — build 189.

Direct Gemini is LOCATION-blocked and direct OpenAI is
territory-blocked from this VPS (builds 164/168): Google/OpenAI
judge the API caller's IP country, and the server sits in Hong
Kong. The owner already owns a V2Ray subscription, so instead of
moving the server, ONLY the Gemini and OpenAI API calls leave
through a tunnel:

  * xray-core (the standard V2Ray client) runs on the server —
    installed and supervised by deploy/activate-settings.sh
    (the build-189 bootstrap §13e, extracted in build 190) —
    with ONE inbound: an HTTP proxy on 127.0.0.1:18119.
  * pipeline/gemini_translate.py and pipeline/openai_translate.py
    pass that proxy to `requests` EXPLICITLY, per call, and only
    while the tunnel is verified up (see proxy_url()). Nothing
    else in the app passes a proxy anywhere: downloads,
    OpenRouter, xKiro, Apps Script, the free chain, edge-tts and
    Telegram all keep the direct route, and there is no OS-level
    route change of any kind.

Owner flow (mirrors the build-184 Telegram Local API card): the
owner pastes a node into the Admin card — a vless:// or vmess://
share link, several links, a subscription URL, or a pasted
subscription body. THIS module parses it (at save time, in the
app) into a ready-to-run xray client config written to
tun-xray.json in the app dir (chmod 600; server state like
users.db — never in git, survives the no-delete deploys). The
raw paste is kept base64'd in .env (VT_TUNNEL_LINK_B64, chmod
600) so the save can be re-done; it is NEVER returned by any
endpoint and never logged — status endpoints expose only
booleans, the node label and the exit country.

Verification handshake with the activation script: after xray is
up AND an egress check through the proxy has answered, the
bootstrap writes VT_TUNNEL_PROXY + VT_TUNNEL_COUNTRY +
VT_TUNNEL_CONF_SHA (the sha256 of the config it verified) into
.env. proxy_url() below honours VT_TUNNEL_PROXY only while the
sha still matches the config on disk — so the moment the owner
saves a DIFFERENT node, the old tunnel stops being trusted
until the bootstrap has verified the new one, and deleting the
settings (clear()) removes the pointers at once.

Supported node formats: vless and vmess share links with
tcp / ws / grpc / h2 transports and none / tls / reality
security (the shapes real subscriptions overwhelmingly use).
Shadowsocks / trojan links and kcp / quic / xhttp transports
are recognised but NOT supported. Node selection from a
subscription (the owner's purchase is a reseller subscription
URL with nodes in many countries): a node whose remark names
Singapore (SG) wins — that is the owner's exit and a
Gemini/OpenAI-supported country; otherwise the first usable
node is taken and its label is shown on the Admin card so a
wrong-country pick is never silent.

Build 190: node usability now mirrors the rule xray-core
itself enforces when it builds a config (diagnosed live on
the owner's server: "infra/conf: vless without TLS or other
encryption is prohibited unless the server address is a
private..."). A VLESS node with no encryption at all — no
TLS/REALITY stream security and no VLESS encryption — pointing
at a PUBLIC address can never run, so selection treats it as
unusable and prefers, in order: SG + encrypted, SG usable,
first encrypted, first usable. When the SG favourite is
skipped for this rule the skip is recorded (label only) and
shown on the card; when EVERY node is like that the save
still succeeds with the first node, flagged `insecure` so
the card can say plainly that xray will refuse it. The
bootstrap's own diagnosis of the last activation attempt
(tun-status.json — sanitized, structural only) is merged
into status() as fail_kind / fail_reason.
"""
import base64
import hashlib
import ipaddress
import json
import os
import re
import urllib.parse
from pathlib import Path

from .common import PROJECT_ROOT

PROXY_PORT = 18119
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
CONF_NAME = "tun-xray.json"

_ENV_LINK = "VT_TUNNEL_LINK_B64"
_ENV_LABEL = "VT_TUNNEL_LABEL"
_ENV_PROXY = "VT_TUNNEL_PROXY"
_ENV_COUNTRY = "VT_TUNNEL_COUNTRY"
_ENV_SHA = "VT_TUNNEL_CONF_SHA"
_ENV_SKIPSG = "VT_TUNNEL_SKIPPED_SG"

_STATUS_NAME = "tun-status.json"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_LINK_RE = re.compile(r"(?:vless|vmess|ss|trojan)://[^\s\"'<>]+")
_SUPPORTED = ("vless://", "vmess://")
_TRANSPORTS = ("tcp", "ws", "grpc", "h2")


# ---------------- storage paths / .env ----------------

def _conf_path():
    return PROJECT_ROOT / CONF_NAME


def _env_path():
    return PROJECT_ROOT / ".env"


def _env_get(key):
    """One KEY=value from the app .env, read live (same semantics
    as the readers in gemini_translate & friends)."""
    p = _env_path()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    return (os.environ.get(key) or "").strip()


def _env_set(key, value):
    """Set or remove (value "") one .env line, chmod 600 — the
    same line surgery as app.py's _env_write/_env_remove (this
    module cannot import app.py: app imports the pipeline)."""
    p = _env_path()
    lines = []
    if p.exists():
        lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                 if not ln.startswith(key + "=")]
    if value:
        lines.append(f"{key}={value}")
    p.write_text("\n".join(lines) + ("\n" if lines else ""),
                 encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _conf_sha():
    try:
        return hashlib.sha256(_conf_path().read_bytes()).hexdigest()
    except Exception:
        return ""


def _status_path():
    """The bootstrap's diagnosis of the last tunnel activation
    (build 190): written by deploy/activate-settings.sh, holds
    only a failure kind + a SANITIZED structural reason + the
    node's protocol/transport/security triple — no addresses,
    no ids, no config contents, by construction."""
    return PROJECT_ROOT / _STATUS_NAME


def _remove_status_file():
    try:
        _status_path().unlink(missing_ok=True)
    except Exception:
        pass


# ---------------- share-link parsing ----------------

def _b64_loose(text):
    """Base64-decode tolerating the urlsafe alphabet, missing
    padding and surrounding whitespace (subscription bodies and
    vmess links use every variant). Returns bytes or None."""
    s = re.sub(r"\s+", "", text or "")
    if not s:
        return None
    s += "=" * (-len(s) % 4)
    for dec in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return dec(s)
        except Exception:
            continue
    return None


def _clean_label(raw, fallback):
    label = re.sub(r"[\x00-\x1f\x7f]+", " ", raw or "").strip()
    return (label or fallback)[:80]


def _stream_settings(network, security, *, sni="", host_header="",
                     path="", service_name="", fingerprint="",
                     alpn="", public_key="", short_id="", spider_x="",
                     allow_insecure=False, header_type=""):
    """The streamSettings block shared by the vless/vmess
    outbounds. Raises ValueError for transports/security this
    build does not implement (kcp/quic/xhttp, unknown security) —
    the caller then tries the next subscription node."""
    net = (network or "tcp").lower()
    if net in ("http", "h2"):
        net = "h2"
    if net == "splithttp":
        net = "xhttp"
    if net not in _TRANSPORTS:
        raise ValueError(f"transport '{network}' မရသေးပါ (tcp/ws/grpc/h2 ပဲ ရပါတယ်)")
    sec = (security or "none").lower()
    if sec not in ("none", "", "tls", "reality"):
        raise ValueError(f"security '{security}' မရသေးပါ")
    ss = {"network": net, "security": sec or "none"}
    if net == "ws":
        ws = {"path": path or "/"}
        if host_header:
            ws["headers"] = {"Host": host_header}
        ss["wsSettings"] = ws
    elif net == "grpc":
        ss["grpcSettings"] = {"serviceName": service_name or "",
                              "multiMode": False}
    elif net == "h2":
        h2 = {"path": path or "/"}
        hosts = [h.strip() for h in (host_header or "").split(",")
                 if h.strip()]
        if hosts:
            h2["host"] = hosts
        ss["httpSettings"] = h2
    elif net == "tcp" and (header_type or "").lower() == "http":
        req = {"path": [path or "/"]}
        if host_header:
            req["headers"] = {"Host": [host_header]}
        ss["tcpSettings"] = {"header": {"type": "http",
                                        "request": req}}
    if sec == "tls":
        tls = {"serverName": sni or host_header or "",
               "allowInsecure": bool(allow_insecure)}
        if fingerprint:
            tls["fingerprint"] = fingerprint
        if alpn:
            tls["alpn"] = [a.strip() for a in alpn.split(",")
                           if a.strip()]
        ss["tlsSettings"] = tls
    elif sec == "reality":
        if not public_key:
            raise ValueError("reality node မှာ public key (pbk) မပါပါ")
        ss["realitySettings"] = {
            "serverName": sni or "",
            "fingerprint": fingerprint or "chrome",
            "publicKey": public_key,
            "shortId": short_id or "",
            "spiderX": spider_x or ""}
    return ss


def _parse_vless(link):
    u = urllib.parse.urlparse(link)
    uuid = urllib.parse.unquote(u.username or "")
    host = u.hostname or ""
    port = u.port or 443
    if not _UUID_RE.match(uuid):
        raise ValueError("vless link မှာ UUID မမှန်ပါ")
    if not host or not (1 <= int(port) <= 65535):
        raise ValueError("vless link မှာ server address/port မမှန်ပါ")
    q = {k: v[0] for k, v in
         urllib.parse.parse_qs(u.query).items()}
    user = {"id": uuid,
            "encryption": (q.get("encryption") or "none").lower()}
    if q.get("flow"):
        user["flow"] = q["flow"]
    stream = _stream_settings(
        q.get("type") or "tcp", q.get("security") or "none",
        sni=q.get("sni") or q.get("peer") or "",
        host_header=q.get("host") or "",
        path=urllib.parse.unquote(q.get("path") or ""),
        service_name=q.get("serviceName") or q.get("path") or "",
        fingerprint=q.get("fp") or "",
        alpn=q.get("alpn") or "",
        public_key=q.get("pbk") or "",
        short_id=q.get("sid") or "",
        spider_x=urllib.parse.unquote(q.get("spx") or ""),
        allow_insecure=(q.get("allowInsecure") or "") in ("1", "true"),
        header_type=q.get("headerType") or "")
    outbound = {
        "tag": "proxy", "protocol": "vless",
        "settings": {"vnext": [{"address": host, "port": int(port),
                                "users": [user]}]},
        "streamSettings": stream}
    label = _clean_label(urllib.parse.unquote(u.fragment or ""),
                         f"vless {host}")
    return outbound, label


def _parse_vmess(link):
    raw = _b64_loose(link[len("vmess://"):].split("#")[0])
    if raw is None:
        raise ValueError("vmess link ကို decode လုပ်လို့ မရပါ")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ValueError("vmess link ထဲက JSON မမှန်ပါ")
    host = (obj.get("add") or "").strip()
    uuid = (obj.get("id") or "").strip()
    try:
        port = int(obj.get("port") or 443)
        aid = int(obj.get("aid") or 0)
    except (TypeError, ValueError):
        raise ValueError("vmess link မှာ port/aid မမှန်ပါ")
    if not _UUID_RE.match(uuid):
        raise ValueError("vmess link မှာ UUID မမှန်ပါ")
    if not host or not (1 <= port <= 65535):
        raise ValueError("vmess link မှာ server address/port မမှန်ပါ")
    user = {"id": uuid, "alterId": aid, "security": "auto"}
    stream = _stream_settings(
        obj.get("net") or "tcp",
        "tls" if (obj.get("tls") or "") else "none",
        sni=obj.get("sni") or "",
        host_header=obj.get("host") or "",
        path=obj.get("path") or "",
        service_name=obj.get("path") or "",
        fingerprint=obj.get("fp") or "",
        alpn=obj.get("alpn") or "",
        allow_insecure=False,
        header_type=obj.get("type") or "")
    outbound = {
        "tag": "proxy", "protocol": "vmess",
        "settings": {"vnext": [{"address": host, "port": port,
                                "users": [user]}]},
        "streamSettings": stream}
    label = _clean_label(obj.get("ps") or "", f"vmess {host}")
    return outbound, label


def parse_share_link(link):
    """One vless:// or vmess:// link -> (outbound, label).
    Raises ValueError (Burmese, owner-facing) on anything this
    build cannot run."""
    link = (link or "").strip()
    if link.startswith("vless://"):
        return _parse_vless(link)
    if link.startswith("vmess://"):
        return _parse_vmess(link)
    raise ValueError("vless:// သို့မဟုတ် vmess:// link ပဲ ရပါတယ် "
                     "(ss/trojan မရသေးပါ)")


# The owner's exit country is Singapore (Gemini + OpenAI both
# support it), and a reseller subscription mixes nodes from
# many countries — so selection prefers a node whose remark
# says SG / Singapore over plain list order.
_SG_RE = re.compile(r"singapore|(?<![a-z])sg(?![a-z])", re.IGNORECASE)


def _node_security(outbound):
    ss = outbound.get("streamSettings") or {}
    return (ss.get("security") or "none").lower()


def node_triple(outbound):
    """The node's protocol / transport / security — the only
    node facts ever shown for diagnosis (build 190). Never an
    address, never an id."""
    ss = outbound.get("streamSettings") or {}
    return {"proto": (outbound.get("protocol") or "").lower(),
            "transport": (ss.get("network") or "tcp").lower(),
            "security": _node_security(outbound)}


def _node_encrypted(outbound):
    """(has_encryption, is_vless). VMess carries its own AEAD
    encryption, so it always counts as encrypted; a VLESS node
    is encrypted when its stream security is tls/reality or
    its VLESS encryption field is set to something real."""
    proto = (outbound.get("protocol") or "").lower()
    if proto != "vless":
        return True, False
    if _node_security(outbound) in ("tls", "reality"):
        return True, True
    try:
        user = outbound["settings"]["vnext"][0]["users"][0]
        enc = (user.get("encryption") or "none").lower()
    except Exception:
        enc = "none"
    return enc not in ("", "none"), True


def _host_is_private(host):
    """True only for a literal private / loopback / link-local
    IP. A hostname is NOT a private address (xray's rule is
    about the address itself), so it counts as public."""
    try:
        ip = ipaddress.ip_address((host or "").strip())
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _xray_usable(outbound):
    """Mirror of the rule xray-core enforces at config build
    (build 190, diagnosed live): a VLESS node with no
    encryption at all may only point at a private address —
    to a public server xray refuses to build the config.
    Everything else this parser accepts is buildable."""
    encrypted, is_vless = _node_encrypted(outbound)
    if encrypted or not is_vless:
        return True
    try:
        host = outbound["settings"]["vnext"][0]["address"] or ""
    except Exception:
        host = ""
    return _host_is_private(host)


def _pick_notes(chosen, skipped_sg):
    outbound = chosen[0]
    return {"skipped_sg": skipped_sg,
            "insecure": not _xray_usable(outbound),
            "triple": node_triple(outbound)}


def _select_node(text):
    """Scan a body of text (a subscription payload, decoded or
    not) for share links and choose the node to run.

    Preference order (build 190): SG + encrypted, then any SG
    node xray can build, then the first encrypted node, then
    the first buildable node. A node xray can NEVER build
    (vless with no encryption to a public address) is skipped
    entirely while any buildable node exists — and when the
    SG favourite is skipped for exactly that rule, its label
    is recorded in the notes so the card can say so. When NO
    buildable node exists, the first parseable node is still
    chosen (the save must not hard-fail) and flagged
    `insecure` in the notes: xray will refuse it, and the
    card says a different node/subscription is needed.
    Returns (outbound, label, raw_link, node_count, notes)."""
    links = _LINK_RE.findall(text or "")
    if not links:
        raise ValueError("link မတွေ့ပါ — vless:// သို့မဟုတ် vmess:// "
                         "ပါတဲ့ စာသား ဖြစ်ရပါမယ်")
    parsed = []
    errs = []
    for link in links:
        if not link.startswith(_SUPPORTED):
            continue
        try:
            outbound, label = parse_share_link(link)
            parsed.append((outbound, label, link))
        except ValueError as e:
            errs.append(str(e))
    if not parsed:
        if errs:
            raise ValueError("သုံးလို့ရတဲ့ node မတွေ့ပါ — " + errs[0])
        raise ValueError("vless/vmess node မတွေ့ပါ — ဒီ subscription မှာ "
                         "ss/trojan ပဲ ရှိနေပါတယ် (မရသေးပါ)")

    def _sg(p):
        return bool(_SG_RE.search(p[1] or ""))

    def _enc(p):
        return _node_encrypted(p[0])[0]

    skipped_sg = ""
    for p in parsed:
        if _sg(p) and not _xray_usable(p[0]):
            skipped_sg = p[1]
            break
    usable = [p for p in parsed if _xray_usable(p[0])]
    pool = usable if usable else parsed
    chosen = None
    for pred in (lambda p: _sg(p) and _enc(p),
                 lambda p: _sg(p),
                 lambda p: _enc(p),
                 lambda p: True):
        for p in pool:
            if pred(p):
                chosen = p
                break
        if chosen is not None:
            break
    outbound, label, link = chosen
    return (outbound, label, link, len(parsed),
            _pick_notes(chosen, skipped_sg))


def _fetch_subscription(url):
    """GET a subscription URL and return its body as text.
    Deliberately DIRECT (no proxy): at save time there may be no
    tunnel yet, and subscription hosts are not the blocked party.
    The URL itself is a credential — never logged, never put in
    an exception message."""
    import requests
    try:
        resp = requests.get(
            url, timeout=30,
            headers={"User-Agent": "v2rayNG/1.9.5 "
                                   "(video-translator tunnel setup)"})
        resp.raise_for_status()
    except Exception:
        raise ValueError("subscription URL ကို ဖတ်လို့ မရပါ — "
                         "link မှန်ရဲ့လား စစ်ပေးပါ")
    return resp.text or ""


def resolve_input(raw):
    """Whatever the owner pasted -> (outbound, label, kind,
    nodes, notes): the chosen node's outbound + label, how the
    input arrived, how many parseable nodes the input offered,
    and the build-190 selection notes (skipped SG label,
    insecure flag, chosen triple) from _pick_notes.

    kind: "link" (one or more share links pasted directly),
    "subscription" (an http(s) URL that was fetched), or
    "subscription-paste" (a pasted base64 subscription body)."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("အရင် link သို့မဟုတ် subscription URL ထည့်ပါ")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) == 1 and lines[0].startswith(_SUPPORTED):
        outbound, label = parse_share_link(lines[0])
        chosen = (outbound, label, lines[0])
        return outbound, label, "link", 1, _pick_notes(chosen, "")
    # A lone http(s) URL is a subscription to fetch — checked
    # BEFORE the link-list branch, which would otherwise eat it
    # (a URL contains "://" too).
    if re.fullmatch(r"https?://\S+", raw):
        body = _fetch_subscription(raw)
        try:
            outbound, label, _l, nodes, notes = _select_node(body)
            return outbound, label, "subscription", nodes, notes
        except ValueError:
            decoded = _b64_loose(body)
            if decoded:
                outbound, label, _l, nodes, notes = _select_node(
                    decoded.decode("utf-8", "replace"))
                return outbound, label, "subscription", nodes, notes
            raise
    if lines and all("://" in ln for ln in lines):
        outbound, label, _link, nodes, notes = _select_node(raw)
        return outbound, label, "link", nodes, notes
    decoded = _b64_loose(raw)
    if decoded:
        outbound, label, _l, nodes, notes = _select_node(
            decoded.decode("utf-8", "replace"))
        return outbound, label, "subscription-paste", nodes, notes
    raise ValueError("ပုံစံ မမှန်ပါ — vless:// / vmess:// link, "
                     "subscription URL, သို့မဟုတ် subscription "
                     "စာသား ထည့်ပါ")


def render_config(outbound):
    """The full xray client config: ONE local HTTP inbound on
    loopback, the owner's node as the only outbound (no routing
    rules — the local proxy is reachable only from this machine
    and is used only by the two engine clients)."""
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [{
            "tag": "tun-in", "listen": "127.0.0.1",
            "port": PROXY_PORT, "protocol": "http",
            "settings": {}}],
        "outbounds": [outbound]}


# ---------------- save / clear / status ----------------

def save_link(raw):
    """Validate + parse the owner's paste, persist the rendered
    config (tun-xray.json, chmod 600) and the bookkeeping .env
    lines. Returns {"label": ..., "kind": ..., "nodes": ...,
    "skipped_sg": ..., "insecure": ..., **triple}. Raises
    ValueError with an owner-facing Burmese message; on ANY
    failure nothing is written (a previously working config
    is never clobbered by a bad paste)."""
    outbound, label, kind, nodes, notes = resolve_input(raw)
    conf = render_config(outbound)
    text = json.dumps(conf, ensure_ascii=False, indent=1)
    # Sanity: the rendered config must round-trip as JSON before
    # it is allowed to replace the live one.
    json.loads(text)
    p = _conf_path()
    p.write_text(text, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    _env_set(_ENV_LINK, base64.b64encode(
        (raw or "").strip().encode("utf-8")).decode("ascii"))
    _env_set(_ENV_LABEL, label.replace("\n", " "))
    # Build 190: the SG node skipped for the no-encryption rule
    # (label only — a remark, not a credential) is persisted so
    # the card keeps showing the skip after a page reload.
    _env_set(_ENV_SKIPSG,
             (notes["skipped_sg"] or "").replace("\n", " "))
    # A new config is by definition NOT the verified one: drop
    # the verified pointers at once (the bootstrap re-adds them
    # after it has the new node up and egress-checked). The old
    # node's activation diagnosis is dropped with them — it
    # described a config that no longer exists.
    _env_set(_ENV_PROXY, "")
    _env_set(_ENV_COUNTRY, "")
    _env_set(_ENV_SHA, "")
    _remove_status_file()
    return {"label": label, "kind": kind, "nodes": nodes,
            "skipped_sg": notes["skipped_sg"],
            "insecure": notes["insecure"],
            **notes["triple"]}


def clear():
    """Forget the tunnel completely: config file gone, every
    VT_TUNNEL_* pointer gone (the engines go direct at once);
    the bootstrap stops/disables the xray unit on its next run."""
    try:
        _conf_path().unlink(missing_ok=True)
    except Exception:
        pass
    _remove_status_file()
    for key in (_ENV_LINK, _ENV_LABEL, _ENV_PROXY,
                _ENV_COUNTRY, _ENV_SHA, _ENV_SKIPSG):
        _env_set(key, "")


def configured():
    """A parseable config is saved (says nothing about whether
    the tunnel is up — that is verified())."""
    try:
        json.loads(_conf_path().read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def verified():
    """The bootstrap has THIS config up and egress-checked: a
    proxy pointer exists AND its recorded sha matches the
    config currently on disk."""
    if not _env_get(_ENV_PROXY):
        return False
    sha = _conf_sha()
    return bool(sha) and _env_get(_ENV_SHA) == sha


def proxy_url():
    """The local proxy URL the engine clients must use, or ""
    when the tunnel is not verified up. Read live on every call
    (like the API keys), so a bootstrap verification — or a
    teardown — takes effect without an app restart."""
    if not verified():
        return ""
    return _env_get(_ENV_PROXY)


def requests_proxies():
    """The `proxies` argument for requests calls in the Gemini /
    OpenAI clients: the tunnel dict while verified, else None —
    and proxies=None is byte-for-byte requests' default
    behaviour, so an unused tunnel changes NOTHING anywhere."""
    url = proxy_url()
    if not url:
        return None
    return {"http": url, "https": url}


def _disk_node():
    """(triple, xray_usable) of the config currently on disk,
    or (None, False) when there is none / it is unreadable."""
    try:
        conf = json.loads(_conf_path().read_text(encoding="utf-8"))
        outbound = (conf.get("outbounds") or [{}])[0]
        return node_triple(outbound), _xray_usable(outbound)
    except Exception:
        return None, False


def status():
    """Admin-card status: booleans + label + exit country ONLY.
    Never the link, the config, or any node credential.

    Build 190 adds: the chosen node's triple (proto /
    transport / security), `insecure` when the saved node is
    one xray will refuse (vless, no encryption, public
    address), `skipped_sg` (label of the SG node skipped for
    that rule, "" when none), and — when the bootstrap's last
    activation attempt failed — `fail_kind` / `fail_reason`
    from tun-status.json (sanitized structural text written
    by deploy/activate-settings.sh; the file carries no
    addresses or ids by construction)."""
    conf_saved = configured()
    up = verified()
    st = {"configured": conf_saved,
          "label": _env_get(_ENV_LABEL) if conf_saved else "",
          "up": up,
          "country": _env_get(_ENV_COUNTRY) if up else "",
          "port": PROXY_PORT,
          "skipped_sg": _env_get(_ENV_SKIPSG) if conf_saved else "",
          "insecure": False}
    triple, usable = _disk_node() if conf_saved else (None, False)
    if triple:
        st.update(triple)
        st["insecure"] = not usable
    try:
        diag = json.loads(_status_path().read_text(encoding="utf-8"))
        if isinstance(diag, dict):
            for key in ("proto", "transport", "security"):
                if diag.get(key):
                    st[key] = diag[key]
            if not diag.get("ok"):
                st["fail_kind"] = diag.get("kind") or ""
                st["fail_reason"] = diag.get("reason") or ""
    except Exception:
        pass
    return st


if __name__ == "__main__":
    print("tunnel configured:", configured(),
          "| verified up:", verified(), "| status:", status())
