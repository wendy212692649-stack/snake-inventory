# -*- coding: utf-8 -*-
"""飞书多维表格 OpenAPI 客户端（应用凭证 / tenant_access_token）。

与本地 lark-cli 不同，这里用 app_id/app_secret 换取 tenant_access_token，
可直接在云端（任意服务器）调用飞书，无需用户本地登录。
凭证从环境变量读取：FEISHU_APP_ID / FEISHU_APP_SECRET
"""
import os
import time
import threading
import requests

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

BASE = "https://open.feishu.cn/open-apis"
_TOKEN = {"t": None, "exp": 0}
_TOKEN_LOCK = threading.Lock()  # 防止多线程并发刷新互相踩掉 token（会导致偶发 401/过期）
_IMG_CACHE = {}  # url -> (ts, data_uri) 图片下载结果缓存，避免每次刷新都重拉飞书拖慢页面
_IMG_BYTES = {}  # url -> (ts, bytes) 图片字节缓存，配合 /img 懒加载端点

# 飞书偶发返回的临时错误码：限流/内部错误/未知，重试大概率能恢复；业务类错误（如跨表 token 错）不重试
_RETRYABLE_CODES = {429, 500, 9999, 99991400, 99991663}


def _token():
    # 快速路径：未过期直接返回，不进锁，不阻塞并发请求
    now = time.time()
    if _TOKEN["t"] and now < _TOKEN["exp"] - 120:
        return _TOKEN["t"]
    with _TOKEN_LOCK:
        # double-check：进锁后可能已被别的线程刷新好
        now = time.time()
        if _TOKEN["t"] and now < _TOKEN["exp"] - 120:
            return _TOKEN["t"]
        if not APP_ID or not APP_SECRET:
            raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量")
        last = None
        for _ in range(3):  # 刷新 token 本身也重试，避免瞬时网络抖动导致全站 401
            try:
                r = requests.post(
                    f"{BASE}/auth/v3/tenant_access_token/internal",
                    json={"app_id": APP_ID, "app_secret": APP_SECRET},
                    timeout=15,
                )
                d = r.json()
                if d.get("code") == 0:
                    _TOKEN["t"] = d["tenant_access_token"]
                    _TOKEN["exp"] = now + d.get("expire", 7200)
                    return _TOKEN["t"]
                last = RuntimeError("获取 tenant_access_token 失败: %s" % d)
            except Exception as e:
                last = e
            time.sleep(0.4)
        raise last or RuntimeError("获取 tenant_access_token 失败")


def _hdr():
    return {"Authorization": "Bearer " + _token(), "Content-Type": "application/json"}


def _url(app_token, table_id, path=""):
    return f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}/records{path}"


def list_records(app_token, table_id, page_size=100):
    out, page_token = [], None
    while True:
        u = _url(app_token, table_id) + f"?page_size={page_size}"
        if page_token:
            u += "&page_token=" + page_token
        last = None
        ok = False
        d = None
        for attempt in range(3):  # 网络抖动 / 限流自动重试
            try:
                r = requests.get(u, headers=_hdr(), timeout=20)
                d = r.json()
                if d.get("code") == 0:
                    ok = True
                    break
                if d.get("code") in _RETRYABLE_CODES:  # 临时错才重试，业务错直接抛
                    last = RuntimeError("读取记录失败: %s" % d)
                    time.sleep(0.5 * (2 ** attempt))
                    continue
                raise RuntimeError("读取记录失败: %s" % d)
            except requests.RequestException as e:
                last = e
                time.sleep(0.5 * (2 ** attempt))
                continue
        if not ok:
            raise last or RuntimeError("读取记录失败")
        out.extend(d["data"].get("items", []))
        if not d["data"].get("has_more"):
            break
        page_token = d["data"].get("page_token")
    return out


def _url_base(app_token, table_id):
    return f"{BASE}/bitable/v1/apps/{app_token}/tables/{table_id}"


def list_fields(app_token, table_id):
    """列出多维表格的所有字段（含 field_name / type / property），用于探测现有结构、做幂等建字段。"""
    r = requests.get(_url_base(app_token, table_id) + "/fields", headers=_hdr(), timeout=20)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("读取字段失败: %s" % d)
    return d["data"].get("items", [])


def create_field(app_token, table_id, name, ftype, property=None):
    """在多维表格新建一个字段。ftype 为飞书字段类型字符串（如 'number'/'text'/'single_select'/'date'）。"""
    body = {"field_name": name, "type": ftype}
    if property is not None:
        body["property"] = property
    r = requests.post(_url_base(app_token, table_id) + "/fields", headers=_hdr(),
                      json=body, timeout=20)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("创建字段失败(%s): %s" % (name, d))
    return d["data"]


def get_record(app_token, table_id, record_id):
    r = requests.get(_url(app_token, table_id, "/" + record_id), headers=_hdr(), timeout=20)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("读取单条失败: %s" % d)
    return d["data"]


def create_record(app_token, table_id, fields):
    r = requests.post(_url(app_token, table_id), headers=_hdr(),
                      json={"fields": fields}, timeout=20)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("创建记录失败: %s" % d)
    data = d.get("data", {})
    return data.get("record", data)  # 真实记录对象，含 record_id


def update_record(app_token, table_id, record_id, fields):
    r = requests.put(_url(app_token, table_id, "/" + record_id), headers=_hdr(),
                     json={"fields": fields}, timeout=20)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("更新记录失败: %s" % d)
    data = d.get("data", {})
    return data.get("record", data)


def delete_record(app_token, table_id, record_id):
    r = requests.delete(_url(app_token, table_id, "/" + record_id), headers=_hdr(), timeout=20)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("删除记录失败: %s" % d)
    return d


def batch_delete(app_token, table_id, record_ids):
    r = requests.post(_url(app_token, table_id) + "/batch_delete",
                      headers=_hdr(), json={"records": record_ids}, timeout=20)
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("批量删除失败: %s" % d)
    return d


def upload_attachment(app_token, table_id, record_id, field_id, file_path, name=None):
    """上传本地文件到指定记录的附件字段，返回 file_token。带重试（每次重试重新打开文件）。"""
    import mimetypes
    name = name or os.path.basename(file_path)
    mime = mimetypes.guess_type(name)[0] or "image/png"
    last = None
    for attempt in range(3):
        try:
            with open(file_path, "rb") as f:
                files = {"file": (name, f, mime)}
                # 飞书 drive 上传（bitable 附件底层走 drive）
                r = requests.post(
                    f"{BASE}/drive/v1/files/upload_all",
                    headers={"Authorization": "Bearer " + _token()},
                    data={"file_name": name, "parent_type": "bitable_file",
                          "parent_node": app_token, "size": str(os.path.getsize(file_path))},
                    files=files,
                    timeout=30,
                )
            d = r.json()
            if d.get("code") == 0:
                return d["data"]["file_token"]
            # 业务错误（如跨表 token 错 1254303）不重试，直接抛
            raise RuntimeError("附件上传失败: %s" % d)
        except RuntimeError:
            raise  # 业务类错误不重试
        except Exception as e:
            last = e
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise RuntimeError("附件上传失败(重试后): %s" % e)
    raise last or RuntimeError("附件上传失败")


def attachment_to_datauri(att_list, max_w=720):
    """把附件字段（含 temp_download_url）下载并压缩为 data URI。带缓存避免重复下载。"""
    from io import BytesIO
    from PIL import Image
    if not isinstance(att_list, list) or not att_list:
        return None
    item = att_list[0]
    url = item.get("url") or item.get("tmp_url") or item.get("temp_download_url")
    if not url:
        return None
    if url in _IMG_CACHE:
        ts, val = _IMG_CACHE[url]
        if time.time() - ts < 600:  # 10 分钟内复用，避免重复下载
            return val
    try:
        r = requests.get(url, headers={"Authorization": "Bearer " + _token()}, timeout=20)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        if img.width > max_w:
            h = int(img.height * max_w / img.width)
            img = img.resize((max_w, h))
        buf = BytesIO()
        img.save(buf, "JPEG", quality=72)
        b64 = __import__("base64").b64encode(buf.getvalue()).decode()
        datauri = "data:image/jpeg;base64," + b64
        _IMG_CACHE[url] = (time.time(), datauri)
        return datauri
    except Exception:
        return None


def attachment_to_bytes(att_list, max_w=720):
    """下载附件并压缩为 JPEG 字节（不内联 base64），供 /img 懒加载端点使用。带内存缓存避免重复下载。"""
    from io import BytesIO
    from PIL import Image
    if not isinstance(att_list, list) or not att_list:
        return None
    item = att_list[0]
    url = item.get("url") or item.get("tmp_url") or item.get("temp_download_url")
    if not url:
        return None
    if url in _IMG_BYTES:
        ts, val = _IMG_BYTES[url]
        if time.time() - ts < 600:
            return val
    raw = None
    for attempt in range(3):  # 图片下载偶发超时/5xx，重试恢复，避免前端裂图
        try:
            r = requests.get(url, headers={"Authorization": "Bearer " + _token()}, timeout=20)
            r.raise_for_status()
            raw = r.content
            break
        except Exception:
            time.sleep(0.5 * (2 ** attempt))
    if raw is None:
        return None
    try:
        img = Image.open(BytesIO(raw)).convert("RGB")
        if img.width > max_w:
            h = int(img.height * max_w / img.width)
            img = img.resize((max_w, h))
        buf = BytesIO()
        img.save(buf, "JPEG", quality=72)
        data = buf.getvalue()
        _IMG_BYTES[url] = (time.time(), data)
        return data
    except Exception:
        return None
