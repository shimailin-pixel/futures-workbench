"""浏览器可达性自诊断。

问题背景：工作台调用云服务 LLM 时，流量由浏览器直接发出（云服务规范禁止把
`/.cloud/llm/*` 挂到后端做代理）。而浏览器走的是 Windows 系统代理 —— 通常由
v2rayN / Clash 之类接管，监听 127.0.0.1:10808。

若云服务域名不在系统代理的「绕过列表」里，请求会被送进代理节点。一旦节点到
该域名的链路不通（TLS 握手失败、RST 等），浏览器只会抛出 `Failed to fetch`，
用户看不到任何可操作信息——之前就踩过这个坑：走代理 10808 时
`finresearch-90334.app.workbuddy.host` 握手失败，而直连返回 200。

本模块在服务端复刻浏览器的代理决策（读注册表 → 匹配绕过列表 → 决定直连还是
走代理），再分别实测两条链路，输出可以直接照做的修复指令。
"""

from __future__ import annotations

import re
import socket
import ssl
import time
import urllib.error
import urllib.request

PROBE_PATH = "/.cloud/llm/models"
DEFAULT_ENDPOINT = "https://finresearch-90334.app.workbuddy.host"
DEFAULT_TIMEOUT = 12


# ---------------------------------------------------------------- 注册表读取

def _read_system_proxy() -> tuple[bool, str, list[str]]:
    """读 Windows 系统代理设置。

    返回 (是否启用, 代理服务器, 绕过列表)。非 Windows 或读不到时返回关闭状态。
    """
    try:
        import winreg
    except ImportError:
        return False, "", []
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enable = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            override = str(winreg.QueryValueEx(key, "ProxyOverride")[0] or "")
    except OSError:
        return False, "", []
    items = [x.strip() for x in override.split(";") if x.strip()]
    return bool(enable), server, items


def _host_bypassed(host: str, overrides: list[str]) -> bool:
    """复刻 WinINET 绕过列表的匹配规则。

    - `<local>`：不含点的主机名（本机）
    - `*.foo.com`：`foo.com` 及其子域
    - `127.*` 一类：IP 前缀通配
    - 其余：精确主机名
    """
    host = (host or "").lower()
    for raw in overrides:
        item = raw.lower().strip()
        if not item:
            continue
        if item == "<local>":
            if host and "." not in host:
                return True
            continue
        if item.startswith("*."):
            suffix = item[1:]            # ".foo.com"
            if host.endswith(suffix) or host == suffix[1:]:
                return True
            continue
        if "*" in item:
            pattern = "^" + re.escape(item).replace(r"\*", ".*") + "$"
            if re.match(pattern, host):
                return True
            continue
        if host == item:
            return True
    return False


# ---------------------------------------------------------------- 连通性实测

def _probe(url: str, proxy: str | None, timeout: int) -> tuple[bool, str]:
    """实测一条链路。返回 (是否通, 说明)。"""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))   # 强制直连，忽略系统代理
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, headers={"User-Agent": "finresearcher-netcheck/1.0"})
    t0 = time.time()
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read(256)
            return True, f"HTTP {resp.status}（{(time.time()-t0)*1000:.0f} ms）"
    except urllib.error.HTTPError as exc:
        # 服务端有响应说明链路是通的，只是鉴权/参数问题
        return True, f"HTTP {exc.code}（链路可达，但请求被拒：{exc.reason}）"
    except ssl.SSLError as exc:
        return False, f"TLS 握手失败：{exc.reason or exc}"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"连接失败：{reason}"
    except socket.timeout:
        return False, "连接超时"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


# ---------------------------------------------------------------- 对外入口

def check_cloud_endpoint(endpoint: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    """诊断浏览器访问云服务 LLM 的可达性。

    返回结构里 `browser_route` 描述浏览器当前会走哪条路，`fix` 给出可照做的指令。
    """
    host = ""
    m = re.search(r"https?://([^/:]+)", endpoint or "")
    if m:
        host = m.group(1)
    url = (endpoint or "").rstrip("/") + PROBE_PATH

    enabled, server, overrides = _read_system_proxy()
    bypassed = _host_bypassed(host, overrides)
    proxy_host = server.split(";")[0].strip()
    if proxy_host and ":" in proxy_host and not proxy_host.startswith("http"):
        proxy_host = "http://" + proxy_host

    direct_ok, direct_msg = _probe(url, None, timeout)
    proxy_ok, proxy_msg = (True, "未启用系统代理，无需测试") if not enabled or bypassed \
        else _probe(url, proxy_host, timeout)

    # 浏览器当前实际会走哪条路
    if not enabled:
        browser_route = "直连（系统代理未启用）"
    elif bypassed:
        browser_route = f"直连（{host} 命中绕过列表）"
    else:
        browser_route = f"经系统代理 {proxy_host}"

    browser_ok = direct_ok if (not enabled or bypassed) else proxy_ok

    fix = ""
    if not browser_ok:
        if direct_ok:
            fix = (
                f"直连可用、经代理不可用——代理软件对 {host} 的分流规则有问题。\n"
                f"解法（任选其一）：\n"
                f"  1) 代理软件里加直连规则：DOMAIN-SUFFIX,workbuddy.host,DIRECT\n"
                f"  2) 系统代理「绕过列表」加入：*.workbuddy.host\n"
                f"     v2rayN：参数设置 → 系统代理设置 → 多个代理绕过地址\n"
                f"     注册表：HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\"
                f"Internet Settings → ProxyOverride"
            )
        else:
            fix = (
                f"直连与代理都不通——请先确认网络与域名解析：\n"
                f"  1) 浏览器访问 {endpoint} 看是否能打开\n"
                f"  2) 确认域名未过期（本工作台的云服务地址随应用绑定变化）"
            )
    elif not direct_ok and proxy_ok:
        fix = (
            f"当前经代理可用、直连不可用——保持现状即可。\n"
            f"若代理节点将来变动导致失败，可把 {host} 加入绕过列表走直连。"
        )

    return {
        "host": host,
        "endpoint": endpoint,
        "system_proxy_enabled": enabled,
        "system_proxy_server": server,
        "bypassed": bypassed,
        "bypass_list": overrides,
        "browser_route": browser_route,
        "browser_ok": browser_ok,
        "direct_ok": direct_ok,
        "direct_msg": direct_msg,
        "proxy_ok": proxy_ok,
        "proxy_msg": proxy_msg,
        "fix": fix,
    }
