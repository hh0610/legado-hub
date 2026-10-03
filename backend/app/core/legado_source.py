"""Generate the Legado virtual source JSON for the shared subscription library.

The virtual source used to live under ``/api/legado/*``; after the shared
subscription refactor it is exposed at ``/api/subscribe/legado/*``.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import GENERATED_DIR
from app.core.aggregate_config import load_aggregate_config
from app.core.app_config import AppConfig
from app.core.public_security import (
    get_public_base_url,
    is_lan_reading_base,
    normalize_public_base_url,
)


# Reading identifies this source by bookSourceUrl and only offers updates when
# lastUpdateTime increases (not when the display version string changes alone).
#
# Release discipline (do not mix):
# - BETA / daily rule tests: ONLY bump _READER_RULE_RELEASED_AT_MS to wall-clock
#   now (ms). Keep _READER_RULE_VERSION unchanged so the name does not churn.
# Beta / daily rule ships bump RELEASED_AT_MS only; formal tags bump both.
_READER_RULE_VERSION = "0.0.32"
# Last beta marker: audio sibling source (bookSourceType 1) + media filtering (ms).
_READER_RULE_RELEASED_AT_MS = 1790935800000

# Dual source identity: public vs LAN imports coexist in Reading.
_PUBLIC_BOOK_SOURCE_URL = "LegadoHub"
_LAN_BOOK_SOURCE_URL = "LegadoHub-LAN"
_LAN_NAME_MARK = "·内网"
_LAN_GROUP_MARK = "内网"



def _reader_rule_version_stamp(version: str = _READER_RULE_VERSION) -> int:
    """Secondary monotonic component derived from X.Y.Z (not a wall clock)."""
    parts = str(version or "0").strip().split(".")
    nums: list[int] = []
    for part in parts[:3]:
        try:
            nums.append(max(0, int(part)))
        except ValueError:
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    major, minor, patch = nums
    return major * 100_000_000 + minor * 100_000 + patch * 100


def _reader_rule_last_update_time(config: AppConfig) -> int:
    """Reading update signal: max(RELEASED_AT + version stamp, AppConfig mtime).

    Clients key off lastUpdateTime. For beta rule ships, only RELEASED_AT_MS
    needs to increase; the version stamp is stable between formal releases.
    """
    floor = _READER_RULE_RELEASED_AT_MS + _reader_rule_version_stamp()
    try:
        config_modified_at = config.path.stat().st_mtime_ns // 1_000_000
    except OSError:
        return floor
    return max(floor, config_modified_at)


def _login_ui(*, bound_access_code: bool = False) -> str:
    """Reading source sheet: 订阅 + 书库 only (no tip/password/login buttons).

    Avoid type=text tip rows — some clients fail to open the sheet when the only
    non-button control is a fake tip field. Auth is automatic for personal links.
    """
    del bound_access_code  # reserved for future copy variants
    btn = {"layout_flexGrow": 1, "layout_flexBasisPercent": 0.48}
    return json.dumps(
        [
            {
                "name": "订阅",
                "type": "button",
                "action": "legadoHubOpenSubscriptions()",
                "style": dict(btn),
            },
            {
                "name": "书库",
                "type": "button",
                "action": "legadoHubOpenLibrary()",
                "style": dict(btn),
            },
        ],
        ensure_ascii=False,
    )


def _auth_runtime_js(base_api: str, *, access_code: str | None = None) -> str:
    """Shared JS: resolve Bearer from LoginHeader, else redeem bound/form code.

    Used by book-source ``header`` (search/toc/book) and by chapter ``java.ajax``
    so search / subscribe / chapter share the same token logic.
    Does not eval ``loginUrl`` (that previously broke Reading login UI).
    """
    base_literal = json.dumps(str(base_api or "").rstrip("/"), ensure_ascii=False)
    access_literal = json.dumps(str(access_code or ""), ensure_ascii=False)
    return f"""
function legadoHubReadStoredAuth() {{
    try {{
        var raw = source.getLoginHeader();
        var st = typeof raw === "string" ? JSON.parse(raw || "{{}}") : (raw || {{}});
        var a = st && (st.Authorization || st.authorization) || "";
        return String(a || "").trim();
    }} catch (e) {{
        return "";
    }}
}}
function legadoHubReadAccessCode() {{
    var code = {access_literal};
    if (String(code || "").trim()) return String(code).trim();
    try {{
        var info = source.getLoginInfoMap();
        if (!info) return "";
        try {{
            var v = info.get("授权码");
            if (v !== null && v !== undefined && String(v).trim()) return String(v).trim();
        }} catch (e1) {{}}
        try {{
            if (info["授权码"] && String(info["授权码"]).trim()) return String(info["授权码"]).trim();
        }} catch (e2) {{}}
    }} catch (e3) {{}}
    return "";
}}
function legadoHubHasBoundToken() {{
    return !!String({access_literal} || "").trim();
}}
function legadoHubRedeemToAuth() {{
    var code = legadoHubReadAccessCode();
    var base = {base_literal};
    if (!code || !base) return "";
    try {{
        var body = JSON.stringify({{accessCode: code}});
        var opt = {{
            method: "POST",
            headers: {{"Accept": "application/json", "Content-Type": "application/json"}},
            body: body
        }};
        var text = String(java.ajax(base + "/api/auth/access/redeem," + JSON.stringify(opt)) || "").trim();
        if (!text) return "";
        var payload = JSON.parse(text);
        if (!payload || !payload.token) return "";
        var auth = "Bearer " + String(payload.token);
        try {{ source.putLoginHeader(JSON.stringify({{Authorization: auth}})); }} catch (e4) {{}}
        return auth;
    }} catch (e5) {{
        return "";
    }}
}}
function legadoHubResolveAuth() {{
    var auth = legadoHubReadStoredAuth();
    if (auth) return auth;
    // Only auto-redeem when personal link embeds a code, or user already saved 授权码.
    return legadoHubRedeemToAuth();
}}
function legadoHubAuthHeaders() {{
    var h = {{"Accept": "application/json"}};
    try {{
        var auth = legadoHubResolveAuth();
        if (auth) h.Authorization = auth;
    }} catch (e) {{}}
    return h;
}}
function legadoHubAjax(url, method, body) {{
    var opt = {{
        method: String(method || "GET").toUpperCase(),
        headers: legadoHubAuthHeaders()
    }};
    if (body !== undefined && body !== null) {{
        opt.body = typeof body === "string" ? body : JSON.stringify(body);
        if (!opt.headers["Content-Type"]) opt.headers["Content-Type"] = "application/json";
    }}
    return String(java.ajax(String(url || "") + "," + JSON.stringify(opt)) || "");
}}
"""


def _request_header_rule(base_api: str = "", *, access_code: str | None = None) -> str:
    """AnalyzeUrl header: inject stored Bearer only — never network.

    Reading evaluates book-source header when opening the login sheet. Any
    java.ajax / redeem here prevents the sheet from appearing. Token redeem
    happens in searchUrl / legadoHubAjax / loginCheckJs instead.
    """
    del base_api, access_code
    return (
        "@js:\n"
        "var h = {\"Accept\": \"application/json\"};\n"
        "try {\n"
        "  var raw = source.getLoginHeader();\n"
        "  var st = typeof raw === \"string\" ? JSON.parse(raw || \"{}\") : (raw || {});\n"
        "  var a = st && (st.Authorization || st.authorization);\n"
        "  if (a && String(a).trim()) h.Authorization = String(a);\n"
        "} catch (e) {}\n"
        "JSON.stringify(h);"
    )


def _search_url_rule(base_api: str, *, media: str = "") -> str:
    """Search entry: resolve token (bound code / stored header) then hit API."""
    base = json.dumps(str(base_api or "").rstrip("/"), ensure_ascii=False)
    # key/page are injected by AnalyzeUrl for searchUrl @js.
    return (
        "@js:\n"
        "try { if (typeof legadoHubResolveAuth === \"function\") legadoHubResolveAuth(); } catch (e0) {}\n"
        "var _base = " + base + ";\n"
        "var _key = \"\";\n"
        "var _page = \"1\";\n"
        "try { _key = String(key != null ? key : \"\"); } catch (e1) { _key = \"\"; }\n"
        "try { _page = String(page != null ? page : \"1\"); } catch (e2) { _page = \"1\"; }\n"
        "_base + \"/api/subscribe/legado/search?keyword=\" + encodeURIComponent(_key) + \"&page=\" + encodeURIComponent(_page)"
        + ";"
    )


def _explore_url_rule(base_api: str, *, media: str = "") -> str:
    """Explore entry with pre-request token resolve (same as search)."""
    base = str(base_api or "").rstrip("/")
    # Keep group title prefix; URL body is @js so token is attached before GET.
    return (
        "已发布书库::@js:\n"
        "try { if (typeof legadoHubResolveAuth === \"function\") legadoHubResolveAuth(); } catch (e0) {}\n"
        "var _base = "
        + json.dumps(base, ensure_ascii=False)
        + ";\n"
        "var _page = \"1\";\n"
        "try { _page = String(page != null ? page : \"1\"); } catch (e1) { _page = \"1\"; }\n"
        "_base + \"/api/subscribe/legado/explore?page=\" + encodeURIComponent(_page)"
        + ";"
    )


def _login_script(base_api: str, *, access_code: str | None = None) -> str:
    base_literal = json.dumps(base_api.rstrip("/"), ensure_ascii=False)
    access_literal = json.dumps(str(access_code or ""), ensure_ascii=False)
    return f"""var LEGADOHUB_BASE = {base_literal};
var LEGADOHUB_ACCESS_CODE = {access_literal};

function legadoHubBoundAccessCode() {{
    try {{
        return String(LEGADOHUB_ACCESS_CODE || "").trim();
    }} catch (e) {{
        return "";
    }}
}}

function legadoHubLoginInfoValue(name) {{
    function readValue(info) {{
        if (!info) return null;
        try {{
            if (typeof info === "string") info = JSON.parse(info);
        }} catch (e) {{}}
        try {{
            var mapped = info.get(name);
            if (mapped !== null && mapped !== undefined) return String(mapped);
        }} catch (e) {{}}
        try {{
            var direct = info[name];
            if (direct !== null && direct !== undefined) return String(direct);
        }} catch (e) {{}}
        try {{
            if (info.containsKey(name)) return String(info.get(name) || "");
        }} catch (e) {{}}
        try {{
            if (info.has(name)) return String(info.get(name) || "");
        }} catch (e) {{}}
        try {{
            if (Object.prototype.hasOwnProperty.call(info, name)) return String(info[name] || "");
        }} catch (e) {{}}
        return null;
    }}

    var current = null;
    try {{
        if (typeof result !== "undefined") current = readValue(result);
    }} catch (e) {{}}
    if (current !== null) return current;

    var stored = null;
    try {{ stored = readValue(source.getLoginInfoMap()); }} catch (e) {{}}
    return stored === null ? "" : stored;
}}

function legadoHubHeaders() {{
    var headers = {{"Accept": "application/json", "Content-Type": "application/json"}};
    try {{
        var raw = source.getLoginHeader();
        var stored = typeof raw === "string" ? JSON.parse(raw || "{{}}") : raw;
        var authorization = stored && (stored.Authorization || stored.authorization);
        if (authorization) headers.Authorization = String(authorization);
    }} catch (e) {{}}
    return headers;
}}

function legadoHubRequest(path, method, body) {{
    var options = {{
        method: String(method || "GET").toUpperCase(),
        headers: legadoHubHeaders()
    }};
    if (body !== undefined && body !== null) options.body = JSON.stringify(body);
    var text = String(java.ajax(LEGADOHUB_BASE + path + "," + JSON.stringify(options)) || "").trim();
    return text ? JSON.parse(text) : {{}};
}}

function legadoHubUsername(payload) {{
    var username = payload && payload.user && payload.user.username;
    return typeof username === "string" ? username.trim() : "";
}}

function legadoHubHasLoginHeader() {{
    try {{
        var raw = source.getLoginHeader();
        var stored = typeof raw === "string" ? JSON.parse(raw || "{{}}") : raw;
        var authorization = stored && (stored.Authorization || stored.authorization);
        return !!(authorization && String(authorization).trim());
    }} catch (e) {{
        return false;
    }}
}}

function legadoHubRedeemAccessCode(code, showMessage) {{
    var payload = legadoHubRequest("/api/auth/access/redeem", "POST", {{accessCode: String(code || "")}});
    var username = legadoHubUsername(payload);
    if (!username || !payload.token) throw new Error("invalid identity");
    source.putLoginHeader(JSON.stringify({{Authorization: "Bearer " + String(payload.token)}}));
    source.putLoginInfo("{{}}");
    if (showMessage) java.toast("登录成功：" + username);
    return true;
}}

function legadoHubEnsureAuth(showMessage) {{
    if (legadoHubHasLoginHeader()) return true;
    var code = legadoHubLoginInfoValue("授权码").trim();
    if (!code) code = legadoHubBoundAccessCode();
    if (!code) {{
        if (showMessage) java.toast("请输入授权码，或使用管理员发放的专属订阅链接导入书源");
        return false;
    }}
    try {{
        return legadoHubRedeemAccessCode(code, !!showMessage);
    }} catch (e) {{
        if (showMessage) java.toast("登录失败，请检查授权码是否已重置或服务是否可用");
        return false;
    }}
}}

function legadoHubLogin() {{
    var code = legadoHubLoginInfoValue("授权码").trim();
    if (!code) code = legadoHubBoundAccessCode();
    if (!code) {{
        java.toast("请输入授权码");
        return false;
    }}
    try {{
        return legadoHubRedeemAccessCode(code, true);
    }} catch (e) {{
        java.toast("登录失败，请检查授权码或服务状态");
        return false;
    }}
}}

function login() {{
    return legadoHubLogin();
}}

function legadoHubStatus(showMessage) {{
    try {{
        if (!legadoHubHasLoginHeader()) {{
            if (legadoHubBoundAccessCode()) {{
                if (!legadoHubEnsureAuth(false)) {{
                    if (showMessage) java.toast("未登录或授权已失效");
                    return false;
                }}
            }} else {{
                if (showMessage) java.toast("未登录或授权已失效");
                return false;
            }}
        }}
        var payload = legadoHubRequest("/api/auth/access/me", "GET", null);
        var username = legadoHubUsername(payload);
        if (username) {{
            if (showMessage) java.toast("已登录：" + username);
            return true;
        }}
        source.removeLoginHeader();
        if (legadoHubBoundAccessCode() && legadoHubEnsureAuth(false)) {{
            payload = legadoHubRequest("/api/auth/access/me", "GET", null);
            username = legadoHubUsername(payload);
            if (username) {{
                if (showMessage) java.toast("已登录：" + username);
                return true;
            }}
        }}
        if (showMessage) java.toast("未登录或授权已失效");
        return false;
    }} catch (e) {{
        if (showMessage) java.toast("暂时无法检查登录状态");
        return false;
    }}
}}

function legadoHubOpenConsolePath(path, title) {{
    var code = legadoHubLoginInfoValue("授权码").trim();
    if (!code) code = legadoHubBoundAccessCode();
    var target = String(path || "/console/subscription");
    if (target.charAt(0) !== "/") target = "/" + target;
    var next = encodeURIComponent(target);
    var url = LEGADOHUB_BASE + "/api/auth/access/enter?next=" + next;
    if (code) url += "&code=" + encodeURIComponent(code);
    java.startBrowser(url, String(title || "LegadoHub"));
}}

function legadoHubOpenSubscriptions() {{
    legadoHubOpenConsolePath("/console/subscription", "订阅");
}}

function legadoHubOpenLibrary() {{
    legadoHubOpenConsolePath("/console/library", "书库");
}}

function legadoHubLogout() {{
    try {{ legadoHubRequest("/api/auth/access/logout", "POST", null); }} catch (e) {{}}
    try {{ source.removeLoginHeader(); }} catch (e) {{}}
    try {{ source.putLoginInfo("{{}}"); }} catch (e) {{}}
    try {{ cookie.removeCookie(LEGADOHUB_BASE); }} catch (e) {{}}
    java.toast("已退出登录");
    return true;
}}
"""


def _login_check_script() -> str:
    # On 401: clear stale Bearer. Bound code is re-attached on the next header
    # redeem; unbound sources stay logged-out so Reading opens the login UI.
    return """var legadoHubOriginalResponse = result;
try {
    eval(String(source.loginUrl));
    var body = String(legadoHubOriginalResponse == null ? "" : legadoHubOriginalResponse);
    var needLogin = /当前未登陆|未登陆|请登陆后使用|Unauthorized/i.test(body);
    if (needLogin) {
        try { source.removeLoginHeader(); } catch (e0) {}
        if (legadoHubBoundAccessCode()) {
            try { legadoHubEnsureAuth(false); } catch (e1) {}
        }
    } else if (!legadoHubHasLoginHeader() && legadoHubBoundAccessCode()) {
        try { legadoHubEnsureAuth(false); } catch (e2) {}
    }
} catch (e) {}
legadoHubOriginalResponse;"""


_LEGADO_E_READER_JS = r"""
function legadoHubReviewRoot(contentUrl) {
    return String(contentUrl || "").split("?")[0].replace(/\/+$/, "");
}

// Rewrite absolute Hub API URLs to the origin baked into this book source.
// Chapter/toc snapshots may still carry a LAN host from an earlier request;
// comments and content should follow the source entry (CF/public or LAN).
// Do NOT call baseUrl() here: AnalyzeRule often binds baseUrl to the chapter
// data: URL and that string shadows the jsLib helper.
function legadoHubRewriteApiUrl(absoluteUrl) {
    var value = String(absoluteUrl || "").trim();
    if (!/^https?:\/\//i.test(value)) return value;
    var configured = "";
    try {
        configured = String(legadoHubSourceBase() || "").trim().replace(/\/+$/, "");
    } catch (e) {
        configured = "";
    }
    if (!/^https?:\/\//i.test(configured)) return value.replace(/\/+$/, "");
    var pathWithQuery = value.replace(/^https?:\/\/[^\/?#]+/i, "");
    if (!pathWithQuery) pathWithQuery = "/";
    return configured + pathWithQuery;
}

function legadoHubReviewCount(item) {
    if (!item) return 0;
    var count = Number(item.commentCount || item.totalCommentCount || item.hotCommentCount || 0);
    return isFinite(count) && count > 0 ? Math.floor(count) : 0;
}

function legadoHubChapterEndReviewCount(reviews) {
    var summary = reviews && reviews.summary && typeof reviews.summary === "object" ? reviews.summary : {};
    var total = Number(summary.chapterEndCount || 0);
    if (isFinite(total) && total > 0) return Math.floor(total);
    return Math.max((reviews.chapterEnd || []).length, (reviews.chapterEndHot || []).length);
}

"""


def _reader_js_lib(base_api: str, *, access_code: str | None = None) -> str:
    base_literal = json.dumps(base_api.rstrip("/"), ensure_ascii=False)
    version_literal = json.dumps(_READER_RULE_VERSION, ensure_ascii=False)
    access_literal = json.dumps(str(access_code or ""), ensure_ascii=False)
    # baseUrl() is the historical helper; legadoHubSourceBase() is collision-safe
    # when AnalyzeRule binds the name baseUrl to a chapter data: URL.
    # LEGADOHUB_RULE_VERSION must change whenever rules change so Reading's
    # imported source body is not "same content, only title renamed".
    # LEGADOHUB_ACCESS_CODE is set only for personalized subscription links.
    # Auth helpers must live in jsLib so ruleContent java.ajax also carries Bearer.
    return (
        "var LEGADOHUB_RULE_VERSION = "
        + version_literal
        + ";\n"
        + "var LEGADOHUB_ACCESS_CODE = "
        + access_literal
        + ";\n"
        + "function legadoHubRuleVersion() { return LEGADOHUB_RULE_VERSION; }\n"
        + "function baseUrl() { return "
        + base_literal
        + "; }\n"
        + "function legadoHubSourceBase() { return "
        + base_literal
        + "; }\n"
        + _auth_runtime_js(base_api, access_code=access_code)
        + _LEGADO_E_READER_JS
    )


def _source_identity_for_base(base_api: str) -> tuple[str, str, str, bool]:
    """Return (bookSourceUrl, display name stem, group, is_lan) for this base."""
    config = load_aggregate_config()
    name = str(config.get("name") or "LegadoHub 聚合").strip() or "LegadoHub 聚合"
    group = str(config.get("group") or "聚合,LegadoHub").strip() or "聚合,LegadoHub"
    lan = is_lan_reading_base(base_api)
    if not lan:
        return _PUBLIC_BOOK_SOURCE_URL, name, group, False
    display = name if _LAN_NAME_MARK in name else f"{name}{_LAN_NAME_MARK}"
    parts = [part.strip() for part in group.split(",") if part.strip()]
    if _LAN_GROUP_MARK not in parts:
        parts.append(_LAN_GROUP_MARK)
    return _LAN_BOOK_SOURCE_URL, display, ",".join(parts), True


def _content_rule() -> str:
    """Chapter content rule for the unified source.

    Fetches the chapter payload through legadoHubAjax (same Bearer as
    search/toc) and branches on the payload format: audio/video chapters
    return the signed media URL (the app's native players handle playback —
    book.type is stamped per-book in ruleBookInfo), text chapters render as
    paragraphs.
    """
    return (
        '@js:\n'
        'var payload = String(result || "");\n'
        'var contentUrl = "";\n'
        'try {\n'
        '  contentUrl = String(java.hexDecodeToString(payload) || "").trim();\n'
        '  try { contentUrl = legadoHubRewriteApiUrl(contentUrl); } catch (e0) {}\n'
        '  contentUrl += (contentUrl.indexOf("?") >= 0 ? "&" : "?") + "reviewBubbles=1";\n'
        '  if (/^https?:\\/\\//i.test(contentUrl)) {\n'
        '    try {\n'
        '      payload = String(legadoHubAjax(contentUrl) || "");\n'
        '    } catch (eAjax) {\n'
        '      payload = String(java.ajax(contentUrl) || "");\n'
        '    }\n'
        '  }\n'
        '} catch (e) {}\n'
        'var text = payload;\n'
        'var chapterPayload = null;\n'
        'try {\n'
        '  chapterPayload = JSON.parse(payload);\n'
        '  if (typeof chapterPayload.content === "string") text = chapterPayload.content;\n'
        '  else if (typeof chapterPayload.detail === "string") text = chapterPayload.detail;\n'
        '  else if (chapterPayload.detail && chapterPayload.detail.message) text = chapterPayload.detail.message;\n'
        '} catch (e) {}\n'
        'var fmt = chapterPayload ? String(chapterPayload.format || "text") : "text";\n'
        'var media = chapterPayload ? String(chapterPayload.mediaUrl || "") : "";\n'
        'if ((fmt === "audio" || fmt === "video") && media) {\n'
        '  result = media;\n'
        '} else {\n'
        '  text = String(text || "").replace(/\\r\\n/g, "\\n").replace(/\\r/g, "\\n");\n'
        '  result = /<(?:p|div)\\b/i.test(text) ? text : text.replace(/\\n\\n+/g, "<br><br>").replace(/\\n/g, "<br>");\n'
        '}'
    )


def _book_info_init_rule() -> str:
    """Unified-source book info init: stamp per-book type flags from contentType.

    阅读C/newer Reading map contentType to BookType flags so a single source
    can carry text (8), audio (32) and video (4) books; the field rules below
    then parse the same data object.
    """
    return (
        "@js:\n"
        'var raw = String(result || "");\n'
        "var data = {};\n"
        "try { data = JSON.parse(raw).data || {}; } catch (e0) {}\n"
        'var kind = String(data.contentType || "text");\n'
        'if (kind === "video") { try { book.type = 4; } catch (e1) {} }\n'
        'else if (kind === "audio") { try { book.type = 32; } catch (e2) {} }\n'
        "JSON.stringify(data);"
    )


def _build_source(
    base_api: str | None = None,
    *,
    access_code: str | None = None,
) -> dict:
    base_api = normalize_public_base_url(base_api or get_public_base_url())
    app_config = AppConfig.get()
    bound = bool(str(access_code or "").strip())

    book_source_url, name, group, is_lan = _source_identity_for_base(base_api)
    book_source_type = 0
    explore_url = _explore_url_rule(base_api)
    search_url = _search_url_rule(base_api)

    network_note = (
        "本条为内网书源（bookSourceUrl=LegadoHub-LAN），可与公网书源并存；"
        if is_lan
        else "本条为公网书源（bookSourceUrl=LegadoHub），可与内网书源并存；"
    )
    bind_note = (
        "专属书源：搜索/目录/正文自动鉴权；登录页提供「订阅 / 书库」入口。"
        if bound
        else "请使用管理员发放的专属书源链接导入。"
    )
    media_note = (
        "统一源：文字/有声/视频书一并收录，书籍类型按内容自动标记，"
        "有声与视频章节的正文为媒体直链，由阅读 App 内置播放器播放；"
        "正文内嵌可点击段评气泡与章末评论卡片（阅读C/Max 客户端），"
        "不再携带 legado-X 的 chapterComment 协议；"
    )
    return {
        "bookSourceName": f"{name}({_READER_RULE_VERSION})",
        "bookSourceGroup": group,
        "bookSourceUrl": book_source_url,
        "lastUpdateTime": _reader_rule_last_update_time(app_config),
        "bookSourceType": book_source_type,
        "enabled": True,
        "enabledCookieJar": True,
        "enabledExplore": True,
        # Header must stay free of java.ajax — login sheet evaluates it on open.
        "header": _request_header_rule(),
        "loginUi": _login_ui(bound_access_code=bound),
        "loginUrl": _login_script(base_api, access_code=access_code),
        "loginCheckJs": _login_check_script(),
        "bookSourceComment": (
            f"规则版本 {_READER_RULE_VERSION}。"
            f"{media_note}"
            f"{network_note}"
            f"{bind_note}"
            "搜索同时显示已发布共享书和启用的第三方书源；官方源仍只用于后台聚合，"
            "新增订阅及运维操作统一在 Web Console 完成。"
        ),
        # Progressive: page1 library + short third-party batch; page2+ continue
        # the same server job for new remotes (see subscribe._legado_search_response).
        "searchUrl": search_url,
        # Slightly above page2 short-wait (20s) so follow-up search pages can finish.
        "respondTime": 25000,
        "exploreUrl": explore_url,
        "ruleSearch": {
            "bookList": "$.items",
            "name": "$.name",
            "author": "$.author",
            "coverUrl": "$.coverUrl",
            "intro": "$.intro",
            "kind": "$.kind",
            "lastChapter": "$.readingLastChapter",
            "wordCount": "$.wordCount",
            "bookUrl": "$.bookUrl",
            "checkKeyWord": "",
        },
        "ruleExplore": {
            "bookList": "$.items",
            "name": "$.name",
            "author": "$.author",
            "coverUrl": "$.coverUrl",
            "intro": "$.intro",
            "kind": "$.kind",
            "lastChapter": "$.lastChapter",
            "wordCount": "$.wordCount",
            "bookUrl": "$.bookUrl",
        },
        "ruleBookInfo": {
            "init": _book_info_init_rule(),
            "name": "$.name",
            "author": "$.author",
            "coverUrl": "$.coverUrl",
            "intro": "$.intro",
            "kind": "$.kind",
            "lastChapter": "$.lastChapter",
            "wordCount": "$.wordCount",
            "updateTime": "$.updateTime",
            "tocUrl": "$.tocUrl",
            "canReName": "1",
        },
        "ruleToc": {
            "chapterList": "$.chapters",
            "chapterName": "$.title",
            "chapterUrl": (
                "<js>\n"
                "var contentUrl = String(result.chapterUrl || '');\n"
                "try { contentUrl = legadoHubRewriteApiUrl(contentUrl); } catch (e) {}\n"
                "var metadata = {type: 'legadoHub'};\n"
                "`data:contentUrl;base64,${java.base64Encode(contentUrl)},${JSON.stringify(metadata)}`;\n"
                "</js>"
            ),
            "isVip": "$.isVip",
            "isPay": "$.isPay",
            "updateTime": "$.updateTime",
        },
        "ruleContent": {
            # Must use legadoHubAjax (jsLib) so chapter fetch carries the same
            # Bearer as search/toc. Plain java.ajax(contentUrl) skipped source header.
            # 段评/章评通过正文内嵌气泡 + 章末卡片（legado_max_bubbles）投递，
            # 不再携带 legado-X 专用的 chapterComment 协议。
            "content": _content_rule(),
            "title": "$.title",
        },
        "jsLib": _reader_js_lib(base_api, access_code=access_code),
    }


def generate_legado_source(
    base_api: str | None = None,
    *,
    access_code: str | None = None,
) -> list[dict]:
    """One unified source for text/audio/video books.

    ruleBookInfo stamps per-book type flags from contentType (audio 32 /
    video 4) so 阅读C/newer Reading route chapters to the right player, and
    ruleContent branches on the chapter payload format.
    """
    return [_build_source(base_api, access_code=access_code)]


def write_legado_source() -> str:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = GENERATED_DIR / "legadohub-source.json"
    data = generate_legado_source()
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)
