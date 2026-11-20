"""Phone VPS -> Render VPS: код выполняется прямо на Render (ресурсы Render, не телефона)."""
import hmac, os, subprocess, sys, threading, time, uuid
from functools import wraps
from flask import Flask, jsonify, request, Response

TOKEN = os.environ.get("TOKEN", "")
if len(TOKEN) < 16:
    raise SystemExit("Задайте переменную окружения TOKEN (минимум 16 символов)")

app = Flask(__name__)
lock = threading.Lock()
tasks, order, procs = {}, [], {}
MAX_TASKS, MAX_CODE, MAX_OUT = 30, 100_000, 20_000
LIBS = "/tmp/libs"
os.makedirs(LIBS, exist_ok=True)


def auth(fn):
    @wraps(fn)
    def w(*a, **k):
        if not hmac.compare_digest(request.headers.get("X-Token", "").encode(), TOKEN.encode()):
            return jsonify(error="unauthorized"), 401
        return fn(*a, **k)
    return w


def worker(t):
    tid = t["id"]
    try:
        reqs = t["requirements"].split()
        if reqs:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", LIBS] + reqs,
                               capture_output=True, text=True, timeout=600)
            if r.returncode:
                t["output"], t["exit_code"], t["status"] = ("pip:\n" + r.stdout + r.stderr)[:MAX_OUT], r.returncode, "done"
                return
        env = dict(os.environ, PYTHONPATH=LIBS, PYTHONUNBUFFERED="1")
        env.pop("TOKEN", None)  # код пользователя не видит токен сервера
        if t["kind"] == "shell":
            cmd, shell = t["code"], True
        else:
            path = f"/tmp/{tid}.py"
            open(path, "w", encoding="utf-8").write(t["code"])
            cmd, shell = [sys.executable, path], False
        p = subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", env=env, cwd="/tmp")
        procs[tid] = p
        t["status"] = "running"
        limit = t["timeout"]
        if limit:
            threading.Timer(limit, lambda: p.poll() is None and (p.kill(), t.update(note="таймаут"))).start()
        for line in p.stdout:
            t["output"] = (t["output"] + line)[-MAX_OUT:]
        p.wait()
        t["exit_code"] = p.returncode
        if t.get("note"):
            t["output"] += f"\n[{t['note']}]"
    except Exception as e:
        t["output"], t["exit_code"] = f"Ошибка: {e}", 1
    finally:
        t["status"] = "done"
        procs.pop(tid, None)


@app.get("/health")
def health():
    return "ok"


@app.post("/api/task")
@auth
def create_task():
    d = request.get_json(silent=True) or {}
    kind, code = d.get("kind", "python"), d.get("code", "")
    if kind not in ("python", "shell", "service"):
        return jsonify(error="bad kind"), 400
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE:
        return jsonify(error="bad code"), 400
    try:
        timeout = 0 if kind == "service" else min(max(int(d.get("timeout", 60)), 1), 600)
    except (TypeError, ValueError):
        timeout = 60
    reqs = d.get("requirements", "")
    if not isinstance(reqs, str) or len(reqs) > 5000:
        return jsonify(error="bad requirements"), 400
    tid = uuid.uuid4().hex[:8]
    t = {"id": tid, "name": str(d.get("name", ""))[:60], "kind": kind,
         "svc": kind == "service", "code": code, "requirements": reqs, "timeout": timeout,
         "status": "pending", "output": "", "exit_code": None}
    with lock:
        tasks[tid] = t
        order.append(tid)
        while len(order) > MAX_TASKS:
            old = order.pop(0)
            p = procs.get(old)
            if p:
                p.kill()
            tasks.pop(old, None)
    threading.Thread(target=worker, args=(t,), daemon=True).start()
    return jsonify(id=tid)


@app.post("/api/stop/<tid>")
@auth
def stop(tid):
    p = procs.get(tid)
    if p and p.poll() is None:
        p.kill()
    return jsonify(ok=True)


@app.get("/api/tasks")
@auth
def list_tasks():
    keys = ("id", "name", "kind", "status", "exit_code", "output")
    return jsonify(tasks=[{k: tasks[i][k] for k in keys} for i in reversed(order) if i in tasks][:20])


@app.get("/api/status")
@auth
def status():
    return jsonify(phone_online=True)


PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8").read()


@app.get("/manifest.json")
def manifest():
    return jsonify(name="Phone VPS", short_name="PhoneVPS", start_url="/", display="standalone",
                   background_color="#0b1020", theme_color="#0b1020", icons=[])


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
