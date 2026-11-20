"""
Клиент для телефона (Termux). Только стандартная библиотека, pip не нужен.

Запуск:
    export SERVER_URL=https://ваш-сервис.onrender.com
    export TOKEN=ваш_токен
    python phone_client.py

Настройки экономии батареи (переменные окружения):
    WAIT        сколько секунд сервер держит запрос (макс. 25, по умолчанию 25)
    IDLE_SLEEP  пауза после пустого ответа, секунд (по умолчанию 0).
                Например IDLE_SLEEP=30 и WAIT=0: телефон спрашивает раз в 30 секунд,
                батарея тратится меньше, задача стартует с задержкой до 30 секунд.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

SERVER = os.environ.get("SERVER_URL", "").rstrip("/")
TOKEN = os.environ.get("TOKEN", "")
WAIT = min(int(os.environ.get("WAIT", "25")), 25)
IDLE_SLEEP = int(os.environ.get("IDLE_SLEEP", "0"))
MAX_OUTPUT = 20_000

if not SERVER or not TOKEN:
    sys.exit("Задайте SERVER_URL и TOKEN")


def call(method, path, data=None, timeout=40):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        SERVER + path, data=body, method=method,
        headers={"X-Token": TOKEN, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def run_task(task):
    kind, code, limit = task["kind"], task["code"], task["timeout"]
    tmp = None
    try:
        if kind == "python":
            with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
                f.write(code)
                tmp = f.name
            cmd, shell = [sys.executable, tmp], False
        else:
            cmd, shell = code, True
        p = subprocess.run(cmd, shell=shell, capture_output=True, text=True,
                           errors="replace", timeout=limit)
        return (p.stdout + p.stderr)[:MAX_OUTPUT], p.returncode
    except subprocess.TimeoutExpired:
        return f"Превышено время выполнения ({limit} с)", 124
    except Exception as e:
        return f"Ошибка запуска: {e}", 1
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


def main():
    print("Клиент запущен. Жду задачи...")
    fails = 0
    while True:
        try:
            resp = call("GET", f"/api/poll?wait={WAIT}", timeout=WAIT + 20)
            fails = 0
            task = resp.get("task")
            if not task:
                if IDLE_SLEEP:
                    time.sleep(IDLE_SLEEP)
                continue
            print(f"Задача {task['id']} ({task['kind']})")
            output, code = run_task(task)
            call("POST", f"/api/result/{task['id']}", {"output": output, "exit_code": code})
            print(f"Готово, код {code}")
        except KeyboardInterrupt:
            print("Остановлено")
            return
        except Exception as e:
            fails += 1
            delay = min(60, 2 ** min(fails, 6))
            print(f"Ошибка связи ({e}), повтор через {delay} с")
            time.sleep(delay)


if __name__ == "__main__":
    main()
