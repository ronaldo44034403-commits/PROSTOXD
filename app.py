"""
Сервер-диспетчер для Render (бесплатный Web Service).
Вы отправляете код через веб-страницу, телефон забирает его и выполняет.
Все данные хранятся в памяти (на бесплатном тарифе диска нет).
"""
import hmac
import os
import threading
import time
import uuid
from functools import wraps

from flask import Flask, jsonify, request, Response

TOKEN = os.environ.get("TOKEN", "")
if len(TOKEN) < 16:
    raise SystemExit("Задайте переменную окружения TOKEN (минимум 16 символов)")

app = Flask(__name__)
cond = threading.Condition()
tasks = {}          # id -> задача
order = []          # id по порядку создания
state = {"last_seen": 0.0}

MAX_TASKS = 50
MAX_CODE = 100_000
MAX_OUTPUT = 20_000


def auth(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        given = request.headers.get("X-Token", "").encode()
        if not hmac.compare_digest(given, TOKEN.encode()):
            return jsonify(error="unauthorized"), 401
        return fn(*a, **kw)
    return wrapper


@app.get("/health")
def health():
    return "ok"


@app.post("/api/task")
@auth
def create_task():
    data = request.get_json(silent=True) or {}
    kind = data.get("kind", "python")
    code = data.get("code", "")
    if kind not in ("python", "shell"):
        return jsonify(error="kind должен быть python или shell"), 400
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE:
        return jsonify(error="пустой или слишком длинный код"), 400
    try:
        timeout = min(max(int(data.get("timeout", 60)), 1), 300)
    except (TypeError, ValueError):
        timeout = 60

    tid = uuid.uuid4().hex[:8]
    with cond:
        tasks[tid] = {
            "id": tid, "kind": kind, "code": code, "timeout": timeout,
            "status": "pending", "output": None, "exit_code": None,
            "created": time.time(),
        }
        order.append(tid)
        while len(order) > MAX_TASKS:
            tasks.pop(order.pop(0), None)
        cond.notify_all()
    return jsonify(id=tid)


@app.get("/api/poll")
@auth
def poll():
    """Телефон спрашивает: есть ли задача? Сервер держит запрос до 25 секунд."""
    try:
        wait = min(max(int(request.args.get("wait", 0)), 0), 25)
    except ValueError:
        wait = 0
    deadline = time.time() + wait
    with cond:
        while True:
            state["last_seen"] = time.time()
            for tid in order:
                t = tasks.get(tid)
                if t and t["status"] == "pending":
                    t["status"] = "running"
                    return jsonify(task={
                        "id": t["id"], "kind": t["kind"],
                        "code": t["code"], "timeout": t["timeout"],
                    })
            remaining = deadline - time.time()
            if remaining <= 0:
                return jsonify(task=None)
            cond.wait(remaining)


@app.post("/api/result/<tid>")
@auth
def result(tid):
    data = request.get_json(silent=True) or {}
    with cond:
        t = tasks.get(tid)
        if not t:
            return jsonify(error="нет такой задачи"), 404
        t["output"] = str(data.get("output", ""))[:MAX_OUTPUT]
        t["exit_code"] = data.get("exit_code")
        t["status"] = "done"
    return jsonify(ok=True)


@app.get("/api/tasks")
@auth
def list_tasks():
    with cond:
        items = [
            {k: tasks[i][k] for k in ("id", "kind", "status", "exit_code", "output")}
            for i in reversed(order) if i in tasks
        ]
    return jsonify(tasks=items[:20])


@app.get("/api/status")
@auth
def status():
    online = time.time() - state["last_seen"] < 90
    return jsonify(phone_online=online)


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Phone VPS</title>
<style>
body{font-family:sans-serif;max-width:700px;margin:0 auto;padding:12px;background:#111;color:#eee}
input,select,textarea,button{width:100%;box-sizing:border-box;margin:4px 0;padding:10px;font-size:16px;
background:#222;color:#eee;border:1px solid #444;border-radius:8px}
textarea{height:160px;font-family:monospace}
button{background:#2563eb;border:0}
pre{background:#1c1c1c;padding:8px;border-radius:8px;white-space:pre-wrap;word-break:break-word}
</style></head><body>
<h3>Phone VPS <small id="st"></small></h3>
<input id="tok" type="password" placeholder="Токен">
<select id="kind"><option value="python">Python</option><option value="shell">Shell</option></select>
<textarea id="code" placeholder="print('привет с телефона')"></textarea>
<button onclick="run()">Выполнить на телефоне</button>
<div id="list"></div>
<script>
const $=id=>document.getElementById(id);
$('tok').value=sessionStorage.getItem('tok')||'';
async function api(path,opt={}){
  const r=await fetch(path,{...opt,headers:{'X-Token':$('tok').value,'Content-Type':'application/json'}});
  if(!r.ok) throw new Error(r.status);
  return r.json();
}
async function run(){
  sessionStorage.setItem('tok',$('tok').value);
  await api('/api/task',{method:'POST',body:JSON.stringify({kind:$('kind').value,code:$('code').value})});
  refresh();
}
async function refresh(){
  if(!$('tok').value) return;
  try{
    const s=await api('/api/status');
    $('st').textContent=s.phone_online?'📱 онлайн':'📴 офлайн';
    const l=await api('/api/tasks'); const box=$('list'); box.textContent='';
    for(const t of l.tasks){
      const d=document.createElement('pre');
      d.textContent='#'+t.id+' ['+t.status+']'+(t.exit_code!=null?' код '+t.exit_code:'')+'\\n'+(t.output||'');
      box.appendChild(d);
    }
  }catch(e){$('st').textContent='ошибка (проверьте токен)';}
}
setInterval(refresh,3000); refresh();
</script></body></html>"""


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
