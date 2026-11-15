"""VPN tunnel for the region-blocked engines — build 189,
engine swapped to sing-box in build 191.

Direct Gemini is LOCATION-blocked and direct OpenAI is
territory-blocked from this VPS (builds 164/168): Google/OpenAI
judge the API caller's IP country, and the server sits in Hong
Kong. The owner already owns a V2Ray subscription, so instead of
moving the server, ONLY the Gemini and OpenAI API calls leave
through a tunnel:

  * sing-box (build 191; builds 189/190 used xray-core) runs on
    the server — installed and supervised by
    deploy/activate-settings.sh (the build-189 bootstrap §13e,
    extracted in build 190) — with ONE inbound: a mixed
    SOCKS/HTTP proxy on 127.0.0.1:18119. "Mixed" keeps the
    build-189 contract byte-for-byte: VT_TUNNEL_PROXY stays
    http://127.0.0.1:18119, which `requests` and curl speak as
    an HTTP proxy, and the same port also answers SOCKS5.
  * pipeline/gemini_translate.py and pipeline/openai_translate.py
    pass that proxy to `requests` EXPLICITLY, per call, and only
    while the tunnel is verified up (see proxy_url()). Nothing
    else in the app passes a proxy anywhere: downloads,
    OpenRouter, xKiro, Apps Script, the free chain, edge-tts and
    Telegram all keep the direct route, and there is no OS-level
    route change of any kind.

Why the engine swap (build 191): xray-core v26 hard-refuses to
build a config whose VLESS node has no TLS/REALITY encryption
and points at a public address ("vless without TLS or other
encryption is prohibited unless the server address is a
private...") — diagnosed live on the owner's server, where the
reseller subscription's usable nodes are exactly that shape and
no encrypted alternative exists in it. sing-box accepts plain
vless nodes, so the tunnel renders sing-box configs now; every
other part of the contract (port, proxy URL, verification
handshake, .env pointers) is unchanged.

Owner flow (mirrors the build-184 Telegram Local API card): the
owner pastes a node into the Admin card — a vless:// or vmess://
share link, several links, a subscription URL, or a pasted
subscription body. THIS module parses it (at save time, in the
app) into a ready-to-run sing-box client config written to
tun-singbox.json in the app dir (chmod 600; server state like
users.db — never in git, survives the no-delete deploys). The
raw paste is kept base64'd in .env (VT_TUNNEL_LINK_B64, chmod
600) so the save can be re-done; it is NEVER returned by any
endpoint and never logged — status endpoints expose only
booleans, the node label and the exit country.

The activation script can also ask this module to re-render the
config from the saved link (`python3 -m pipeline.tunnel
rerender`, build 191): that is how a saved link picked under an
older build's selection rules — or rendered for the retired
xray engine — becomes a current sing-box config on the next
activation run, with no owner re-save.

Build 192: node egress-failover. A subscription's first node
can simply be dead — the owner's first sing-box activation
validated its config, then failed the egress check on the
picked node and stopped there. The activation now walks the
candidate list this module exposes (`python3 -m pipeline.tunnel
candidates` / `render N`): every usable node in selection
order, each rendered, checked and egress-tested in turn; the
first one actually carrying traffic wins. The saved input is
resolved ONCE per walk and the scanned source text is cached
(tun-candidates.cache, chmod 600 — it carries node credentials
exactly like the rendered config does), so every `render N`
renders the SAME list the walk was planned from, with no
repeated subscription fetch. The winning node's index is
persisted (VT_TUNNEL_NODE_IDX) so a later walk tries the
known-good node first instead of rotating away from it.

Verification handshake with the activation script: after
sing-box is up AND an egress check through the proxy has
answered, the script writes VT_TUNNEL_PROXY + VT_TUNNEL_COUNTRY
+ VT_TUNNEL_CONF_SHA (the sha256 of the config it verified)
into .env. proxy_url() below honours VT_TUNNEL_PROXY only while
the sha still matches the config on disk — so the moment the
owner saves a DIFFERENT node, the old tunnel stops being
trusted until the script has verified the new one, and deleting
the settings (clear()) removes the pointers at once.

Build 194: HTTP-Upgrade transport. The owner's subscription
turned out to carry VLESS nodes whose transport is
**httpupgrade** (his phone's proxy app shows "VLESS /
HTTPUPGRADE" for the Singapore nodes) — a transport this
module did not know, so those nodes never became usable
candidates and the build-192 walk exhausted the rest. The
parser now accepts type=httpupgrade (vless query param, or
the vmess JSON `net` field) and renders sing-box's
{"type": "httpupgrade", "host": ..., "path": ...} transport
block (validated against the pinned sing-box v1.14.4 binary,
which also proves XHTTP is NOT available: it rejects
{"type": "xhttp"} as an unknown transport, so xhttp nodes
stay cleanly skipped). The renderer also gained the guard
this incident argues for: an unrecognised transport value
now raises instead of falling through to a plain-tcp render
— a config sing-box would validate that is wrong on the
wire.

Supported node formats: vless and vmess share links with
tcp / ws / grpc / h2 / httpupgrade transports and none / tls
/ reality security (the shapes real subscriptions
overwhelmingly use). Shadowsocks / trojan links and
kcp / quic / xhttp transports are recognised but NOT
supported. Two xray-only shapes have no sing-box equivalent
and degrade to plain tcp silently: tcp "http" header
camouflage and reality spiderX. Node selection
from a subscription (the owner's purchase is a reseller
subscription URL with nodes in many countries): a node whose
remark names Singapore (SG) wins — that is the owner's exit and
a Gemini/OpenAI-supported country; otherwise the first usable
node is taken and its label is shown on the Admin card so a
wrong-country pick is never silent. When an SG node exists but
cannot be used (unsupported format) and a non-SG node wins
instead, the SG node's label is recorded and shown on the card
(skipped_sg).

The activation script's own diagnosis of the last attempt
(tun-status.json — sanitized, structural only, carries the
engine name since build 191) is merged into status() as
fail_kind / fail_reason.
"""
import base64
import hashlib
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

from . import envfile
from .common import PROJECT_ROOT

PROXY_PORT = 18119
PROXY_URL = f"http://127.0.0.1:{PROXY_PORT}"
CONF_NAME = "tun-singbox.json"
LEGACY_CONF_NAME = "tun-xray.json"  # builds 189/190; removed on sight
CACHE_NAME = "tun-candidates.cache"  # build 192: resolved source text
ENGINE = "sing-box"

_ENV_LINK = "VT_TUNNEL_LINK_B64"
_ENV_LABEL = "VT_TUNNEL_LABEL"
_ENV_PROXY = "VT_TUNNEL_PROXY"
_ENV_COUNTRY = "VT_TUNNEL_COUNTRY"
_ENV_SHA = "VT_TUNNEL_CONF_SHA"
_ENV_SKIPSG = "VT_TUNNEL_SKIPPED_SG"
_ENV_NODE_IDX = "VT_TUNNEL_NODE_IDX"  # build 192: failover winner

_STATUS_NAME = "tun-status.json"

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_LINK_RE = re.compile(r"(?:vless|vmess|ss|trojan)://[^\s\"'<>]+")
_SUPPORTED = ("vless://", "vmess://")
_TRANSPORTS = ("tcp", "ws", "grpc", "h2", "httpupgrade")
_VMESS_SECURITIES = ("auto", "aes-128-gcm", "chacha20-poly1305",
                     "none", "zero")


# ---------------- storage paths / .env ----------------

def _conf_path():
    return PROJECT_ROOT / CONF_NAME


def _legacy_conf_path():
    return PROJECT_ROOT / LEGACY_CONF_NAME


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
    """Set or remove (value "") one .env line, chmod 600 — via
    pipeline.envfile (build 192): fresh read inside an flock,
    foreign lines preserved byte-for-byte, atomic replace. The
    pre-192 hand-rolled copy of this surgery (like app.py's)
    could lose foreign keys when writers overlapped; see
    envfile.py for the incident analysis."""
    envfile.update(_env_path(), key, value if value else None)


def _conf_sha():
    try:
        return hashlib.sha256(_conf_path().read_bytes()).hexdigest()
    except Exception:
        return ""


def _status_path():
    """The activation script's diagnosis of the last tunnel
    activation (build 190): written by
    deploy/activate-settings.sh, holds only a failure kind + a
    SANITIZED structural reason + the node's
    protocol/transport/security triple + the engine name — no
    addresses, no ids, no config contents, by construction."""
    return PROJECT_ROOT / _STATUS_NAME


def _remove_status_file():
    try:
        _status_path().unlink(missing_ok=True)
    except Exception:
        pass


def _remove_legacy_conf():
    """The xray-era rendered config (builds 189/190) must not
    linger: nothing reads it any more, and it carries the node
    credential."""
    try:
        _legacy_conf_path().unlink(missing_ok=True)
    except Exception:
        pass


# ---------------- share-link parsing ----------------
# Parsers produce a NEUTRAL node dict; render_config() turns it
# into the sing-box outbound. Node fields: proto, address, port,
# uuid, flow, encryption, packet_encoding (vless), alter_id,
# vmess_security (vmess), transport, security, sni, host, path,
# service_name, fingerprint, alpn, public_key, short_id,
# allow_insecure.

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


def _node_base(network, security, *, sni="", host_header="",
               path="", service_name="", fingerprint="",
               alpn="", public_key="", short_id="",
               allow_insecure=False):
    """Validate + normalise the transport/security half of a
    node. Raises ValueError for transports/security this build
    does not implement (kcp/quic/xhttp, unknown security) — the
    caller then tries the next subscription node."""
    net = (network or "tcp").lower()
    if net in ("http", "h2"):
        net = "h2"
    if net == "splithttp":
        net = "xhttp"
    if net not in _TRANSPORTS:
        raise ValueError(f"transport '{network}' မရသေးပါ (tcp/ws/grpc/h2/httpupgrade ပဲ ရပါတယ်)")
    sec = (security or "none").lower()
    if sec not in ("none", "", "tls", "reality"):
        raise ValueError(f"security '{security}' မရသေးပါ")
    if sec == "reality" and not public_key:
        raise ValueError("reality node မှာ public key (pbk) မပါပါ")
    return {"transport": net, "security": sec or "none",
            "sni": sni or "", "host": host_header or "",
            "path": path or "", "service_name": service_name or "",
            "fingerprint": fingerprint or "", "alpn": alpn or "",
            "public_key": public_key or "",
            "short_id": short_id or "",
            "allow_insecure": bool(allow_insecure)}


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
    node = _node_base(
        q.get("type") or "tcp", q.get("security") or "none",
        sni=q.get("sni") or q.get("peer") or "",
        host_header=q.get("host") or "",
        path=urllib.parse.unquote(q.get("path") or ""),
        service_name=q.get("serviceName") or q.get("path") or "",
        fingerprint=q.get("fp") or "",
        alpn=q.get("alpn") or "",
        public_key=q.get("pbk") or "",
        short_id=q.get("sid") or "",
        allow_insecure=(q.get("allowInsecure") or "") in ("1", "true"))
    node.update({
        "proto": "vless", "address": host, "port": int(port),
        "uuid": uuid,
        "encryption": (q.get("encryption") or "none").lower(),
        "flow": q.get("flow") or "",
        "packet_encoding": (q.get("packetEncoding")
                            or q.get("packet-encoding") or "").lower(),
        "alter_id": 0, "vmess_security": "auto"})
    label = _clean_label(urllib.parse.unquote(u.fragment or ""),
                         f"vless {host}")
    return node, label


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
    node = _node_base(
        obj.get("net") or "tcp",
        "tls" if (obj.get("tls") or "") else "none",
        sni=obj.get("sni") or "",
        host_header=obj.get("host") or "",
        path=obj.get("path") or "",
        service_name=obj.get("path") or "",
        fingerprint=obj.get("fp") or "",
        alpn=obj.get("alpn") or "",
        allow_insecure=False)
    scy = (obj.get("scy") or "auto").lower()
    node.update({
        "proto": "vmess", "address": host, "port": port,
        "uuid": uuid, "encryption": "none", "flow": "",
        "packet_encoding": "",
        "alter_id": aid,
        "vmess_security": scy if scy in _VMESS_SECURITIES else "auto"})
    label = _clean_label(obj.get("ps") or "", f"vmess {host}")
    return node, label


def parse_share_link(link):
    """One vless:// or vmess:// link -> (node, label).
    Raises ValueError (Burmese, owner-facing) on anything this
    build cannot run."""
    link = (link or "").strip()
    if link.startswith("vless://"):
        return _parse_vless(link)
    if link.startswith("vmess://"):
        return _parse_vmess(link)
    raise ValueError("vless:// သို့မဟုတ် vmess:// link ပဲ ရပါတယ် "
                     "(ss/trojan မရသေးပါ)")


def node_triple(node):
    """The node's protocol / transport / security — the only
    node facts ever shown for diagnosis (build 190). Never an
    address, never an id."""
    return {"proto": (node.get("proto") or "").lower(),
            "transport": (node.get("transport") or "tcp").lower(),
            "security": (node.get("security") or "none").lower()}


# ---------------- sing-box rendering ----------------

def render_outbound(node):
    """The sing-box outbound for one parsed node."""
    ob = {"type": node["proto"], "tag": "proxy",
          "server": node["address"], "server_port": node["port"],
          "uuid": node["uuid"]}
    if node["proto"] == "vless":
        if node.get("flow"):
            ob["flow"] = node["flow"]
        enc = (node.get("encryption") or "none").lower()
        if enc not in ("", "none"):
            ob["encryption"] = node["encryption"]
        if node.get("packet_encoding") in ("xudp", "packetaddr"):
            ob["packet_encoding"] = node["packet_encoding"]
    else:  # vmess carries its own AEAD security + alterId
        ob["alter_id"] = int(node.get("alter_id") or 0)
        ob["security"] = node.get("vmess_security") or "auto"
    net = node.get("transport") or "tcp"
    if net == "ws":
        ws = {"type": "ws", "path": node.get("path") or "/"}
        if node.get("host"):
            ws["headers"] = {"Host": node["host"]}
        ob["transport"] = ws
    elif net == "grpc":
        ob["transport"] = {"type": "grpc",
                           "service_name": node.get("service_name") or ""}
    elif net == "h2":
        h2 = {"type": "http", "path": node.get("path") or "/"}
        hosts = [h.strip() for h in (node.get("host") or "").split(",")
                 if h.strip()]
        if hosts:
            h2["host"] = hosts
        ob["transport"] = h2
    elif net == "httpupgrade":
        # Build 194: the owner's reseller nodes are VLESS over
        # HTTP-Upgrade (Happ shows "VLESS / HTTPUPGRADE").
        # sing-box's httpupgrade transport takes the upgrade
        # Host and path as dedicated fields; empty fields are
        # omitted (the binary accepts the bare block and the
        # server-side defaults then apply).
        hu = {"type": "httpupgrade"}
        if node.get("host"):
            hu["host"] = node["host"]
        if node.get("path"):
            hu["path"] = node["path"]
        ob["transport"] = hu
    elif net == "tcp":
        # tcp: no transport block (sing-box default). The
        # xray-era tcp "http" header camouflage has no sing-box
        # equivalent and is dropped (plain tcp) — see the
        # module docstring.
        pass
    else:
        # Build 194: NEVER fall through to plain tcp. Before
        # this guard, an unrecognised transport value rendered
        # with no transport block at all — a config sing-box
        # validates happily that is wrong on the wire (the
        # server waits for a handshake that never comes, and
        # every egress check fails). _node_base() already
        # rejects such nodes at parse time; this raise makes
        # the renderer itself uphold the same contract for any
        # node dict it is handed.
        raise ValueError(f"transport '{net}' မရသေးပါ "
                         "(tcp/ws/grpc/h2/httpupgrade ပဲ ရပါတယ်)")
    sec = node.get("security") or "none"
    if sec == "tls":
        tls = {"enabled": True, "insecure": bool(node.get("allow_insecure"))}
        # SNI fallback: the first Host value (the host field can
        # be a comma list for h2 — a list is not a valid SNI).
        sni = node.get("sni") or ""
        if not sni and node.get("host"):
            sni = node["host"].split(",")[0].strip()
        if sni:
            tls["server_name"] = sni
        if node.get("alpn"):
            tls["alpn"] = [a.strip() for a in node["alpn"].split(",")
                           if a.strip()]
        if node.get("fingerprint"):
            tls["utls"] = {"enabled": True,
                           "fingerprint": node["fingerprint"]}
        ob["tls"] = tls
    elif sec == "reality":
        tls = {"enabled": True,
               "utls": {"enabled": True,
                        "fingerprint": node.get("fingerprint") or "chrome"},
               "reality": {"enabled": True,
                           "public_key": node.get("public_key") or "",
                           "short_id": node.get("short_id") or ""}}
        if node.get("sni"):
            tls["server_name"] = node["sni"]
        ob["tls"] = tls
    return ob


def render_config(node):
    """The full sing-box client config: ONE local mixed
    (SOCKS + HTTP) inbound on loopback, the owner's node as the
    only outbound (no routing rules — the local proxy is
    reachable only from this machine and is used only by the
    two engine clients)."""
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [{
            "type": "mixed", "tag": "tun-in", "listen": "127.0.0.1",
            "listen_port": PROXY_PORT}],
        "outbounds": [render_outbound(node)]}


# ---------------- selection / input resolution ----------------

# The owner's exit country is Singapore (Gemini + OpenAI both
# support it), and a reseller subscription mixes nodes from
# many countries — so selection prefers a node whose remark
# says SG / Singapore over plain list order.
_SG_RE = re.compile(r"singapore|(?<![a-z])sg(?![a-z])", re.IGNORECASE)


def _link_label_hint(link):
    """Best-effort remark for a link we could NOT use (an
    unsupported scheme, or a parse failure): the #fragment for
    vless/ss/trojan, the `ps` field for vmess. Label only —
    remarks are not credentials."""
    frag = ""
    if "#" in link:
        frag = urllib.parse.unquote(link.split("#", 1)[1]).strip()
    if frag:
        return _clean_label(frag, "")
    if link.startswith("vmess://"):
        raw = _b64_loose(link[len("vmess://"):].split("#")[0])
        if raw:
            try:
                return _clean_label(
                    json.loads(raw.decode("utf-8")).get("ps") or "", "")
            except Exception:
                return ""
    return ""


def _pick_notes(chosen, skipped_sg):
    node = chosen[0]
    return {"skipped_sg": skipped_sg,
            "triple": node_triple(node)}


def _candidates_from_text(text):
    """Scan a body of text (a subscription payload, decoded or
    not) for share links and return EVERY usable node in
    selection order (build 192): SG-labelled nodes first
    (stable, in input order), then the rest in input order —
    element 0 is the node builds ≤191 picked. "Usable" = a
    vless/vmess link this build can parse and render —
    sing-box accepts plain (unencrypted-transport) nodes, so
    the build-190 xray encryption mirror no longer applies.

    Also returns the skipped-SG note: when an SG node exists in
    the input but cannot be used (an unsupported scheme like
    ss/trojan, or a parse failure) AND no usable SG node
    exists at all, the unusable SG node's label is returned so
    the card can say it was skipped and why. Raises ValueError
    (owner-facing Burmese) when nothing is usable."""
    links = _LINK_RE.findall(text or "")
    if not links:
        raise ValueError("link မတွေ့ပါ — vless:// သို့မဟုတ် vmess:// "
                         "ပါတဲ့ စာသား ဖြစ်ရပါမယ်")
    parsed = []
    errs = []
    skipped_sg = ""
    for link in links:
        if not link.startswith(_SUPPORTED):
            if not skipped_sg:
                lab = _link_label_hint(link)
                if lab and _SG_RE.search(lab):
                    skipped_sg = lab
            continue
        try:
            node, label = parse_share_link(link)
            parsed.append((node, label, link))
        except ValueError as e:
            errs.append(str(e))
            if not skipped_sg:
                lab = _link_label_hint(link)
                if lab and _SG_RE.search(lab):
                    skipped_sg = lab
    if not parsed:
        if errs:
            raise ValueError("သုံးလို့ရတဲ့ node မတွေ့ပါ — " + errs[0])
        raise ValueError("vless/vmess node မတွေ့ပါ — ဒီ subscription မှာ "
                         "ss/trojan ပဲ ရှိနေပါတယ် (မရသေးပါ)")
    sg = [p for p in parsed if _SG_RE.search(p[1] or "")]
    rest = [p for p in parsed if not _SG_RE.search(p[1] or "")]
    if sg:
        skipped_sg = ""  # a usable SG node exists — nothing skipped
    return sg + rest, skipped_sg


def _select_node(text):
    """The pre-192 single pick, kept for resolve_input():
    element 0 of the candidate order + the selection notes.
    Returns (node, label, raw_link, node_count, notes)."""
    ordered, skipped_sg = _candidates_from_text(text)
    node, label, link = ordered[0]
    return (node, label, link, len(ordered),
            _pick_notes(ordered[0], skipped_sg))


def _fetch_subscription(url):
    """GET a subscription URL and return its body as text.
    Deliberately DIRECT (no proxy): at save time there may be no
    tunnel yet, and subscription hosts are not the blocked party.
    The URL itself is a credential — never logged, never put in
    an exception message."""
    try:
        import requests
    except Exception:
        raise ValueError("subscription URL ကို ဖတ်လို့ မရပါ — "
                         "link မှန်ရဲ့လား စစ်ပေးပါ")
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


def _scan_text(raw):
    """Whatever the owner pasted -> (text_to_scan, kind): the
    plain text the share links are scanned from, and how the
    input arrived. A subscription URL is fetched ONCE, here;
    callers that need the same resolution again (the build-192
    failover walk) reuse this text via the candidates cache
    instead of fetching again.

    kind: "link" (one or more share links pasted directly),
    "subscription" (an http(s) URL that was fetched), or
    "subscription-paste" (a pasted base64 subscription body)."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("အရင် link သို့မဟုတ် subscription URL ထည့်ပါ")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) == 1 and lines[0].startswith(_SUPPORTED):
        return lines[0], "link"
    # A lone http(s) URL is a subscription to fetch — checked
    # BEFORE the link-list branch, which would otherwise eat it
    # (a URL contains "://" too).
    if re.fullmatch(r"https?://\S+", raw):
        body = _fetch_subscription(raw)
        if _LINK_RE.search(body or ""):
            return body, "subscription"
        decoded = _b64_loose(body)
        if decoded:
            return decoded.decode("utf-8", "replace"), "subscription"
        # Nothing scannable either way: let the candidate scan
        # raise its own owner-facing error on the raw body.
        return body, "subscription"
    if lines and all("://" in ln for ln in lines):
        return raw, "link"
    decoded = _b64_loose(raw)
    if decoded:
        return decoded.decode("utf-8", "replace"), "subscription-paste"
    raise ValueError("ပုံစံ မမှန်ပါ — vless:// / vmess:// link, "
                     "subscription URL, သို့မဟုတ် subscription "
                     "စာသား ထည့်ပါ")


def _resolve_candidates(raw):
    """Full resolution of a paste: (ordered_candidates,
    kind, skipped_sg, scan_text). ordered_candidates is the
    _candidates_from_text list of (node, label, raw_link)."""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("အရင် link သို့မဟုတ် subscription URL ထည့်ပါ")
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) == 1 and lines[0].startswith(_SUPPORTED):
        # The single-link fast path keeps the parser's own
        # precise error messages (the pre-192 behaviour).
        node, label = parse_share_link(lines[0])
        return [(node, label, lines[0])], "link", "", lines[0]
    text, kind = _scan_text(raw)
    ordered, skipped_sg = _candidates_from_text(text)
    return ordered, kind, skipped_sg, text


def resolve_input(raw):
    """Whatever the owner pasted -> (node, label, kind, nodes,
    notes): the chosen node (candidate 0) + label, how the
    input arrived, how many parseable nodes the input offered,
    and the selection notes (skipped SG label, chosen triple)
    from _pick_notes."""
    ordered, kind, skipped_sg, _text = _resolve_candidates(raw)
    node, label, _link = ordered[0]
    return node, label, kind, len(ordered), \
        _pick_notes(ordered[0], skipped_sg)


# ---------------- candidates cache (build 192) ----------------
# The failover walk resolves the saved input ONCE (candidates)
# and then renders candidates one by one (render N), each in
# its own CLI process. Re-resolving per render would re-fetch a
# subscription URL every time and could even return a DIFFERENT
# list mid-walk. So the scanned source text is cached next to
# the config, keyed by the sha256 of the saved-link value it
# came from: while the saved link is unchanged, every render
# scans the cached text. The cache carries node credentials
# (it IS the subscription body) — chmod 600, gitignored,
# removed by clear(), exactly like the rendered config.

def _cache_path():
    return PROJECT_ROOT / CACHE_NAME


def _write_cache(link_b64, scan_text):
    payload = json.dumps(
        {"link_sha": hashlib.sha256(
            (link_b64 or "").encode("utf-8")).hexdigest(),
         "text": scan_text},
        ensure_ascii=False)
    p = _cache_path()
    try:
        if p.read_text(encoding="utf-8") == payload:
            return
    except Exception:
        pass
    p.write_text(payload, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass


def _read_cache(link_b64):
    """The cached scan text for THIS saved link, or None."""
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        if data.get("link_sha") == hashlib.sha256(
                (link_b64 or "").encode("utf-8")).hexdigest():
            return data.get("text") or ""
    except Exception:
        pass
    return None


def _remove_cache():
    try:
        _cache_path().unlink(missing_ok=True)
    except Exception:
        pass


def _resolve_saved():
    """Resolve the link stored in .env to the ordered candidate
    list: (ordered, kind, skipped_sg). Returns None when no
    link is saved; raises ValueError when the saved input no
    longer resolves. Uses (and refreshes) the candidates cache,
    so a subscription URL is fetched at most once per walk."""
    b64 = _env_get(_ENV_LINK)
    if not b64:
        return None
    cached = _read_cache(b64)
    if cached is not None:
        try:
            ordered, skipped_sg = _candidates_from_text(cached)
            return ordered, "cache", skipped_sg
        except ValueError:
            pass  # stale/corrupt cache — resolve fresh below
    raw_bytes = _b64_loose(b64)
    if raw_bytes is None:
        raise ValueError("သိမ်းထားတဲ့ link ကို decode လုပ်လို့ မရပါ")
    raw = raw_bytes.decode("utf-8", "replace")
    ordered, kind, skipped_sg, text = _resolve_candidates(raw)
    _write_cache(b64, text)
    return ordered, kind, skipped_sg


def _refresh_bookkeeping(label, skipped_sg):
    _env_set(_ENV_LABEL, (label or "").replace("\n", " "))
    _env_set(_ENV_SKIPSG, (skipped_sg or "").replace("\n", " "))


def saved_candidates():
    """The build-192 failover surface for the activation script
    (`python3 -m pipeline.tunnel candidates`): every usable
    node in the saved input, in selection order, as
    [{"label": ..., "proto": ..., "transport": ...,
    "security": ...}] — labels + triples ONLY, never an
    address or an id. Returns None when no link is saved;
    raises ValueError when the saved input no longer resolves.
    Refreshes the label / skipped-SG bookkeeping to the default
    pick, exactly like a re-render."""
    res = _resolve_saved()
    if res is None:
        return None
    ordered, _kind, skipped_sg = res
    _remove_legacy_conf()
    _refresh_bookkeeping(ordered[0][1], skipped_sg)
    return [{"label": label, **node_triple(node)}
            for node, label, _link in ordered]


def render_candidate(index):
    """Render candidate `index` (selection order, build 192) to
    tun-singbox.json for the activation's failover walk.
    Returns {"label": ..., **triple} of the rendered node.
    Raises ValueError on a bad index or an unresolvable saved
    input. Does NOT touch the verified pointers — the
    activation owns them during the walk (and the app-side sha
    check distrusts the old pointers the moment the config
    bytes change anyway)."""
    res = _resolve_saved()
    if res is None:
        raise ValueError("သိမ်းထားတဲ့ link မရှိပါ")
    ordered, _kind, _skip = res
    if not (0 <= int(index) < len(ordered)):
        raise ValueError("node နံပါတ် မမှန်ပါ")
    node, label, _link = ordered[int(index)]
    _write_conf(node)
    _remove_legacy_conf()
    return {"label": label, **node_triple(node)}


# ---------------- save / clear / status ----------------

def _write_conf(node):
    """Render + persist the sing-box config (chmod 600). Returns
    True when the bytes on disk changed. The rendered config
    must round-trip as JSON before it may replace the live one."""
    conf = render_config(node)
    text = json.dumps(conf, ensure_ascii=False, indent=1)
    json.loads(text)
    p = _conf_path()
    try:
        if p.read_text(encoding="utf-8") == text:
            return False
    except Exception:
        pass
    p.write_text(text, encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return True


def _drop_verified_pointers():
    """A changed config is by definition NOT the verified one:
    drop the verified pointers at once (the activation script
    re-adds them after it has the new node up and
    egress-checked). The old node's activation diagnosis is
    dropped with them — it described a config that no longer
    exists."""
    _env_set(_ENV_PROXY, "")
    _env_set(_ENV_COUNTRY, "")
    _env_set(_ENV_SHA, "")
    _remove_status_file()


def save_link(raw):
    """Validate + parse the owner's paste, persist the rendered
    sing-box config (tun-singbox.json, chmod 600) and the
    bookkeeping .env lines. Returns {"label": ..., "kind": ...,
    "nodes": ..., "skipped_sg": ..., **triple}. Raises
    ValueError with an owner-facing Burmese message; on ANY
    failure nothing is written (a previously working config
    is never clobbered by a bad paste)."""
    ordered, kind, skipped_sg, text = _resolve_candidates(raw)
    node, label, _link = ordered[0]
    notes = _pick_notes(ordered[0], skipped_sg)
    _write_conf(node)
    _remove_legacy_conf()
    link_b64 = base64.b64encode(
        (raw or "").strip().encode("utf-8")).decode("ascii")
    _env_set(_ENV_LINK, link_b64)
    _env_set(_ENV_LABEL, label.replace("\n", " "))
    # The SG node skipped for format reasons (label only — a
    # remark, not a credential) is persisted so the card keeps
    # showing the skip after a page reload.
    _env_set(_ENV_SKIPSG,
             (notes["skipped_sg"] or "").replace("\n", " "))
    # A fresh paste invalidates the old failover winner hint
    # (build 192): the indices belong to the OLD input's list.
    _env_set(_ENV_NODE_IDX, "")
    # Cache the scanned source text so the activation's
    # failover walk renders this exact list (build 192).
    _write_cache(link_b64, text)
    _drop_verified_pointers()
    return {"label": label, "kind": kind, "nodes": len(ordered),
            "skipped_sg": notes["skipped_sg"],
            **notes["triple"]}


def rerender_from_saved():
    """Re-render tun-singbox.json from the link stored in .env
    (build 191) — invoked by deploy/activate-settings.sh as
    `python3 -m pipeline.tunnel rerender`, so a link saved
    under an older build (an xray-format render, or an older
    selection) becomes a current sing-box config on the next
    activation run WITHOUT the owner re-saving. Refreshes the
    label / skipped-SG bookkeeping to the fresh selection, and
    when the rendered bytes change, drops the verified pointers
    exactly like a fresh save (the activation re-verifies
    before anything trusts the tunnel again). Returns True
    when a saved link exists and rendered (or was already
    current); False when no link is saved. Raises ValueError
    when the saved input no longer resolves."""
    res = _resolve_saved()
    if res is None:
        return False
    ordered, _kind, skipped_sg = res
    node, label, _link = ordered[0]
    changed = _write_conf(node)
    _remove_legacy_conf()
    _refresh_bookkeeping(label, skipped_sg)
    if changed:
        _drop_verified_pointers()
    return True


def clear():
    """Forget the tunnel completely: config files gone, every
    VT_TUNNEL_* pointer gone (the engines go direct at once);
    the activation script stops/disables the tunnel unit on
    its next run."""
    try:
        _conf_path().unlink(missing_ok=True)
    except Exception:
        pass
    _remove_legacy_conf()
    _remove_cache()
    _remove_status_file()
    for key in (_ENV_LINK, _ENV_LABEL, _ENV_PROXY,
                _ENV_COUNTRY, _ENV_SHA, _ENV_SKIPSG,
                _ENV_NODE_IDX):
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
    """The activation script has THIS config up and
    egress-checked: a proxy pointer exists AND its recorded
    sha matches the config currently on disk."""
    if not _env_get(_ENV_PROXY):
        return False
    sha = _conf_sha()
    return bool(sha) and _env_get(_ENV_SHA) == sha


def proxy_url():
    """The local proxy URL the engine clients must use, or ""
    when the tunnel is not verified up. Read live on every call
    (like the API keys), so an activation verification — or a
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


def _disk_triple():
    """The triple of the config currently on disk, read from
    the sing-box shape, or None when there is none / it is
    unreadable. Transport/security are reported in the same
    vocabulary as node_triple() (sing-box's http transport is
    h2; TLS-with-reality reports "reality")."""
    try:
        conf = json.loads(_conf_path().read_text(encoding="utf-8"))
        ob = (conf.get("outbounds") or [{}])[0]
        transport = ((ob.get("transport") or {}).get("type")
                     or "tcp").lower()
        if transport == "http":
            transport = "h2"
        tls = ob.get("tls") or {}
        if (tls.get("reality") or {}).get("enabled"):
            security = "reality"
        elif tls.get("enabled"):
            security = "tls"
        else:
            security = "none"
        return {"proto": (ob.get("type") or "").lower(),
                "transport": transport, "security": security}
    except Exception:
        return None


def status():
    """Admin-card status: booleans + label + exit country ONLY.
    Never the link, the config, or any node credential.

    Also carries: the engine name (build 191), the chosen
    node's triple (proto / transport / security), `skipped_sg`
    (label of an SG node skipped for format reasons while a
    non-SG node won, "" when none), the failover walk's
    `tried` / `nodes` counts (build 192, when the last
    activation recorded them), and — when the activation
    script's last attempt failed — `fail_kind` / `fail_reason`
    from tun-status.json (sanitized structural text written by
    deploy/activate-settings.sh; the file carries no addresses
    or ids by construction)."""
    conf_saved = configured()
    up = verified()
    st = {"configured": conf_saved,
          "label": _env_get(_ENV_LABEL) if conf_saved else "",
          "up": up,
          "country": _env_get(_ENV_COUNTRY) if up else "",
          "port": PROXY_PORT,
          "engine": ENGINE,
          "skipped_sg": _env_get(_ENV_SKIPSG) if conf_saved else ""}
    triple = _disk_triple() if conf_saved else None
    if triple:
        st.update(triple)
    try:
        diag = json.loads(_status_path().read_text(encoding="utf-8"))
        if isinstance(diag, dict):
            for key in ("proto", "transport", "security"):
                if diag.get(key):
                    st[key] = diag[key]
            if diag.get("engine"):
                st["engine"] = diag["engine"]
            # Build 192: how far the failover walk got (the
            # winning node's label is the status `label` above —
            # the activation persists it in VT_TUNNEL_LABEL).
            for key in ("tried", "nodes"):
                if isinstance(diag.get(key), int) and diag[key]:
                    st[key] = diag[key]
            if not diag.get("ok"):
                st["fail_kind"] = diag.get("kind") or ""
                st["fail_reason"] = diag.get("reason") or ""
    except Exception:
        pass
    return st


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rerender":
        # Build 191: the activation script's re-render entry
        # point. Tokens only on stdout — never the link, never
        # node details. Exit: 0 rendered/current, 2 no saved
        # link, 1 saved link no longer resolves.
        try:
            done = rerender_from_saved()
        except ValueError:
            print("RERENDER_FAIL")
            sys.exit(1)
        print("RERENDER_OK" if done else "RERENDER_NOLINK")
        sys.exit(0 if done else 2)
    if len(sys.argv) > 1 and sys.argv[1] == "candidates":
        # Build 192: the failover walk's planning call. Prints
        # ONE JSON line {"count": N, "candidates": [{label,
        # proto, transport, security}, ...]} — labels + triples
        # only, never an address or an id. Exit: 0 ok, 2 no
        # saved link, 1 saved link no longer resolves.
        try:
            cands = saved_candidates()
        except ValueError:
            print("CANDIDATES_FAIL")
            sys.exit(1)
        if cands is None:
            print("CANDIDATES_NOLINK")
            sys.exit(2)
        print(json.dumps({"count": len(cands),
                          "candidates": cands},
                         ensure_ascii=False))
        sys.exit(0)
    if len(sys.argv) > 2 and sys.argv[1] == "render":
        # Build 192: render candidate N (selection order) to
        # tun-singbox.json for the failover walk. Prints ONE
        # JSON line {"label": ..., "proto": ..., ...}. Exit:
        # 0 ok, 2 no saved link, 1 bad index / unresolvable.
        if not _env_get(_ENV_LINK):
            print("RENDER_NOLINK")
            sys.exit(2)
        try:
            info = render_candidate(int(sys.argv[2]))
        except (ValueError, IndexError):
            print("RENDER_FAIL")
            sys.exit(1)
        print(json.dumps(info, ensure_ascii=False))
        sys.exit(0)
    print("tunnel configured:", configured(),
          "| verified up:", verified(), "| status:", status())
