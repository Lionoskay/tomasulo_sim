#!/usr/bin/env python3
"""
Flask 入口：API 路由 + 启动服务器。
"""

import os
import threading
import time
from flask import Flask, request, jsonify, render_template, send_from_directory
from tomasulo import TomasuloEngine

app = Flask(__name__)
engine = TomasuloEngine()

# test_programs 目录的绝对路径
TEST_PROGRAMS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_programs")

# 连续执行控制
_run_thread = None
_run_stop_flag = threading.Event()


@app.route("/")
def index():
    return render_template("index.html")


# ── 静态文件：test_programs ──

@app.route("/test_programs/<path:filename>")
def serve_test_program(filename):
    return send_from_directory(TEST_PROGRAMS_DIR, filename)


# ── GET /api/state ──

@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify(engine.to_snapshot())


# ── POST /api/step ──

@app.route("/api/step", methods=["POST"])
def api_step():
    data = request.get_json(silent=True) or {}
    cycles = max(1, int(data.get("cycles", 1)))
    for _ in range(cycles):
        if engine.done:
            break
        engine.step()
    return jsonify(engine.to_snapshot())


# ── POST /api/run ──

@app.route("/api/run", methods=["POST"])
def api_run():
    global _run_thread, _run_stop_flag
    _run_stop_flag.clear()
    engine.running = True
    if _run_thread is None or not _run_thread.is_alive():
        _run_thread = threading.Thread(target=_run_loop, daemon=True)
        _run_thread.start()
    return jsonify({"status": "started"})


def _run_loop():
    """后台连续执行直到完成或暂停。"""
    while engine.running and not engine.done:
        if _run_stop_flag.is_set():
            break
        engine.step()
        time.sleep(0.01)  # 小延迟避免 CPU 空转


# ── POST /api/pause ──

@app.route("/api/pause", methods=["POST"])
def api_pause():
    global _run_stop_flag
    _run_stop_flag.set()
    engine.running = False
    return jsonify({"status": "paused"})


# ── POST /api/reset ──

@app.route("/api/reset", methods=["POST"])
def api_reset():
    global _run_stop_flag
    _run_stop_flag.set()
    engine.reset()
    return jsonify(engine.to_snapshot())


# ── POST /api/load ──

@app.route("/api/load", methods=["POST"])
def api_load():
    data = request.get_json(silent=True) or {}
    code = data.get("code", "")
    latencies = data.get("latencies", None)
    if not code.strip():
        return jsonify({"error": "代码为空", "line": 0}), 400
    try:
        engine.load_program(code, latencies)
    except Exception as e:
        line_no = getattr(e, "line_no", 0)
        return jsonify({"error": str(e), "line": line_no}), 400
    return jsonify(engine.to_snapshot())


# ── POST /api/history ──

@app.route("/api/history", methods=["POST"])
def api_history():
    data = request.get_json(silent=True) or {}
    cycle = int(data.get("cycle", 0))
    snapshot = engine.get_history(cycle)
    if snapshot is None:
        return jsonify({"error": "无效的周期号"}), 404
    return jsonify(snapshot)


if __name__ == "__main__":
    app.run(debug=True, host="localhost", port=5050)
