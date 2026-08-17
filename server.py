# -*- coding: utf-8 -*-
"""云端工作台服务（Flask）。

- 提供两个页面：/ (总工作台) 与 /anchor (主播工作台)
- 提供读写 API，直接调用飞书 OpenAPI（无需本地登录）
- 凭证来自环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET
"""
import os
import json
import time
import tempfile
import threading
import hashlib
from flask import Flask, request, send_file, jsonify, make_response

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_DIR = os.path.join(HERE, "pages")  # cloud/pages/ 仅含页面，不暴露源码
CACHE_DIR = os.path.join(HERE, ".cache")
CACHE_FILE = os.path.join(CACHE_DIR, "api_data.json")
CACHE_TTL = 120  # 秒：文件级缓存，避免每次刷新都重拉飞书（写入后自动失效）
import cloud_sync as cs

app = Flask(__name__, static_folder=HTML_DIR, static_url_path="")


@app.route("/")
def index():
    return send_file(os.path.join(HTML_DIR, "总工作台.html"))


@app.route("/anchor")
def anchor():
    return send_file(os.path.join(HTML_DIR, "主播工作台.html"))


def _get_all_cached():
    # 文件级缓存：跨请求/进程/线程共享，避免每次刷新都重拉飞书
    if os.path.exists(CACHE_FILE):
        age = time.time() - os.path.getmtime(CACHE_FILE)
        if age < CACHE_TTL:
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except Exception:
                pass
    data = cs.get_all()
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(CACHE_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:
        pass
    return data


def _invalidate():
    """写操作后清空缓存，保证下次拉取是最新数据。"""
    try:
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
    except Exception:
        pass


_fwd_lock = threading.Lock()
_fwd_running = False
_fwd_pending = False
_sync_state = {"running": False, "last": None, "last_result": None, "last_error": None}


def _run_forward():
    """在后台线程里执行同步（含图片二次上传，可能 10~40s）。"""
    global _fwd_running, _fwd_pending, _sync_state
    _sync_state["running"] = True
    _sync_state["last_error"] = None
    try:
        while True:
            result = cs.run_sync()
            _sync_state["last_result"] = result
            _sync_state["last"] = time.strftime("%Y-%m-%d %H:%M:%S")
            with _fwd_lock:
                if _fwd_pending:
                    _fwd_pending = False
                    continue
                break
    except Exception as e:
        _sync_state["last_error"] = str(e)
        print("background forward failed:", e)
    finally:
        _sync_state["running"] = False
        with _fwd_lock:
            _fwd_running = False


def _bg_forward():
    """把 forward 同步放到后台线程执行，避免阻塞保存请求（保存秒回）。

    若同步期间又有新保存，用 _fwd_pending 标记，同步结束后补跑一次，
    避免遗漏最新的总台账改动。
    """
    global _fwd_running, _fwd_pending
    with _fwd_lock:
        if _fwd_running:
            _fwd_pending = True
            return
        _fwd_running = True
    threading.Thread(target=_run_forward, daemon=True).start()


@app.route("/api/data")
def api_data():
    try:
        return jsonify(_get_all_cached())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


IMG_CACHE_DIR = os.path.join(HERE, ".imgcache")
IMG_TTL = 3600


@app.route("/img")
def api_img():
    """图片懒加载端点：前端传飞书附件直链(u)，后端下载一次并缓存，返回 JPEG 字节。"""
    u = request.args.get("u")
    if not u:
        return "missing u", 400
    h = hashlib.sha1(u.encode("utf-8")).hexdigest()
    cached = os.path.join(IMG_CACHE_DIR, h + ".jpg")
    if os.path.exists(cached) and time.time() - os.path.getmtime(cached) < IMG_TTL:
        with open(cached, "rb") as fh:
            data = fh.read()
    else:
        data = cs.f.attachment_to_bytes([{"url": u}])
        if not data:
            return "image fetch failed", 502
        try:
            os.makedirs(IMG_CACHE_DIR, exist_ok=True)
            with open(cached, "wb") as fh:
                fh.write(data)
        except Exception:
            pass
    resp = make_response(data)
    resp.headers["Content-Type"] = "image/jpeg"
    resp.headers["Cache-Control"] = "public, max-age=%d" % IMG_TTL
    return resp


@app.route("/api/total", methods=["POST"])
def api_total():
    body = request.get_json(force=True, silent=True) or {}
    try:
        if body.get("record_id"):
            res = jsonify(cs.update_total(body["record_id"], body))
        else:
            res = jsonify(cs.add_total(body))
        _invalidate()      # 立即失效缓存，前端刷新即可看到最新总台账
        _bg_forward()      # 后台把在售苗子同步到主播台（含图片），不阻塞保存
        return res
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/total/<record_id>", methods=["DELETE"])
def api_total_del(record_id):
    try:
        res = jsonify(cs.delete_total(record_id))
        _invalidate()
        _bg_forward()
        return res
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/loan", methods=["POST"])
def api_loan():
    body = request.get_json(force=True, silent=True) or {}
    try:
        res = jsonify(cs.add_loan(body))
        _invalidate()
        return res
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mark_sold", methods=["POST"])
def api_mark_sold():
    body = request.get_json(force=True, silent=True) or {}
    try:
        res = jsonify(cs.mark_sold(body.get("record_id")))
        _invalidate()
        return res
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/recover", methods=["POST"])
def api_recover():
    body = request.get_json(force=True, silent=True) or {}
    try:
        res = jsonify(cs.recover_anchor(body.get("record_id")))
        _invalidate()
        return res
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync", methods=["POST", "GET"])
def api_sync():
    """手动/定时触发主播台同步。POST 立即后台触发并返回；GET 返回当前同步状态。"""
    try:
        if request.method == "POST":
            _bg_forward()
            _invalidate()
            return jsonify({"ok": True, "running": _fwd_running,
                            "last": _sync_state["last"],
                            "last_error": _sync_state["last_error"]})
        return jsonify({"running": _fwd_running, "last": _sync_state["last"],
                        "last_result": _sync_state["last_result"],
                        "last_error": _sync_state["last_error"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """接收网页上传的图片，存到飞书文件1 的附件空间，返回 file_token。"""
    up = request.files.get("file")
    if not up:
        return jsonify({"error": "未收到文件"}), 400
    name = up.filename or "snake.jpg"
    suffix = os.path.splitext(name)[1].lower() or ".jpg"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    up.save(tmp)
    try:
        token = cs.f.upload_attachment(cs.FILE1, cs.TOTAL, None, "图片", tmp, name=name)
        return jsonify({"file_token": token, "name": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _warm_cache():
    """应用启动时预热文件缓存，避免冷启动首访慢。"""
    try:
        _get_all_cached()
    except Exception as e:
        print("warm cache failed:", e)


threading.Thread(target=_warm_cache, daemon=True).start()

# 定时自动同步：默认每 5 分钟把总台账同步到主播台。
# 可在 Railway 环境变量设 SYNC_INTERVAL（秒）调整，例如 900 = 15 分钟。
_SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "300"))


def _schedule_sync():
    """后台线程：每隔 SYNC_INTERVAL 秒自动触发一次主播台同步。"""
    while True:
        time.sleep(_SYNC_INTERVAL)
        try:
            _bg_forward()
        except Exception as e:
            print("scheduled sync error:", e)


threading.Thread(target=_schedule_sync, daemon=True).start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
