# -*- coding: utf-8 -*-
"""云端工作台服务（Flask）。

- 提供两个页面：/ (总工作台) 与 /anchor (主播工作台)
- 提供读写 API，直接调用飞书 OpenAPI（无需本地登录）
- 凭证来自环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET
"""
import os
import json
from flask import Flask, request, send_file, jsonify

HERE = os.path.dirname(os.path.abspath(__file__))
HTML_DIR = os.path.join(HERE, "pages")  # cloud/pages/ 仅含页面，不暴露源码
import cloud_sync as cs

app = Flask(__name__, static_folder=HTML_DIR, static_url_path="")


@app.route("/")
def index():
    return send_file(os.path.join(HTML_DIR, "总工作台.html"))


@app.route("/anchor")
def anchor():
    return send_file(os.path.join(HTML_DIR, "主播工作台.html"))


@app.route("/api/data")
def api_data():
    try:
        return jsonify(cs.get_all())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/total", methods=["POST"])
def api_total():
    body = request.get_json(force=True, silent=True) or {}
    try:
        if body.get("record_id"):
            return jsonify(cs.update_total(body["record_id"], body))
        return jsonify(cs.add_total(body))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/total/<record_id>", methods=["DELETE"])
def api_total_del(record_id):
    try:
        return jsonify(cs.delete_total(record_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/loan", methods=["POST"])
def api_loan():
    body = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(cs.add_loan(body))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mark_sold", methods=["POST"])
def api_mark_sold():
    body = request.get_json(force=True, silent=True) or {}
    try:
        return jsonify(cs.mark_sold(body.get("record_id")))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync", methods=["POST"])
def api_sync():
    try:
        return jsonify(cs.run_sync())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
