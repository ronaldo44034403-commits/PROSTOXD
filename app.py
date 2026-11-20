import os, re, resource, signal, sqlite3, subprocess, sys, threading, time, uuid
from functools import wraps
from flask import Flask, jsonify, request, session, Response
from werkzeug.security import generate_password_hash, check_password_hash

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "CHANGE_ME@gmail.com").lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "NfIb1lh10G606zTu")
SECRET_KEY = os.environ.get("SECRET_KEY", "96a6a7be6bf538f2bfb00998fa8674461f7d2dc609b6feca612e1c82b39615b6")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")  # пусто: вход через Google выключен

app = Flask(__name__)
app.config.update(SECRET_KEY=SECRET_KEY, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True,
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)
lock = threading.Lock()
db = sqlite3.connect("/tmp/vps.db", check_same_thread=False)
db.execute("create table if not exists users(id integer primary key, email text unique, pw text, approved int, admin int)")
tasks, order, procs, fails = {}, [], {}, {}
MAX_TASKS, MAX_CODE, MAX_OUT, MEM = 60, 100_000, 20_000, 400 * 1024 * 1024
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")


def q(sql, *a):
    with lock:
        cur = db.execute(sql, a)
        db.commit()
        return cur


def init_admin():
    h = generate_password_hash(ADMIN_PASSWORD)
    if q("select 1 from users where email=?", ADMIN_EMAIL).fetchone():
        q("update users set pw=?, approved=1, admin=1 where email=?", h, ADMIN_EMAIL)
    else:
        q("insert into users(email,pw,approved,admin) values(?,?,1,1)", ADMIN_EMAIL, h)


init_admin()


def me():
    uid = session.get("uid")
    if not uid:
        return None
    r = q("select id,email,approved,admin from users where id=?", uid).fetchone()
    return dict(id=r[0], email=r[1], approved=bool(r[2]), admin=bool(r[3])) if r and r[2] else None


def login_required(admin=False):
    def deco(fn):
        @wraps(fn)
        def w(*a, **k):
            u = me()
            if not u or (admin and not u["admin"]):
                return jsonify(error="unauthorized"), 401
            return fn(u, *a, **k)
        return w
    return deco


def throttle(key):
    now = time.time()
    fails[key] = [t for t in fails.get(key, []) if now - t < 300]
    return len(fails[key]) >= 6


@app.get("/api/config")
def config():
    return jsonify(google=GOOGLE_CLIENT_ID)


@app.post("/api/register")
def register():
    d = request.get_json(silent=True) or {}
    email, pw = str(d.get("email", "")).strip().lower(), str(d.get("password", ""))
    if not EMAIL.match(email) or len(pw) < 8 or len(pw) > 200:
        return jsonify(error="Нужен email и пароль от 8 символов"), 400
    if email == ADMIN_EMAIL:
        return jsonify(error="Этот email занят"), 400
    try:
        q("insert into users(email,pw,approved,admin) values(?,?,0,0)", email, generate_password_hash(pw))
    except sqlite3.IntegrityError:
        return jsonify(error="Этот email уже зарегистрирован"), 400
    return jsonify(message="Аккаунт создан. Ждите одобрения администратора.")


@app.post("/api/login")
def login():
    d = request.get_json(silent=True) or {}
    email, pw = str(d.get("email", "")).strip().lower(), str(d.get("password", ""))
    key = request.remote_addr + email
    if throttle(key):
        return jsonify(error="Слишком много попыток, подождите 5 минут"), 429
    r = q("select id,pw,approved from users where email=?", email).fetchone()
    if not r or not check_password_hash(r[1], pw):
        fails.setdefault(key, []).append(time.time())
        return jsonify(error="Неверный email или пароль"), 401
    if not r[2]:
        return jsonify(error="Аккаунт ещё не одобрен администратором"), 403
    session.clear()
    session.permanent = True
    session["uid"] = r[0]
    return jsonify(ok=True)


@app.post("/api/google")
def google_login():
    if not GOOGLE_CLIENT_ID:
        return jsonify(error="Google-вход не настроен"), 400
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as grq
        info = id_token.verify_oauth2_token((request.get_json(silent=True) or {}).get("credential", ""),
                                            grq.Request(), GOOGLE_CLIENT_ID)
        if not info.get("email_verified"):
            raise ValueError("email not verified")
        email = info["email"].lower()
    except Exception:
        return jsonify(error="Google не подтвердил вход"), 401
    r = q("select id,approved from users where email=?", email).fetchone()
    if not r:
        q("insert into users(email,pw,approved,admin) values(?,?,?,0)",
          email, generate_password_hash(uuid.uuid4().hex), 0)
        r = q("select id,approved from users where email=?", email).fetchone()
    if not r[1]:
        return jsonify(error="Аккаунт создан, ждите одобрения администратора"), 403
    session.clear()
    session.permanent = True
    session["uid"] = r[0]
    return jsonify(ok=True)


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def whoami():
    u = me()
    return jsonify(email=u["email"], admin=u["admin"]) if u else (jsonify(error="no"), 401)


@app.get("/api/users")
@login_required(admin=True)
def users(u):
    rows = q("select id,email,approved,admin from users order by approved,id").fetchall()
    return jsonify(users=[dict(id=r[0], email=r[1], approved=bool(r[2]), admin=bool(r[3])) for r in rows])


@app.post("/api/users/<int:uid>/<act>")
@login_required(admin=True)
def user_act(u, uid, act):
    if act == "approve":
        q("update users set approved=1 where id=?", uid)
    elif act == "delete":
        q("delete from users where id=? and admin=0", uid)
        for t in list(tasks.values()):
            if t["uid"] == uid:
                kill(procs.get(t["id"]))
    else:
        return jsonify(error="bad action"), 400
    return jsonify(ok=True)


def kill(p):
    if p and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            p.kill()


def limits():
    resource.setrlimit(resource.RLIMIT_AS, (MEM, MEM))


def worker(t):
    tid, uid = t["id"], t["uid"]
    libs, home = f"/tmp/libs/{uid}", f"/tmp/u{uid}"
    os.makedirs(libs, exist_ok=True)
    os.makedirs(home, exist_ok=True)
    try:
        reqs = t["requirements"].split()
        if any(x.startswith("-") for x in reqs):
            t["output"], t["exit_code"] = "pip: параметры (-...) запрещены", 1
            return
        if reqs:
            r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--target", libs] + reqs,
                               capture_output=True, text=True, timeout=600)
            if r.returncode:
                t["output"], t["exit_code"] = ("pip:\n" + r.stdout + r.stderr)[-MAX_OUT:], r.returncode
                return
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "PYTHONPATH": libs,
               "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8"}
        if t["kind"] == "shell":
            cmd, shell = t["code"], True
        else:
            path = f"{home}/{tid}.py"
            open(path, "w", encoding="utf-8").write(t["code"])
            cmd, shell = [sys.executable, path], False
        p = subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", env=env, cwd=home,
                             start_new_session=True, preexec_fn=limits)
        procs[tid] = p
        t["status"] = "running"
        if t["timeout"]:
            def on_timeout():
                if p.poll() is None:
                    t["note"] = "таймаут"
                    kill(p)
            threading.Timer(t["timeout"], on_timeout).start()
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


@app.post("/api/task")
@login_required()
def create_task(u):
    d = request.get_json(silent=True) or {}
    kind, code = d.get("kind", "python"), d.get("code", "")
    if kind not in ("python", "shell", "service"):
        return jsonify(error="bad kind"), 400
    if not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE:
        return jsonify(error="bad code"), 400
    reqs = d.get("requirements", "")
    if not isinstance(reqs, str) or len(reqs) > 5000:
        return jsonify(error="bad requirements"), 400
    try:
        timeout = 0 if kind == "service" else min(max(int(d.get("timeout", 60)), 1), 600)
    except (TypeError, ValueError):
        timeout = 60
    with lock:
        active = sum(1 for t in tasks.values() if t["uid"] == u["id"] and t["status"] != "done")
        if active >= (10 if u["admin"] else 3):
            return jsonify(error="Слишком много активных задач, остановите лишние"), 429
        tid = uuid.uuid4().hex[:8]
        t = {"id": tid, "uid": u["id"], "name": str(d.get("name", ""))[:60], "kind": kind, "code": code,
             "requirements": reqs, "timeout": timeout, "status": "pending", "output": "", "exit_code": None}
        tasks[tid] = t
        order.append(tid)
        while len(order) > MAX_TASKS:
            old = order.pop(0)
            kill(procs.get(old))
            tasks.pop(old, None)
    threading.Thread(target=worker, args=(t,), daemon=True).start()
    return jsonify(id=tid)


@app.post("/api/stop/<tid>")
@login_required()
def stop(u, tid):
    t = tasks.get(tid)
    if t and (t["uid"] == u["id"] or u["admin"]):
        kill(procs.get(tid))
    return jsonify(ok=True)


@app.get("/api/tasks")
@login_required()
def list_tasks(u):
    keys = ("id", "name", "kind", "status", "exit_code", "output")
    mine = [tasks[i] for i in reversed(order) if i in tasks and tasks[i]["uid"] == u["id"]][:20]
    return jsonify(tasks=[{k: t[k] for k in keys} for t in mine])


@app.get("/health")
def health():
    return "ok"


PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8").read()


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
