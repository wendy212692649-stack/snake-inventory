# -*- coding: utf-8 -*-
"""飞书多维表格 OpenAPI 客户端（应用凭证 / tenant_access_token）。

与本地 lark-cli 不同，这里用 app_id/app_secret 换取 tenant_access_token，
可直接在云端（任意服务器）调用飞书，无需用户本地登录。
凭证从环境变量读取：FEISHU_APP_ID / FEISHU_APP_SECRET
"""
import os
import time
import requests

APP_ID = os.environ.get("FEISHU_APP_ID", "")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")

BASE = "https://open.feishu.cn/open-apis"
_TOKEN = {"t": None, "exp": 0}
_IMG_CACHE = {}  # url -> (ts, data_uri) 图片下载结果缓存，避免每次刷新都重拉飞书拖慢页面


def _token():
    now = time.time()
    if _TOKEN["t"] and now < _TOKEN["exp"] - 120:
        return _TOKEN["t"]
    if not APP_ID or not APP_SECRET:
        raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET 环境变量")
    r = requests.post(
        f"{BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=15,
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError("获取 tenant_access_token 失败: %s" % d)
    _TOKEN["t"] = d["tenant_access_token"]
    _TOKEN["exp"] = now + d.get("expire", 7200)
    return _TOKEN["t"]


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
        r = requests.get(u, headers=_hdr(), timeout=20)
        d = r.json()
        if d.get("code") != 0:
            raise RuntimeError("读取记录失败: %s" % d)
        out.extend(d["data"].get("items", []))
        if not d["data"].get("has_more"):
            break
        page_token = d["data"].get("page_token")
    return out


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
    """上传本地文件到指定记录的附件字段，返回 file_token。"""
    import mimetypes
    name = name or os.path.basename(file_path)
    mime = mimetypes.guess_type(name)[0] or "image/png"
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
    if d.get("code") != 0:
        raise RuntimeError("附件上传失败: %s" % d)
    return d["data"]["file_token"]


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
