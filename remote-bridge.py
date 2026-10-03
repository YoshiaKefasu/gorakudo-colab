#!/usr/bin/env python3
"""remote-bridge.py -- ローカルComfyUIリバースプロキシ (実行はColab, 保存はローカル).

ローカルのブラウザ (http://127.0.0.1:8188) でGUIを触り、実行だけ
Colab上のComfyUI (推奨=Tailscaleの http://100.x.y.z:8188、フォールバック=cloudflared/ngrok/pinggy) に流し、
生成画像はローカルの --save-dir に自動保存する。小さな透過プロキシ。

使い方 (推奨・Tailscale):
    python tools/remote-bridge.py --remote http://100.x.y.z:8188
    python tools/remote-bridge.py --remote https://xxxx.trycloudflare.com --url-file tunnel_url.txt
    python tools/remote-bridge.py --remote https://xxxx.ngrok-free.app  (ngrokも可)
    python tools/remote-bridge.py --remote https://xxxx.run.pinggy-free.link  (pinggyも可)

Note: Tailscale IP (http://100.x.y.z:8188) はそのまま渡すだけ。https強制なし・ngrokヘッダなし。

既定でハイブリッド: UI・設定・ワークフローはローカル配信、実行系だけリモート。
完全プロキシに戻すには --no-local-ui。

ngrok無料枠の警告ページ回避と、静的アセットのローカルキャッシュ入り。

依存: 標準ライブラリ + aiohttp のみ (ComfyUIのvenvに入っている)。
"""
from __future__ import annotations

import argparse
import asyncio
import glob
import hashlib
import json
import mimetypes
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib import parse

import aiohttp
from aiohttp import ClientSession, ClientTimeout, web

# 2秒ごとに /history を見に行く / --url-file は5秒ごとに読み直す
POLL_INTERVAL = 2.0
URLFILE_INTERVAL = 5.0
CHUNK = 64 * 1024

# hop-by-hopヘッダは転送しない (HostはaiohttpがURLから付け直す)。
HOP_REQ = {"host", "connection", "keep-alive", "proxy-authenticate",
           "proxy-authorization", "te", "trailer", "transfer-encoding",
           "upgrade", "content-length"}
HOP_RES = {"connection", "keep-alive", "proxy-authenticate",
           "proxy-authorization", "te", "trailer", "transfer-encoding",
           "upgrade"}


# ---------- ngrok判定 ----------

# ngrok無料枠はブラウザ警告ページを挟む。対象ホストへの上流リクエストには
# ngrok-skip-browser-warning を付けて警告HTMLを回避する（自動判定）。
NGROK_SUFFIXES = (".ngrok-free.app", ".ngrok-free.dev", ".ngrok.io", ".ngrok.app", ".ngrok.dev")


def _ngrok_headers(remote: str) -> dict:
    host = remote.split("://", 1)[1].split("/", 1)[0].lower() if "://" in remote else ""
    if host.endswith(NGROK_SUFFIXES):
        return {"ngrok-skip-browser-warning": "true"}
    return {}


# ---------- Tailscale判定 ----------

# Tailscale (100.64.0.0/10 のCGNAT帯 / *.ts.net) はP2P直結の素のHTTP。
# https強制なし・ngrokヘッダなし（_ngrok_headersはサフィックス不一致で{}を返す）。
def _remote_host(remote: str) -> str:
    return remote.split("://", 1)[1].split("/", 1)[0].lower() if "://" in remote else ""


def _is_tailscale(remote: str) -> bool:
    host = _remote_host(remote).split(":")[0]
    if host.endswith(".ts.net"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and parts[0] == "100":
        try:
            second = int(parts[1])
            nums = [int(p) for p in parts]
        except ValueError:
            return False
        return 64 <= second <= 127 and all(0 <= n <= 255 for n in nums)
    return False


# ---------- Pinggy判定 ----------

# Pinggy無料枠はブラウザ向けscreeningページを挟むことがある。対象ホストへの
# 上流リクエストには X-Pinggy-No-Screen を付けて回避する（ngrokと同型）。
# 根拠: https://pinggy.io/docs/http_tunnels/screening/
PINGGY_SUFFIXES = (".pinggy.link", ".pinggy-free.link")


def _is_pinggy(remote: str) -> bool:
    host = _remote_host(remote).split(":")[0]
    return host.endswith(PINGGY_SUFFIXES)


def _pinggy_headers(remote: str) -> dict:
    if _is_pinggy(remote):
        return {"X-Pinggy-No-Screen": "1"}
    return {}


def _tunnel_headers(remote: str) -> dict:
    """ngrok/Pinggyの回避ヘッダをまとめて返す（どちらでもなければ{}）。"""
    return {**_ngrok_headers(remote), **_pinggy_headers(remote)}


# ---------- ローカルキャッシュ ----------

# Note: 実測でトンネル1リクエスト約5秒。GUIは静的アセットを数百回取るので
# 初回だけ転送してディスクに置き、2回目以降はローカルから返す。
STATIC_PREFIXES = ("/assets/", "/extensions/", "/static/", "/templates/", "/scripts/")
MAX_CACHE_BYTES = 32 * 1024 * 1024  # これより大きい応答はキャッシュしない

# 短期TTLで覚える重い読み取りAPI（クエリ込みでキー化）
API_CACHEABLE = ("/object_info", "/embeddings", "/extensions", "/models")
API_CACHEABLE_PREFIX = ("/object_info/", "/embeddings/", "/extensions/", "/models/",
                        "/view_metadata/")

# 絶対にキャッシュしない（常に転送。プロンプト投下・キュー・履歴・画像・WS等）
NEVER_CACHE_PREFIX = ("/prompt", "/queue", "/interrupt", "/history", "/view",
                      "/upload", "/ws")


def _is_never_cache(path: str) -> bool:
    return path == "/ws" or path.startswith(NEVER_CACHE_PREFIX)


def _is_static_cacheable(path: str) -> bool:
    if path == "/user.css" or path.startswith("/favicon"):
        return True
    return path.startswith(STATIC_PREFIXES)


def _is_api_cacheable(path: str) -> bool:
    if path in API_CACHEABLE:
        return True
    return path.startswith(API_CACHEABLE_PREFIX)


def _cache_key(raw_path_qs: str) -> str:
    return hashlib.sha1(raw_path_qs.encode("utf-8")).hexdigest()


def _static_paths(cache_dir: Path, key: str) -> tuple:
    return cache_dir / (key + ".body"), cache_dir / (key + ".meta.json")


def _static_lookup(cache_dir: Path, key: str):
    """ヒット時は (meta, body_bytes) を返す。なければNone。"""
    body_p, meta_p = _static_paths(cache_dir, key)
    try:
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        return meta, body_p.read_bytes()
    except OSError:
        return None


def _static_save(cache_dir: Path, key: str, status: int, headers: list, body: bytes) -> None:
    etag = None
    last_mod = None
    for k, v in headers:
        kl = k.lower()
        if kl == "etag":
            etag = v
        elif kl == "last-modified":
            last_mod = v
    body_p, meta_p = _static_paths(cache_dir, key)
    try:
        body_p.write_bytes(body)
        meta_p.write_text(json.dumps({"status": status, "headers": headers,
                                      "etag": etag, "last_modified": last_mod}),
                          encoding="utf-8")
    except OSError as e:
        log(f"[cache] 保存失敗: {type(e).__name__}: {e}")


def _static_response(request: web.Request, meta: dict, body: bytes) -> web.Response | None:
    """条件付きリクエストなら304、そうでなければ200キャッシュ応答。"""
    etag = meta.get("etag")
    last_mod = meta.get("last_modified")
    inm = request.headers.get("If-None-Match")
    if etag and inm and (inm.strip() == "*" or etag in [t.strip() for t in inm.split(",")]):
        return web.Response(status=304, headers={"ETag": etag, "X-Bridge-Cache": "hit"})
    ims = request.headers.get("If-Modified-Since")
    if last_mod and ims and ims.strip() == last_mod:
        return web.Response(status=304, headers={"Last-Modified": last_mod,
                                                 "X-Bridge-Cache": "hit"})
    hdrs = {k: v for k, v in meta["headers"]}
    hdrs["X-Bridge-Cache"] = "hit"
    return web.Response(status=meta["status"], headers=hdrs, body=body)


def _api_lookup(state: dict, key: str):
    entry = state["api_cache"].get(key)
    if entry and entry[0] >= time.monotonic():
        return entry
    if entry:
        del state["api_cache"][key]  # 期限切れは捨てる
    return None


def _api_save(state: dict, key: str, status: int, headers: list, body: bytes) -> None:
    ttl = state["api_ttl"]
    if ttl <= 0:
        return
    cache = state["api_cache"]
    now = time.monotonic()
    for k in [k for k, v in cache.items() if v[0] < now]:
        del cache[k]  # 期限切れ掃除
    if len(cache) > 128:
        cache.pop(next(iter(cache)))  # 最古を1件捨てる（順序保持dict）
    cache[key] = (now + ttl, status, headers, body)


def _api_response(entry) -> web.Response:
    _, status, headers, body = entry
    hdrs = {k: v for k, v in headers}
    hdrs["X-Bridge-Cache"] = "hit"
    return web.Response(status=status, headers=hdrs, body=body)


def _cacheable_response(status: int, headers: list, body: bytes) -> bool:
    """200・サイズ妥当なら保存してよい。"""
    if status != 200:
        return False
    if len(body) > MAX_CACHE_BYTES:
        return False
    for k, v in headers:
        if k.lower() == "content-length":
            try:
                if int(v) != len(body):
                    return False
            except ValueError:
                return False
    return True


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------- ローカルUI (ハイブリッドモード) ----------
#
# UI・設定・ワークフローはローカルPCから直接配信し、GPU計算・実行系だけを
# リモート(Colab)に投げる。既定で有効 (--no-local-ui で完全プロキシに戻す)。
#
# ローカル配信: / (index.html), 静的アセット, /settings(+/api/*), /userdata(+/api/*)
# リモート転送: /prompt, /queue, /history, /interrupt, /view, /upload,
#               /ws, /object_info, /models/*, /system_stats 等 (既存プロキシ経由)
#
# Note: 本家 server.py / app/user_manager.py / app/app_settings.py と同じ
# 振る舞いを模す (クエリ名・ステータスコード・保存形式)。本家とずれたらこちらを直す。

SYSTEM_USER_PREFIX = "__"

# text/html等がそのまま返ると保存済みマークアップがapp originで実行される
# (stored XSS)。本家は folder_paths.is_dangerous_content_type で弾く。
_DANGEROUS_CT = {"text/html", "application/xhtml+xml", "application/javascript",
                 "text/javascript", "image/svg+xml"}


def detect_comfy_root() -> Path | None:
    """tools/remote-bridge.py の親=ComfyUIルート。main.py等がなければNone。"""
    root = Path(__file__).resolve().parent.parent
    if (root / "main.py").is_file() and (root / "folder_paths.py").is_file():
        return root
    return None


def detect_web_root(comfy_root: Path | None) -> Path | None:
    """フロントエンド静的dir。index.htmlがあるものだけ有効。"""
    cands: list[Path] = []
    try:
        import comfyui_frontend_package as _fp  # venvに入っている想定
        cands.append(Path(_fp.__file__).resolve().parent / "static")
    except Exception:
        pass
    if comfy_root is not None:
        cands.append(comfy_root / "web")  # 旧版フォールバック
    for sp in (Path(sys.prefix) / "Lib" / "site-packages",
               Path(sys.prefix) / "lib" / "site-packages"):
        cands.append(sp / "comfyui_frontend_package" / "static")
    for c in cands:
        try:
            if (c / "index.html").is_file():
                return c
        except OSError:
            pass
    return None


def _local_user_id(request: web.Request) -> str:
    return request.headers.get("comfy-user", "default") or "default"


def _local_user_root(state: dict, request: web.Request) -> str | None:
    """user_dir/<user> の絶対パス。systemユーザは拒否。なければNone。"""
    if _local_user_id(request).startswith(SYSTEM_USER_PREFIX):
        return None
    try:
        root = os.path.abspath(str(state["user_dir"]))
        target = os.path.abspath(os.path.join(root, _local_user_id(request)))
        if os.path.commonpath((root, target)) != root:
            return None
        return target
    except (ValueError, OSError):
        return None


def _resolve_user_path(state: dict, request: web.Request, file: str | None,
                       create_dir: bool = True) -> str | None:
    """user配下の絶対パスに解決。traversalはNoneで拒否 (本家と同等)。"""
    user_root = _local_user_root(state, request)
    if user_root is None:
        return None
    if file is not None and "%" in file:
        file = parse.unquote(file)
    path = os.path.abspath(os.path.join(user_root, file)) if file else user_root
    try:
        if os.path.commonpath((user_root, path)) != user_root:
            return None
    except (ValueError, OSError):
        return None
    if create_dir:
        try:
            os.makedirs(os.path.split(path)[0], exist_ok=True)
        except OSError:
            pass
    return path


# ----- settings (/settings, /api/settings) -----

def _local_settings_load(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _local_settings_save(path: str, settings: dict) -> None:
    os.makedirs(os.path.split(path)[0], exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(settings, indent=4))
    os.replace(tmp, path)


async def local_settings(request: web.Request, state: dict, sub: str):
    sfile = _resolve_user_path(state, request, "comfy.settings.json",
                               create_dir=False)
    if sfile is None:
        return web.Response(status=403, text="Invalid user")
    if request.method == "GET":
        settings = _local_settings_load(sfile)
        if not sub:
            return web.json_response(settings)
        return web.json_response(settings.get(parse.unquote(sub)))
    # POST / PUT (本家はPOSTのみだがPUTも同義で受ける)
    try:
        new = await request.json()
    except Exception:
        return web.Response(status=400, text="Invalid JSON")
    settings = _local_settings_load(sfile)
    if not sub:
        if not isinstance(new, dict):
            return web.Response(status=400, text="Settings must be a JSON object")
        settings = {**settings, **new}
    else:
        settings[parse.unquote(sub)] = new
    try:
        _local_settings_save(sfile, settings)
    except OSError as e:
        return web.Response(status=400, text=f"save failed: {e}")
    return web.Response(status=200)


# ----- userdata (/userdata, /v2/userdata, /api/*) -----

def _local_file_info(path: str, relative_to: str) -> dict:
    return {
        "path": os.path.relpath(path, relative_to).replace(os.sep, "/"),
        "size": os.path.getsize(path),
        "modified": int(os.path.getmtime(path) * 1000),
        "created": int(os.path.getctime(path) * 1000),
    }


async def local_userdata_list(request: web.Request, state: dict):
    directory = request.rel_url.query.get("dir", "")
    if not directory:
        return web.Response(status=400, text="Directory not provided")
    path = _resolve_user_path(state, request, directory, create_dir=False)
    if not path:
        return web.Response(status=403, text="Invalid directory")
    if not os.path.exists(path):
        return web.Response(status=404, text="Directory not found")
    recurse = request.rel_url.query.get("recurse", "").lower() == "true"
    full_info = request.rel_url.query.get("full_info", "").lower() == "true"
    split_path = request.rel_url.query.get("split", "").lower() == "true"
    pattern = (os.path.join(glob.escape(path), "**", "*") if recurse
               else os.path.join(glob.escape(path), "*"))
    results: list = []
    for full in glob.glob(pattern, recursive=recurse):
        if not os.path.isfile(full):
            continue
        if full_info:
            results.append(_local_file_info(full, path))
        else:
            rel = os.path.relpath(full, path).replace(os.sep, "/")
            results.append([rel] + rel.split("/") if split_path else rel)
    return web.json_response(results)


async def local_userdata_list_v2(request: web.Request, state: dict):
    requested = request.rel_url.query.get("path", "")
    try:
        requested = parse.unquote(requested)
    except Exception:
        return web.Response(status=400, text="Invalid characters in path parameter")
    try:
        base = _resolve_user_path(state, request, None, create_dir=False)
        target = (_resolve_user_path(state, request, requested, create_dir=False)
                  if requested else base)
    except Exception:
        return web.Response(status=403, text="Invalid user specified in request")
    if not base or not target:
        return web.Response(status=400, text="Invalid path requested")
    if not os.path.exists(target):
        if target == base:
            return web.json_response([])  # 新規ユーザは空
        return web.Response(status=404, text="Requested path not found")
    if not os.path.isdir(target):
        return web.Response(status=400, text="Requested path is not a directory")
    results: list = []
    try:
        for root, dirs, files in os.walk(target, topdown=True):
            for d in dirs:
                p = os.path.join(root, d)
                results.append({"name": d, "type": "directory",
                                "path": os.path.relpath(p, base).replace(os.sep, "/")})
            for fn in files:
                p = os.path.join(root, fn)
                entry: dict = {"name": fn, "type": "file",
                               "path": os.path.relpath(p, base).replace(os.sep, "/")}
                try:
                    st = os.stat(p)
                    entry["size"] = st.st_size
                    entry["modified"] = st.st_mtime
                except OSError:
                    pass
                results.append(entry)
    except OSError as e:
        log(f"[local-ui] v2 list失敗: {e}")
        return web.Response(status=500, text="Error reading directory contents")
    results.sort(key=lambda x: (x["type"] != "directory", x["name"].lower()))
    return web.json_response(results)


def local_userdata_get(request: web.Request, state: dict, file: str):
    path = _resolve_user_path(state, request, file, create_dir=False)
    if not path:
        return web.Response(status=403)
    if not os.path.exists(path):
        return web.Response(status=404)
    if os.path.isdir(path):
        return web.Response(status=400, text="Not a file")
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    user_root = _resolve_user_path(state, request, None, create_dir=False)
    if user_root and path == os.path.abspath(os.path.join(user_root, "user.css")):
        return web.FileResponse(path, headers={"Content-Type": "text/css",
            "X-Content-Type-Options": "nosniff", "Content-Disposition": "inline"})
    if content_type in _DANGEROUS_CT:
        content_type = "application/octet-stream"
    return web.FileResponse(path, headers={"Content-Type": content_type,
        "X-Content-Type-Options": "nosniff", "Content-Disposition": "attachment"})


async def local_userdata_post(request: web.Request, state: dict, file: str):
    path = _resolve_user_path(state, request, file, create_dir=True)
    if not path:
        return web.Response(status=403)
    overwrite = request.query.get("overwrite", "true") != "false"
    full_info = request.query.get("full_info", "false").lower() == "true"
    if not overwrite and os.path.exists(path):
        return web.Response(status=409, text="File already exists")
    try:
        body = await request.read()
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError:
        return web.Response(status=400,
            reason="Invalid filename. Please avoid special characters like :\\/*?\"<>|")
    user_root = _resolve_user_path(state, request, None, create_dir=False)
    resp = (_local_file_info(path, user_root) if full_info
            else os.path.relpath(path, user_root))
    return web.json_response(resp)


def local_userdata_delete(request: web.Request, state: dict, file: str):
    path = _resolve_user_path(state, request, file, create_dir=False)
    if not path:
        return web.Response(status=403)
    if not os.path.exists(path):
        return web.Response(status=404)
    try:
        os.remove(path)
    except OSError as e:
        return web.Response(status=400, text=f"delete failed: {e}")
    return web.Response(status=204)


def local_userdata_move(request: web.Request, state: dict, file: str, dest: str):
    src = _resolve_user_path(state, request, file, create_dir=False)
    dst = _resolve_user_path(state, request, dest, create_dir=True)
    if not src or not dst:
        return web.Response(status=403)
    if not os.path.exists(src):
        return web.Response(status=404, text="Source not found")
    overwrite = request.query.get("overwrite", "true") != "false"
    full_info = request.query.get("full_info", "false").lower() == "true"
    if not overwrite and os.path.exists(dst):
        return web.Response(status=409, text="Destination already exists")
    try:
        shutil.move(src, dst)
    except OSError as e:
        return web.Response(status=400, text=f"move failed: {e}")
    user_root = _resolve_user_path(state, request, None, create_dir=False)
    resp = (_local_file_info(dst, user_root) if full_info
            else os.path.relpath(dst, user_root))
    return web.json_response(resp)


# ----- 静的アセット (web_root実ファイル・custom_nodes拡張) -----

def local_static_path(state: dict, request: web.Request) -> str | None:
    """ローカルから返せる実ファイルの絶対パス。なければNone (プロキシへ)。"""
    web_root = state.get("web_root")
    if web_root is None or request.method not in ("GET", "HEAD"):
        return None
    root = os.path.abspath(str(web_root))
    raw: str = request.rel_url.raw_path
    if raw == "/":
        cand = os.path.join(root, "index.html")
        return cand if os.path.isfile(cand) else None
    if raw.startswith("/extensions/"):
        # /extensions/<name>/... -> web_root配下 or custom_nodes/<name>/web or 直下
        rest = parse.unquote(raw[len("/extensions/"):])
        parts = rest.split("/", 1)
        if len(parts) == 2:
            name, sub = parts
            for base in (os.path.join(root, "extensions", name),
                         os.path.join(str(state.get("comfy_root") or ""),
                                      "custom_nodes", name, "web"),
                         os.path.join(str(state.get("comfy_root") or ""),
                                      "custom_nodes", name)):
                cand = os.path.abspath(os.path.join(base, sub))
                try:
                    inside = os.path.commonpath((base, cand)) == base
                except (ValueError, OSError):
                    inside = False
                if inside and os.path.isfile(cand):
                    return cand
        return None  # /extensions 一覧JSON等はリモートへ
    rel = parse.unquote(raw.lstrip("/"))
    cand = os.path.abspath(os.path.join(root, rel))
    try:
        inside = os.path.commonpath((root, cand)) == root
    except (ValueError, OSError):
        return None
    return cand if (inside and os.path.isfile(cand)) else None


async def handle_local_ui(request: web.Request, state: dict):
    """処理できたらResponse、対象外ならNone (プロキシへフォールスルー)。"""
    lpath = request.path
    raw = request.rel_url.raw_path
    if lpath.startswith("/api/"):
        lpath = lpath[4:] or "/"
    if raw.startswith("/api/"):
        raw = raw[4:] or "/"
    # --- settings ---
    if lpath == "/settings" or lpath.startswith("/settings/"):
        if request.method not in ("GET", "POST", "PUT"):
            return None
        sub = lpath[len("/settings/"):] if lpath.startswith("/settings/") else ""
        return await local_settings(request, state, sub)
    # --- userdata ---
    if lpath == "/userdata":
        if request.method == "GET":
            return await local_userdata_list(request, state)
        return None
    if lpath == "/v2/userdata":
        if request.method == "GET":
            return await local_userdata_list_v2(request, state)
        return None
    if lpath.startswith("/userdata/"):
        rest = raw[len("/userdata/"):]
        # 本家ルート /userdata/{file}/move/{dest} ({file},{dest}は単一セグメント)
        parts = rest.split("/")
        if request.method in ("POST", "PUT") and len(parts) == 3 and parts[1] == "move":
            return local_userdata_move(request, state,
                                       parse.unquote(parts[0]),
                                       parse.unquote(parts[2]))
        file = parse.unquote(rest)
        if request.method in ("GET", "HEAD"):
            return local_userdata_get(request, state, file)
        if request.method in ("POST", "PUT"):
            return await local_userdata_post(request, state, file)
        if request.method == "DELETE":
            return local_userdata_delete(request, state, file)
        return None
    # --- 静的アセット ---
    cand = local_static_path(state, request)
    if cand is not None:
        resp = web.FileResponse(cand)
        if request.path == "/":
            # 本家と同じくindex.htmlはキャッシュさせない
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            resp.headers["Pragma"] = "no-cache"
            resp.headers["Expires"] = "0"
        resp.headers["X-Bridge-Local"] = "1"
        return resp
    return None


# ---------- URL切替 ----------

def read_url_file(path: str) -> str | None:
    """URLファイルの先頭の http(s) 行を返す。なければNone。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                s = line.strip().rstrip("/")
                if s.startswith(("http://", "https://")):
                    return s
    except OSError:
        pass
    return None


def start_stdin_thread(state: dict) -> None:
    """`url <新しいURL>` を受け付ける。パイプ時は黙って無効。"""
    if not sys.stdin.isatty():
        return
    log("実行中の切替: `url <新しいURL>` + Enter (例: url https://abc-123.trycloudflare.com)")

    def _loop() -> None:
        try:
            for line in sys.stdin:
                s = line.strip()
                if not s:
                    continue
                if s.lower() == "url" or s.lower().startswith("url "):
                    new = s[3:].strip().rstrip("/")
                    if new.startswith(("http://", "https://")):
                        state["remote"] = new
                        log(f"[url] リモート切替: {new}")
                    else:
                        log(f"[url] 無効なURL (http(s):// で始めてください): {new}")
                else:
                    log("使い方: url <新しいURL>")
        except Exception as e:  # Note: stdinスレッドは落とさない
            log(f"[stdin] {type(e).__name__}: {e}")

    threading.Thread(target=_loop, daemon=True).start()


# ---------- HTTP透過プロキシ ----------

async def http_proxy(request: web.Request, session: ClientSession, state: dict) -> web.StreamResponse:
    remote = state["remote"]
    raw_qs = request.rel_url.raw_path_qs
    url = remote + raw_qs
    fwd = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_REQ]
    extra = _tunnel_headers(remote)
    if extra:
        fwd += list(extra.items())
    path = request.path
    use_static = (request.method == "GET" and not _is_never_cache(path)
                  and _is_static_cacheable(path))
    use_api = (request.method == "GET" and not _is_never_cache(path)
               and _is_api_cacheable(path))
    if use_static:
        hit = _static_lookup(state["cache_dir"], _cache_key(raw_qs))
        if hit is not None:
            meta, body = hit
            return _static_response(request, meta, body)
    elif use_api:
        hit = _api_lookup(state, _cache_key(raw_qs))
        if hit is not None:
            return _api_response(hit)
    # ボディなしGET等ではdataを付けない (付けるとchunked GETになり嫌がる鯖がある)
    data = request.content.iter_chunked(CHUNK) if request.can_read_body else None
    try:
        if use_static or use_api:
            # 保存のため一旦全部読む（対象は小物アセット・JSON想定）
            async with session.request(request.method, url, headers=fwd, data=data,
                                       allow_redirects=False, auto_decompress=False) as r:
                res_hdrs = [(k, v) for k, v in r.headers.items() if k.lower() not in HOP_RES]
                body = await r.read()
                if _cacheable_response(r.status, res_hdrs, body):
                    if use_static:
                        _static_save(state["cache_dir"], _cache_key(raw_qs),
                                     r.status, res_hdrs, body)
                    else:
                        _api_save(state, _cache_key(raw_qs), r.status, res_hdrs, body)
                return web.Response(status=r.status, reason=r.reason,
                                    headers=res_hdrs, body=body)
        async with session.request(request.method, url, headers=fwd, data=data,
                                   allow_redirects=False, auto_decompress=False) as r:
            res_hdrs = [(k, v) for k, v in r.headers.items() if k.lower() not in HOP_RES]
            sresp = web.StreamResponse(status=r.status, reason=r.reason, headers=res_hdrs)
            await sresp.prepare(request)
            async for chunk in r.content.iter_chunked(CHUNK):
                await sresp.write(chunk)
            await sresp.write_eof()
            return sresp
    except asyncio.CancelledError:
        raise
    except Exception as e:
        # リモート死亡時は502で返す (GUIはエラーを出すがプロキシは生き残る)
        log(f"[proxy] {request.method} {request.path} -> 失敗: {type(e).__name__}: {e}")
        return web.Response(status=502, text=f"remote-bridge: upstream failed: {e}")


# ---------- WebSocket中継 (/ws: GUIのキュー進行表示に必須) ----------

def _to_ws_url(remote: str, path_qs: str) -> str:
    if remote.startswith("https://"):
        return "wss://" + remote[len("https://"):] + path_qs
    if remote.startswith("http://"):
        return "ws://" + remote[len("http://"):] + path_qs
    raise ValueError(f"http(s):// で始まっていません: {remote}")


async def ws_proxy(request: web.Request, session: ClientSession, state: dict) -> web.WebSocketResponse:
    ws_server = web.WebSocketResponse(max_msg_size=64 * 1024 * 1024, heartbeat=30)
    await ws_server.prepare(request)
    try:
        ws_url = _to_ws_url(state["remote"], request.rel_url.raw_path_qs)
    except ValueError as e:
        log(f"[ws] {e}")
        await ws_server.close()
        return ws_server

    async def _forward(src: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
                       dst: web.WebSocketResponse | aiohttp.ClientWebSocketResponse) -> None:
        async for msg in src:
            try:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    code = msg.data if isinstance(msg.data, int) else 1000
                    try:
                        await dst.close(code=code, message=(msg.extra or "").encode()[:123])
                    except Exception:
                        pass
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSED,
                                  aiohttp.WSMsgType.CLOSING,
                                  aiohttp.WSMsgType.ERROR):
                    break
                # PING/PONG は aiohttp が自動応答するので転送しない
            except (ConnectionResetError, asyncio.CancelledError):
                break
            except Exception:
                break

    try:
        async with session.ws_connect(ws_url, max_msg_size=64 * 1024 * 1024,
                                      heartbeat=30,
                                      headers=_tunnel_headers(state["remote"])) as ws_remote:
            t1 = asyncio.create_task(_forward(ws_server, ws_remote))
            t2 = asyncio.create_task(_forward(ws_remote, ws_server))
            _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log(f"[ws] リモート接続失敗 (GUIは自動再接続します): {type(e).__name__}: {e}")
    finally:
        if not ws_server.closed:
            try:
                await ws_server.close()
            except Exception:
                pass
    return ws_server


async def handle(request: web.Request, session: ClientSession, state: dict):
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await ws_proxy(request, session, state)
    if state.get("local_ui"):
        # ローカル対象外はNoneが返る -> そのままリモートへフォールスルー
        try:
            resp = await handle_local_ui(request, state)
        except Exception as e:  # Note: ローカル失敗は500。黙ってリモート書き込みしない
            log(f"[local-ui] {request.method} {request.path} 失敗: {type(e).__name__}: {e}")
            return web.Response(status=500, text=f"remote-bridge local-ui failed: {e}")
        if resp is not None:
            return resp
    return await http_proxy(request, session, state)


# ---------- 生成物のローカル保存 (/historyポーリング) ----------

def dest_path(save_dir: Path, pid: str, sub: str, filename: str) -> Path | None:
    safe_name = Path(filename).name
    parts = [p for p in Path(sub or "").parts if p not in ("..", ".", "/", "\\", "")]
    dest = save_dir.joinpath(*parts, f"{pid[:8]}_{safe_name}")
    try:
        if not dest.resolve().is_relative_to(save_dir.resolve()):
            log(f"[warn] 不正なパスを拒否: subfolder={sub!r} filename={filename!r}")
            return None
    except Exception:
        return None
    return dest


async def fetch_and_save(session: ClientSession, remote: str, pid: str,
                         sub: str, filename: str, typ: str, save_dir: Path) -> bool:
    dest = dest_path(save_dir, pid, sub, filename)
    if dest is None:
        return True  # 拒否済み。二度と試さない
    if dest.exists():
        return True  # 再起動後の再取得は黙って既知扱い
    try:
        params = {"filename": filename, "subfolder": sub, "type": typ}
        async with session.get(remote + "/view", params=params,
                               headers=_tunnel_headers(remote),
                               timeout=ClientTimeout(total=120)) as r:
            if r.status != 200:
                return False
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name(dest.name + ".part")
            with open(tmp, "wb") as f:
                async for chunk in r.content.iter_chunked(CHUNK):
                    f.write(chunk)
            os.replace(tmp, dest)
        log(f"[saved] {dest}")
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        return False


async def sweep_outputs(session: ClientSession, remote: str, hist: dict,
                        save_dir: Path, seen: set, failed_once: set) -> None:
    if not isinstance(hist, dict):
        return
    for pid, entry in hist.items():
        if not isinstance(entry, dict):
            continue
        for node_out in (entry.get("outputs") or {}).values():
            if not isinstance(node_out, dict):
                continue
            for img in node_out.get("images") or []:
                if not isinstance(img, dict):
                    continue
                fn = img.get("filename")
                if not fn:
                    continue
                sub = img.get("subfolder") or ""
                typ = img.get("type") or "output"  # output/temp 両対応
                key = (pid, sub, fn, typ)
                if key in seen:
                    continue
                try:
                    ok = await fetch_and_save(session, remote, pid, sub, fn, typ, save_dir)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    ok = False
                if ok:
                    seen.add(key)
                    failed_once.discard(key)
                elif key not in failed_once:
                    failed_once.add(key)
                    log(f"[warn] 保存失敗 (次回pollで再試行): {fn} subfolder={sub!r} type={typ}")


async def poll_loop(session: ClientSession, state: dict, save_dir: Path,
                    seen: set, failed_once: set, url_file: str | None) -> None:
    last_ok = True
    last_mtime = 0.0
    last_check = 0.0
    while True:
        try:
            now = time.monotonic()
            if url_file and now - last_check >= URLFILE_INTERVAL:
                last_check = now
                try:
                    mt = os.path.getmtime(url_file)
                except OSError:
                    mt = 0.0
                if mt != last_mtime:
                    last_mtime = mt
                    u = read_url_file(url_file)
                    if u and u != state["remote"]:
                        state["remote"] = u
                        log(f"[url-file] リモート切替: {u}")
            remote = state["remote"]
            try:
                async with session.get(remote + "/history?max_items=20",
                                       headers=_tunnel_headers(remote),
                                       timeout=ClientTimeout(total=20)) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    hist = await r.json()
                if not last_ok:
                    log("[ok] リモートに再接続しました")
                last_ok = True
            except Exception as e:
                if last_ok:
                    log(f"[warn] /history 取得失敗 ({type(e).__name__}: {e}) "
                        "-- トンネルURL・Colabを確認")
                last_ok = False
                await asyncio.sleep(POLL_INTERVAL)
                continue
            await sweep_outputs(session, remote, hist, save_dir, seen, failed_once)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # Note: ポーラーは何があっても落とさない
            log(f"[warn] poller内部エラー: {type(e).__name__}: {e}")
        await asyncio.sleep(POLL_INTERVAL)


# ---------- 起動 ----------

async def check_remote(session: ClientSession, remote: str) -> bool:
    url = remote + "/system_stats"
    try:
        async with session.get(url, headers=_tunnel_headers(remote),
                               timeout=ClientTimeout(total=20)) as r:
            if r.status == 200:
                try:
                    info = await r.json()
                except Exception:
                    info = {}
                ver = (info.get("system") or {}).get("comfyui_version", "?")
                log(f"[ok] リモート接続OK: {url} (ComfyUI {ver})")
                return True
            log(f"[error] リモート応答異常: HTTP {r.status} <- {url}")
    except Exception as e:
        log(f"[error] リモートに接続できません: {url}")
        log(f"  原因: {type(e).__name__}: {e}")
    log("  心当たり: quick tunnelのURLが変わった可能性 "
        "(ランタイム再起動・cloudflared再作成でURLは変わります)")
    log("  対処: Colabでセル5を再実行 -> 新しい公開URLを確認 -> "
        "`url <新しいURL>` で切替か、--url-file を更新")
    return False


async def amain(args: argparse.Namespace) -> int:
    remote = args.remote.rstrip("/")
    if not remote.startswith(("http://", "https://")):
        log(f"[error] --remote は http(s):// で始めてください: {args.remote}")
        return 1
    state = {"remote": remote}
    if _is_tailscale(remote):
        log("[tailscale] P2P直結モード (http/Tailscale)。ngrokヘッダなし・キャッシュ有効")
        log("[tailscale] PCとColabが同じTailnetに入っていること（100.x.y.z が見える状態）を確認")
    if _is_pinggy(remote):
        log("[pinggy] SSHリモートフォワード経由 (TCP)。X-Pinggy-No-Screen を自動付与")
        log("[pinggy] 無料枠は60分でURLが変わります。Colabセル5のwatchdogが張り直したら `url <新しいURL>` で切替か --url-file を更新")
    state["local_ui"] = args.local_ui
    state["comfy_root"] = None
    state["web_root"] = None
    state["user_dir"] = None
    if args.local_ui:
        comfy_root = Path(args.comfy_root) if args.comfy_root else detect_comfy_root()
        web_root = (Path(args.web_root) if args.web_root
                    else detect_web_root(comfy_root))
        user_dir = (Path(args.user_dir) if args.user_dir
                    else (comfy_root / "user" if comfy_root else None))
        if web_root is None or user_dir is None or not user_dir.is_dir():
            log("[warn] ローカルUI素材を検出できません。完全プロキシで起動します "
                "(--web-root/--user-dir で上書き可、--no-local-ui で警告を消せます)")
            state["local_ui"] = False
        else:
            state["comfy_root"] = comfy_root
            state["web_root"] = web_root
            state["user_dir"] = user_dir
            log(f"[local-ui] UI配信: {web_root} (/, assets/, extensions/等をローカルから)")
            log(f"[local-ui] 設定・WF: {user_dir} (settings・userdataをローカル読書)")
            log("[local-ui] 実行系 (/prompt /queue /history /view /upload /ws "
                "/object_info /models) はリモートへ転送")
    cache_dir = Path(args.cache_dir) if args.cache_dir else Path(args.save_dir).parent / "bridge-cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        cache_dir = Path(tempfile.mkdtemp(prefix="bridge-cache-"))
        log(f"[warn] キャッシュdirを作れないのでtempを使います: {cache_dir}")
    state["cache_dir"] = cache_dir
    state["api_ttl"] = args.api_cache_ttl
    state["api_cache"] = {}
    save_dir = Path(args.save_dir)
    save_dir.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    failed_once: set = set()

    if args.url_file:
        u = read_url_file(args.url_file)
        if u and u != state["remote"]:
            state["remote"] = u
            log(f"[url-file] 初期URL (ファイル優先): {u}")

    timeout = ClientTimeout(total=600, sock_connect=30)
    async with ClientSession(timeout=timeout) as session:
        ok = await check_remote(session, state["remote"])
        if not ok and not args.url_file:
            return 1
        if not ok:
            log("[warn] --url-file の更新を待って起動を続行します")
        log(f"ローカル: http://{args.host}:{args.port}  →  リモート: {state['remote']}")
        log(f"キャッシュ: {cache_dir} / TTL={args.api_cache_ttl:g}s / ヒット時は X-Bridge-Cache: hit")

        poller = asyncio.create_task(
            poll_loop(session, state, save_dir, seen, failed_once, args.url_file))
        start_stdin_thread(state)

        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", lambda req: handle(req, session, state))
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, args.host, args.port).start()
        except OSError as e:
            log(f"[error] {args.host}:{args.port} で待ち受けできません: {e}")
            log("ローカルのComfyUIが既にこのポートを使っている可能性。止めるか --port で変えてください。")
            poller.cancel()
            await runner.cleanup()
            return 1
        log(f"待ち受け中: http://{args.host}:{args.port} (終了は Ctrl+C)")
        if args.host not in ("127.0.0.1", "localhost", "::1"):
            log("[warn] --host が外部公開です。認証なしプロキシなので 127.0.0.1 推奨。")
        try:
            await asyncio.Event().wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            poller.cancel()
            try:
                await poller
            except asyncio.CancelledError:
                pass
            await runner.cleanup()
    log("終了します")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="ComfyUI remote-bridge: ブラウザはローカル・実行はColab・保存はローカル")
    ap.add_argument("--remote", required=True, help="Colabの公開URL (推奨: http://100.x.y.z:8188 [Tailscale]、他に https://xxxx.trycloudflare.com / ngrok / pinggy)")
    ap.add_argument("--host", default="127.0.0.1", help="既定 127.0.0.1 (0.0.0.0 は非推奨)")
    ap.add_argument("--port", type=int, default=8188, help="既定 8188")
    ap.add_argument("--save-dir", default=r"D:\OSS_ProgramFiles\ComfyUI\output\remote",
                    help="生成画像の保存先")
    ap.add_argument("--url-file", default=None,
                    help="トンネルURLを書いたテキストファイル。5秒ごとに読み直して自動追従")
    ap.add_argument("--cache-dir", default=None,
                    help="静的アセットの保存先。既定は <save-dirの親>/bridge-cache")
    ap.add_argument("--api-cache-ttl", type=float, default=300,
                    help="重い読み取りAPIの保持秒数。0で無効。既定300")
    ap.add_argument("--no-local-ui", dest="local_ui", action="store_false",
                    default=True,
                    help="ローカルUI配信を無効化し、全てをリモートへ転送する (既定: 有効)")
    ap.add_argument("--web-root", default=None,
                    help="フロントエンド静的dirの上書き (既定: 自動検出)")
    ap.add_argument("--user-dir", default=None,
                    help="user dirの上書き (既定: <ComfyUI>/user)")
    ap.add_argument("--comfy-root", default=None,
                    help="ComfyUIルートの上書き (既定: 自動検出)")
    args = ap.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        log("\n終了します")
        return 0


if __name__ == "__main__":
    sys.exit(main())
