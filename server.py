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
from flask import Flask, request, send_file, jsonify

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


@app.route("/api/data")
def api_data():
    try:
        return jsonify(_get_all_cached())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/total", methods=["POST"])
def api_total():
    body = request.get_json(force=True, silent=True) or {}
    try:
        if body.get("record_id"):
            res = jsonify(cs.update_total(body["record_id"], body))
        else:
            res = jsonify(cs.add_total(body))
        _invalidate()
        return res
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/total/<record_id>", methods=["DELETE"])
def api_total_del(record_id):
    try:
        res = jsonify(cs.delete_total(record_id))
        _invalidate()
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


@app.route("/api/sync", methods=["POST"])
def api_sync():
    try:
        res = jsonify(cs.run_sync())
        _invalidate()
        return res
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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
