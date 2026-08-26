# -*- coding: utf-8 -*-
"""云端同步逻辑（基于 feishu_api 的 OpenAPI 实现）。

文件1（私有）: OZGLbaZWNavOAcsKH2ccSYBlnCb
   总台账  tblGvKYvzJr3VZ2t
   租借    tbl3lJDlYGFasbUR
文件2（主播）: S69DbXTKPaNbMesylMTcFeUInqu
   主播表  tblEXYGLRS2MEgEQ

正向：文件1 处置=在售 -> 文件2 主播表（状态强制=在售）
反向：文件2 状态=已售 -> 文件1 处置=已售 + 出库时间
图片：首建时从文件1 下载并上传到文件2
"""
import os
import io
import time
import base64
import requests
from PIL import Image
from concurrent.futures import ThreadPoolExecutor
import feishu_api as f

FILE1 = "OZGLbaZWNavOAcsKH2ccSYBlnCb"
TOTAL = "tblGvKYvzJr3VZ2t"
LOAN = "tbl3lJDlYGFasbUR"
FILE2 = "S69DbXTKPaNbMesylMTcFeUInqu"
ANCHOR2 = "tblEXYGLRS2MEgEQ"

MAP = ["抽屉号", "品种", "性别", "出生日期", "体重(g)", "供货价", "建议价格"]
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sync_tmp")

# 销售收款情况所需的字段（加进总台账，单一数据源）。ensure_sales_fields() 会幂等创建。
SALES_FIELDS = [
    {"name": "售出价", "type": 2, "property": {"formatter": "0.00"}},
    {"name": "收款状态", "type": 3,
     "property": {"options": [{"name": "未收"}, {"name": "部分收"}, {"name": "已收"}]}},
    {"name": "收货方", "type": 1, "property": {}},
    {"name": "收款日期", "type": 5,
     "property": {"auto_fill": False, "date_formatter": "yyyy-MM-dd"}},
]


def ensure_sales_fields():
    """幂等：若总台账尚未含销售/收款字段则创建，已存在则跳过。失败不抛错（不影响主流程）。"""
    try:
        existing = {x.get("field_name") for x in f.list_fields(FILE1, TOTAL)}
    except Exception as e:
        print("ensure_sales_fields 读取字段失败:", e)
        return
    for spec in SALES_FIELDS:
        if spec["name"] in existing:
            continue
        try:
            f.create_field(FILE1, TOTAL, spec["name"], spec["type"], spec.get("property"))
            print("  已创建字段:", spec["name"])
        except Exception as e:
            print("  创建字段失败(%s):" % spec["name"], e)


def _norm_date(v):
    """毫秒时间戳 int / ISO / 字符串 -> 显示用 'YYYY-MM-DD'（按北京时间 UTC+8）。"""
    if not v:
        return ""
    if isinstance(v, (int, float)):
        ms = int(v)
        if ms < 1e12:
            ms = ms * 1000
        import datetime as _dt
        # 飞书返回 UTC 毫秒；按北京时间展示，避免凌晨时段跨日偏差
        return (_dt.datetime.utcfromtimestamp(ms / 1000)
                + _dt.timedelta(hours=8)).strftime("%Y-%m-%d")
    s = str(v)
    return s[:10] if "T" in s else (s[:10] if len(s) >= 10 else s)


# 所有日期型字段统一规范化为 'YYYY-MM-DD' 字符串。
# 注意：飞书日期字段回传的是毫秒时间戳(int)，若不在此列，前端对其做字符串操作会抛异常。
DATE_FIELDS = ("出生日期", "租借日期", "归还日期", "收款日期", "出库", "发货")


def _to_ms(v):
    """'YYYY-MM-DD' / ISO / 毫秒int / 秒int -> 飞书 Date 需要的毫秒时间戳 int。"""
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        m = int(v)
        if m < 1e12:
            m = m * 1000
        return m
    s = str(v).strip()
    if s.isdigit():
        return _to_ms(int(s))
    s2 = s.replace("Z", "").replace("T", " ")
    import datetime as _dt
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
        try:
            t = _dt.datetime.strptime(s2[:19] if " " in s2 else s2[:10], fmt)
            return int(t.timestamp() * 1000)
        except ValueError:
            continue
    return ""


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


def normalize(rec):
    """API 记录 -> 前端友好 dict。图片不在此下载，仅暴露飞书直链供 /img 懒加载。"""
    rid = rec.get("record_id")
    fld = rec.get("fields", {})
    out = {"record_id": rid}
    for k, v in fld.items():
        if k in DATE_FIELDS:
            out[k] = _norm_date(v)
        elif isinstance(v, dict) and "text" in v:  # 单选等可能返回 {text,value}
            out[k] = v["text"]
        else:
            out[k] = v
    att = fld.get("图片")
    if isinstance(att, list) and att:
        item = att[0]
        out["img_url"] = item.get("url") or item.get("tmp_url") or item.get("temp_download_url")
        out["img_token"] = item.get("file_token")
    return out


def get_all():
    total = [normalize(r) for r in f.list_records(FILE1, TOTAL)]
    loan = [normalize(r) for r in f.list_records(FILE1, LOAN)]
    anchor = [normalize(r) for r in f.list_records(FILE2, ANCHOR2)]
    return {"total": total, "loan": loan, "anchor": anchor,
            "updated_at": time.strftime("%Y-%m-%d %H:%M")}


# ---------- 正向：文件1 -> 文件2 ----------
def _download_bytes(url):
    r = requests.get(url, headers={"Authorization": "Bearer " + f._token()}, timeout=20)
    r.raise_for_status()
    return r.content


def _push_image(src_rec, anchor_id, existing_token=None):
    """把源记录的图片同步到主播看板记录。

    - 源记录无图 -> 跳过
    - 主播台已有相同 file_token -> 跳过（避免每次 forward 都重复上传）
    - 否则下载原图字节直接重传到文件2（不再重新编码，省时省流量）
    """
    att = src_rec.get("fields", {}).get("图片")
    if not isinstance(att, list) or not att:
        return
    src_token = att[0].get("file_token")
    if existing_token == src_token:
        return
    url = att[0].get("url") or att[0].get("tmp_url") or att[0].get("temp_download_url")
    if not url:
        return
    try:
        raw = _download_bytes(url)
        fn = att[0].get("name") or "snake.jpg"
        # 上传原图字节（不重新编码），归属文件2
        token = f.upload_attachment(FILE2, ANCHOR2, anchor_id, "图片",
                                    _to_tmpfile(raw, fn), name=fn)
        f.update_record(FILE2, ANCHOR2, anchor_id,
                        {"图片": [{"file_token": token, "name": fn, "type": "image/jpeg"}]})
    except Exception as e:
        print("  图片推送失败:", e)


def _to_tmpfile(data, name):
    os.makedirs(TMP, exist_ok=True)
    p = os.path.join(TMP, name)
    with open(p, "wb") as fh:
        fh.write(data)
    return p


def forward():
    total = f.list_records(FILE1, TOTAL)
    anchor = f.list_records(FILE2, ANCHOR2)
    by_src = {r["fields"].get("源记录ID"): r["record_id"]
              for r in anchor if r["fields"].get("源记录ID")}
    anchor_status = {r["record_id"]: r["fields"].get("状态") for r in anchor}
    # 主播台现有图片 token，用于判断是否需要重新推送
    anchor_imgs = {}
    for r in anchor:
        a = r.get("fields", {}).get("图片")
        if isinstance(a, list) and a:
            anchor_imgs[r["record_id"]] = a[0].get("file_token")
    onsale = [r for r in total if r["fields"].get("处置") == "在售"]
    onsale_ids = {r["record_id"] for r in onsale}
    created = updated = 0
    for r in onsale:
        mapped = {k: r["fields"][k] for k in MAP if k in r["fields"] and r["fields"][k] is not None}
        mapped["出生日期"] = _to_ms(mapped.get("出生日期"))
        mapped["体重(g)"] = _num(mapped.get("体重(g)"))
        mapped["供货价"] = _num(mapped.get("供货价"))
        mapped["源记录ID"] = r["record_id"]
        mapped["状态"] = "在售"
        if r["record_id"] in by_src:
            aid = by_src[r["record_id"]]
            # 主播已手动标记「已售」的，不要覆盖回在售（保留灰色留板）
            if anchor_status.get(aid) == "已售":
                continue
            f.update_record(FILE2, ANCHOR2, aid, mapped)
            # 编辑时也要把（可能更换的）图片同步到主播台
            _push_image(r, aid, anchor_imgs.get(aid))
            updated += 1
        else:
            new = f.create_record(FILE2, ANCHOR2, mapped)
            created += 1
            _push_image(r, new["record_id"])
    # 清理：仅当 文件1 已不在售 且 文件2 该条并非「已售」时才删除（已售的保留给主播看灰）
    stale = [aid for src, aid in by_src.items()
             if src not in onsale_ids and anchor_status.get(aid) != "已售"]
    if stale:
        f.batch_delete(FILE2, ANCHOR2, stale)
    return {"created": created, "updated": updated, "removed": len(stale)}


# ---------- 反向：文件2 已售 -> 文件1 ----------
def backward():
    anchor = f.list_records(FILE2, ANCHOR2)
    sold = [r for r in anchor if r["fields"].get("状态") == "已售"]
    done = 0
    for r in sold:
        src = r["fields"].get("源记录ID")
        if not src:
            continue
        try:
            f.update_record(FILE1, TOTAL, src,
                            {"处置": "已售", "出库": int(time.time() * 1000)})
            done += 1
        except Exception as e:
            print("  回写失败", src, e)
    return {"writeback": done}


def run_sync():
    b = backward()
    fr = forward()
    return {"backward": b, "forward": fr}


# ---------- 编辑接口（供 server 调用） ----------
def add_total(fields):
    payload = {
        "抽屉号": fields.get("抽屉号", ""),
        "品种": fields.get("品种", ""),
        "性别": fields.get("性别", ""),
        "出生日期": _to_ms(fields.get("出生日期")),
        "体重(g)": _num(fields.get("体重(g)")),
        "供货价": _num(fields.get("供货价")),
        "建议价格": fields.get("建议价格", ""),
        "处置": fields.get("处置", "在售"),
        "售出价": _num(fields.get("售出价")),
    }
    for k in ("收款状态", "收货方"):
        if fields.get(k):
            payload[k] = fields[k]
    if fields.get("收款日期"):
        payload["收款日期"] = _to_ms(fields["收款日期"])
    if fields.get("图片"):
        payload["图片"] = fields["图片"]
    rec = f.create_record(FILE1, TOTAL, payload)
    return rec


def update_total(record_id, fields):
    payload = {}
    for k in ("抽屉号", "品种", "性别", "建议价格", "处置", "收款状态", "收货方"):
        if k in fields and fields[k] != "":
            payload[k] = fields[k]
    if "出生日期" in fields:
        payload["出生日期"] = _to_ms(fields["出生日期"])
    if "体重(g)" in fields:
        payload["体重(g)"] = _num(fields["体重(g)"])
    if "供货价" in fields:
        payload["供货价"] = _num(fields["供货价"])
    if "售出价" in fields:
        payload["售出价"] = _num(fields["售出价"])
    if fields.get("收款日期"):
        payload["收款日期"] = _to_ms(fields["收款日期"])
    if fields.get("图片"):
        payload["图片"] = fields["图片"]
    f.update_record(FILE1, TOTAL, record_id, payload)
    return {"ok": True}


def delete_total(record_id):
    f.delete_record(FILE1, TOTAL, record_id)
    return {"ok": True}


def add_loan(fields):
    payload = {
        "抽屉号": fields.get("抽屉号", ""),
        "品种": fields.get("品种", ""),
        "性别": fields.get("性别", ""),
        "出生日期": _to_ms(fields.get("出生日期")),
        "体重(g)": _num(fields.get("体重(g)")),
        "租借方": fields.get("租借方", ""),
        "租借日期": _to_ms(fields.get("租借日期")),
        "归还日期": _to_ms(fields.get("归还日期")),
        "状态": fields.get("状态", "租借中"),
        "备注": fields.get("备注", ""),
    }
    return f.create_record(FILE1, LOAN, payload)


def mark_sold(record_id):
    # record_id 是主播看板(FILE2)的 record_id：标记已售后仍保留在主播看板（变灰），不删除
    anchor_rows = f.list_records(FILE2, ANCHOR2)
    row = next((r for r in anchor_rows if r.get("record_id") == record_id), None)
    src = row["fields"].get("源记录ID") if row else None
    if src:
        f.update_record(FILE1, TOTAL, src,
                        {"处置": "已售", "出库": int(time.time() * 1000)})
    if row:
        f.update_record(FILE2, ANCHOR2, record_id, {"状态": "已售"})
    return {"ok": True, "anchor_record_id": record_id}


def sell_total(record_id, fields=None):
    """从「总台账」侧登记售出（record_id 是 FILE1 总台账的 record_id）。

    旧实现误把总台账 id 传给只在主播表查找的 mark_sold()，导致静默无效。
    这里直接写 FILE1，并联动把主播台对应记录置为「已售」（变灰留板）。
    """
    fields = fields or {}
    payload = {"处置": "已售"}
    # 出库时间：已有则不覆盖，避免重复登记时把原始出库时间改掉
    try:
        cur = f.get_record(FILE1, TOTAL, record_id) or {}
        # 飞书「查询单条」返回 {"record": {...}}，兼容直接返回记录体的情况
        rec = cur.get("record", cur) if isinstance(cur, dict) else {}
        cur_fields = rec.get("fields", {}) if isinstance(rec, dict) else {}
    except Exception:
        cur_fields = {}
    if not cur_fields.get("出库"):
        payload["出库"] = int(time.time() * 1000)
    if fields.get("售出价") not in (None, ""):
        payload["售出价"] = _num(fields["售出价"])
    for k in ("收款状态", "收货方"):
        if fields.get(k):
            payload[k] = fields[k]
    if fields.get("收款日期"):
        payload["收款日期"] = _to_ms(fields["收款日期"])
    f.update_record(FILE1, TOTAL, record_id, payload)
    # 联动主播台：把映射到该源记录的看板卡置为已售（保留灰卡）
    linked = None
    try:
        for r in f.list_records(FILE2, ANCHOR2):
            if r["fields"].get("源记录ID") == record_id:
                f.update_record(FILE2, ANCHOR2, r["record_id"], {"状态": "已售"})
                linked = r["record_id"]
                break
    except Exception as e:
        print("  主播台联动失败:", e)
    return {"ok": True, "anchor_record_id": linked}


def set_paid(record_id, date=None):
    """一键收款：把总台账记录标记为「已收」并写入收款日期（默认今天，北京时间）。"""
    import datetime as _dt
    if not date:
        date = (_dt.datetime.utcnow() + _dt.timedelta(hours=8)).strftime("%Y-%m-%d")
    f.update_record(FILE1, TOTAL, record_id,
                    {"收款状态": "已收", "收款日期": _to_ms(date)})
    return {"ok": True, "收款日期": date}


def recover_anchor(record_id):
    # 主播台「恢复售卖」：文件2 状态改回在售 + 文件1 处置改回在售
    anchor_rows = f.list_records(FILE2, ANCHOR2)
    row = next((r for r in anchor_rows if r.get("record_id") == record_id), None)
    src = row["fields"].get("源记录ID") if row else None
    if row:
        f.update_record(FILE2, ANCHOR2, record_id, {"状态": "在售"})
    if src:
        f.update_record(FILE1, TOTAL, src, {"处置": "在售"})
    return {"ok": True}
