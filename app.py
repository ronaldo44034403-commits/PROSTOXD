import base64, binascii, hashlib, io, os, re, resource, shutil, signal, sqlite3, subprocess, sys, threading, time, uuid, zipfile
from functools import wraps
from flask import Flask, jsonify, request, session, Response
from werkzeug.security import generate_password_hash, check_password_hash

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "CHANGE_ME@gmail.com").lower()
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "NfIb1lh10G606zTu")
SECRET_KEY = os.environ.get("SECRET_KEY", "96a6a7be6bf538f2bfb00998fa8674461f7d2dc609b6feca612e1c82b39615b6")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")  # пусто: вход через Google выключен

app = Flask(__name__)
app.config.update(MAX_CONTENT_LENGTH=9 * 1024 * 1024, SECRET_KEY=SECRET_KEY, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=True,
                  PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)
lock = threading.Lock()
db = sqlite3.connect("/tmp/vps.db", check_same_thread=False)
db.execute("create table if not exists users(id integer primary key, email text unique, pw text, approved int, admin int)")
tasks, order, procs, fails = {}, [], {}, {}
MAX_TASKS, MAX_CODE, MAX_OUT, MEM = 60, 100_000, 20_000, 400 * 1024 * 1024
MAX_ZIP, MAX_UNZIP, MAX_FILES = 5 * 1024 * 1024, 30 * 1024 * 1024, 500
inst_locks = {}
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


def unpack(data, dest):
    """Распаковывает zip в dest. Возвращает (папка с main.py, имя main.py, число остальных .py)."""
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ValueError("Это не ZIP-архив или он повреждён")
    root = os.path.realpath(dest)
    os.makedirs(root, exist_ok=True)
    total = 0
    with z:
        infos = [i for i in z.infolist() if not i.is_dir() and not i.filename.startswith("__MACOSX/")
                 and not os.path.basename(i.filename).startswith("._")]
        if len(infos) > MAX_FILES:
            raise ValueError(f"В архиве слишком много файлов (максимум {MAX_FILES})")
        for i in infos:
            if (i.external_attr >> 16) & 0o170000 == 0o120000:  # симлинки пропускаем
                continue
            target = os.path.realpath(os.path.join(root, i.filename.replace("\\", "/")))
            if not target.startswith(root + os.sep):
                raise ValueError("Небезопасный путь в архиве: " + i.filename)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(i) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UNZIP:
                        raise ValueError("Архив слишком большой после распаковки (максимум 30 МБ)")
                    out.write(chunk)
    pys, mains = 0, []
    for dp, _, fs in os.walk(root):
        for f in fs:
            if f.endswith(".py"):
                pys += 1
            if f.lower() == "main.py":
                mains.append(os.path.join(dp, f))
    if not mains:
        raise ValueError("В архиве нет main.py (запускается только он). Положите main.py в корень архива.")
    mains.sort(key=lambda p: (p.count(os.sep), p))
    return os.path.dirname(mains[0]), os.path.basename(mains[0]), pys - 1


def read_reqs(path):
    """Читает requirements.txt: (список пакетов, пропущенные строки с параметрами -...)."""
    ok, skipped = [], []
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        for line in f:
            line = line.split(" #")[0].strip()
            if line and not line.startswith("#"):
                (skipped if line.startswith("-") else ok).append(line)
    return ok, skipped


def worker(t, data=None):
    tid, uid = t["id"], t["uid"]
    home = f"/tmp/u{uid}"
    base = f"{home}/p{tid}"
    try:
        os.makedirs(home, exist_ok=True)
        cwd, script, reqs = home, None, []
        manual = t["requirements"].split()
        if any(x.startswith("-") for x in manual):
            t["output"], t["exit_code"] = "pip: параметры (-...) запрещены", 1
            return
        if data is not None:
            t["status"] = "installing"
            t["output"] = "📂 Распаковываю архив...\n"
            cwd, main, others = unpack(data, base)
            data = None
            script = os.path.join(cwd, main)
            t["output"] += f"✅ Запускаю {main}, остальные .py ({others}) не трогаю\n"
            for d in (cwd, base):
                rf = os.path.join(d, "requirements.txt")
                if os.path.isfile(rf):
                    found, skipped = read_reqs(rf)
                    reqs += found
                    t["output"] += f"📄 Нашёл requirements.txt: {len(found)} пакетов\n"
                    if skipped:
                        t["output"] += "⚠️ Пропустил строки с параметрами (-...): " + ", ".join(skipped) + "\n"
                    break
        for r in manual:
            if r not in reqs:
                reqs.append(r)
        libs = ""
        if reqs:
            t["status"] = "installing"
            libs = f"/tmp/libs/{uid}/" + hashlib.sha1("\n".join(sorted(reqs)).encode()).hexdigest()[:12]
            with inst_locks.setdefault(libs, threading.Lock()):
                if os.path.exists(libs + "/.ok"):
                    t["output"] += "📦 Библиотеки уже установлены\n"
                else:
                    t["output"] += "📦 Устанавливаю: " + ", ".join(reqs) + "\n"
                    shutil.rmtree(libs, ignore_errors=True)
                    os.makedirs(libs)
                    p = subprocess.Popen([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                                          "--no-input", "--target", libs] + reqs,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         errors="replace", start_new_session=True)
                    procs[tid] = p
                    try:
                        out, _ = p.communicate(timeout=600)
                    except subprocess.TimeoutExpired:
                        kill(p)
                        out = p.communicate()[0] + "\n[таймаут установки]"
                    procs.pop(tid, None)
                    if p.returncode:
                        shutil.rmtree(libs, ignore_errors=True)
                        t["output"] = (t["output"] + "❌ pip:\n" + out)[-MAX_OUT:]
                        t["exit_code"] = p.returncode
                        return
                    open(libs + "/.ok", "w").close()
                    t["output"] += "✅ Библиотеки установлены\n"
        env = {"PATH": os.environ.get("PATH", ""), "HOME": home, "PYTHONPATH": libs,
               "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8"}
        if script:
            cmd, shell = [sys.executable, script], False
        elif t["kind"] == "shell":
            cmd, shell = t["code"], True
        else:
            path = f"{home}/{tid}.py"
            open(path, "w", encoding="utf-8").write(t["code"])
            cmd, shell = [sys.executable, path], False
        p = subprocess.Popen(cmd, shell=shell, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", env=env, cwd=cwd,
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
    except ValueError as e:
        t["output"], t["exit_code"] = f"❌ {e}", 1
    except Exception as e:
        t["output"], t["exit_code"] = f"Ошибка: {e}", 1
    finally:
        t["status"] = "done"
        procs.pop(tid, None)
        shutil.rmtree(base, ignore_errors=True)


@app.post("/api/task")
@login_required()
def create_task(u):
    d = request.get_json(silent=True) or {}
    kind, code = d.get("kind", "python"), d.get("code", "")
    if kind not in ("python", "shell", "service"):
        return jsonify(error="bad kind"), 400
    zb, data = d.get("zip"), None
    if zb:
        if kind == "shell":
            return jsonify(error="ZIP нельзя запускать как Shell, выберите Python или Сервис"), 400
        if not isinstance(zb, str) or len(zb) > MAX_ZIP * 4 // 3 + 16:
            return jsonify(error="ZIP слишком большой (максимум 5 МБ)"), 400
        try:
            data = base64.b64decode(zb, validate=True)
        except (binascii.Error, ValueError):
            return jsonify(error="Не удалось прочитать ZIP"), 400
        code = ""
    elif not isinstance(code, str) or not code.strip() or len(code) > MAX_CODE:
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
    threading.Thread(target=worker, args=(t, data), daemon=True).start()
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


@app.errorhandler(413)
def too_big(e):
    return jsonify(error="Файл слишком большой (ZIP до 5 МБ)"), 413


@app.get("/health")
def health():
    return "ok"


PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"), encoding="utf-8").read()


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
