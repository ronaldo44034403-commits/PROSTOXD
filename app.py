import asyncio, hashlib, html, io, json, os, re, resource, shutil, signal, sys, time, uuid, zipfile
from pathlib import Path, PurePosixPath

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import CommandStart, BaseFilter
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

# ВСТАВЛЕН ВАШ ТОКЕН (настоятельно рекомендую поменять его в @BotFather)
TOKEN = "9241683084:AAHD7FVqTT0XyiVtLxDLQjaGC9jsozVELLg"
ROOT = Path(os.environ.get("DATA_DIR", "data")).resolve()
MEM = int(os.environ.get("MEM_MB", "512")) * 1024 * 1024
MAX_FILE, MAX_UNZIP, MAX_FILES, PER_PAGE = 20 << 20, 80 << 20, 2000, 8
PID = re.compile(r"[0-9a-f]{8}")

# ================= ФИЛЬТР ПОДПИСКИ НА КАНАЛ =================
CHANNEL_ID = "@BLACK_AI_soft"
CHANNEL_URL = "https://t.me/BLACK_AI_soft"

class IsSubscribed(BaseFilter):
    async def __call__(self, event: Message | CallbackQuery, bot: Bot) -> bool:
        user_id = event.from_user.id
        try:
            # Проверяем, есть ли пользователь в канале
            member = await bot.get_chat_member(chat_id=CHANNEL_ID, user_id=user_id)
            if member.status in ("member", "administrator", "creator"):
                return True
        except Exception:
            pass # Если бот не админ или другая ошибка - считаем, что не подписан

        # Если не подписан - отправляем сообщение с кнопкой
        text = "🛑 Для использования бота подпишитесь на канал!"
        kb_sub = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📢 Подписаться", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")]
        ])
        
        if isinstance(event, Message):
            await event.answer(text, reply_markup=kb_sub)
        else:
            await event.answer("❌ Сначала подпишитесь на канал!", show_alert=True)
        return False
# ============================================================

r, deny = Router(), Router()
# Применяем проверку подписки ко всем сообщениям и кнопкам
r.message.filter(IsSubscribed())
r.callback_query.filter(IsSubscribed())

runs, groups, jobs = {}, {}, set()

class Run:
    def __init__(self, log):
        self.text, self.proc, self.msg = "", None, None
        self.done = self.stopped = False
        self.code = None
        self.t0 = time.time()
        self.log = open(log, "w", encoding="utf-8")

    def add(self, s):
        self.text = (self.text + s)[-30000:]
        self.log.write(s)
        self.log.flush()

def esc(s):
    return html.escape(s, quote=False)

def kb(*rows):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])

MENU = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Проекты"), KeyboardButton(text="Загрузить")],
                                     [KeyboardButton(text="Помощь")]], resize_keyboard=True)

def pdir(uid, pid):
    return ROOT / str(uid) / pid

def meta(uid, pid):
    return json.loads((pdir(uid, pid) / "meta.json").read_text(encoding="utf-8"))

def listing(uid):
    out = [(p.parent.name, json.loads(p.read_text(encoding="utf-8"))) for p in (ROOT / str(uid)).glob("*/meta.json")]
    return sorted(out, key=lambda x: x[1]["t"])

def kill(run):
    run.stopped = True
    if run.proc and run.proc.returncode is None:
        try:
            os.killpg(run.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

def unzip(raw):
    out, total = {}, 0
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for i in z.infolist():
            n = i.filename.replace("\\", "/")
            if i.is_dir() or n.startswith("__MACOSX/") or PurePosixPath(n).name.startswith("._"):
                continue
            p = PurePosixPath(n)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("в архиве небезопасный путь")
            with z.open(i) as f:
                data = f.read(MAX_UNZIP - total + 1)
            total += len(data)
            if total > MAX_UNZIP or len(out) >= MAX_FILES:
                raise ValueError("архив слишком большой")
            out[n] = data
    return out

def find_entry(files):
    if len(files) == 1 and next(iter(files)).endswith(".py"):
        return next(iter(files))
    mains = sorted((n for n in files if PurePosixPath(n).name.lower() == "main.py"), key=lambda n: (n.count("/"), n))
    if not mains:
        raise ValueError("в проекте нет main.py")
    return mains[0]

def store(uid, name, files):
    entry = find_entry(files)
    pid = next((p for p, m in listing(uid) if m["name"] == name), None) or uuid.uuid4().hex[:8]
    if (uid, pid) in runs:
        raise ValueError("проект сейчас запущен, сначала остановите его")
    pd = pdir(uid, pid)
    shutil.rmtree(pd / "code", ignore_errors=True)
    for n, b in files.items():
        p = pd / "code" / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b)
    (pd / "meta.json").write_text(json.dumps({"name": name, "entry": entry, "t": time.time()}), encoding="utf-8")
    return pid

def render(run, m):
    if not run.done:
        st = "● работает"
    elif run.stopped:
        st = f"■ остановлен ({int(time.time() - run.t0)} с)"
    else:
        st = f"○ завершён, код {run.code} ({int(time.time() - run.t0)} с)"
    n = 3000
    while n > 0:
        tail = esc(run.text[-n:])
        if len(tail) <= 3300:
            break
        n = n * 2 // 3
    else:
        tail = esc(run.text[-500:])
    return f"<b>BLACK VPS</b>  <code>{esc(m['name'])}</code>\n{st}\n\n<pre>{tail or '...'}</pre>"

def run_kb(pid, done):
    if done:
        return kb([("▶ Заново", f"r:{pid}"), ("Лог", f"g:{pid}")], [("‹ Проекты", "l:0")])
    return kb([("■ Стоп", f"s:{pid}"), ("↻ Перезапуск", f"rr:{pid}")])

async def pump(run, proc):
    while True:
        data = await proc.stdout.read(2048)
        if not data:
            break
        run.add(data.decode(errors="replace"))
    return await proc.wait()

def limits():
    if MEM:
        resource.setrlimit(resource.RLIMIT_AS, (MEM, MEM))

async def install(run, pd, base):
    rf = base / "requirements.txt"
    if not rf.is_file():
        return ""
    lines = [l.split(" #")[0].strip() for l in rf.read_text(encoding="utf-8-sig", errors="replace").splitlines()]
    reqs = [l for l in lines if l and not l.startswith(("#", "-"))]
    if not reqs:
        return ""
    libs = pd / "libs" / hashlib.sha1("\n".join(sorted(reqs)).encode()).hexdigest()[:12]
    if not (libs / ".ok").exists():
        run.add("$ pip install " + " ".join(reqs) + "\n")
        shutil.rmtree(libs, ignore_errors=True)
        libs.mkdir(parents=True)
        run.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--no-input",
            "--target", str(libs), *reqs, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={"PATH": os.environ.get("PATH", ""), "HOME": str(pd)}, start_new_session=True)
        if await pump(run, run.proc):
            shutil.rmtree(libs, ignore_errors=True)
            raise RuntimeError("зависимости не установились")
        (libs / ".ok").touch()
    return str(libs)

async def execute(bot, chat, uid, pid, run):
    m, pd = meta(uid, pid), pdir(uid, pid)
    entry = pd / "code" / m["entry"]
    ticker = asyncio.create_task(tick(bot, chat, uid, pid, run, m))
    try:
        libs = await install(run, pd, entry.parent)
        if not run.stopped:
            home = pd / "home"
            home.mkdir(exist_ok=True)
            run.proc = await asyncio.create_subprocess_exec(
                sys.executable, "-u", entry.name, cwd=entry.parent, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                preexec_fn=limits, env={"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": libs,
                                        "PYTHONUNBUFFERED": "1", "LANG": "C.UTF-8"})
            run.code = await pump(run, run.proc)
    except Exception as e:
        run.add(f"\n{e}\n")
        run.code = -1
    run.done = True
    run.log.close()
    await ticker
    runs.pop((uid, pid), None)

async def tick(bot, chat, uid, pid, run, m):
    last = None
    while True:
        await asyncio.sleep(2)
        fin = run.done
        txt = render(run, m)
        if txt != last:
            try:
                await bot.edit_message_text(txt, chat_id=chat, message_id=run.msg, reply_markup=run_kb(pid, fin))
                last = txt
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
                continue
            except TelegramAPIError:
                last = txt
        if fin:
            break

async def launch(bot, chat, uid, pid):
    if (uid, pid) in runs:
        return False
    run = Run(pdir(uid, pid) / "run.log")
    runs[(uid, pid)] = run
    sent = await bot.send_message(chat, render(run, meta(uid, pid)), reply_markup=run_kb(pid, False))
    run.msg = sent.message_id
    t = asyncio.create_task(execute(bot, chat, uid, pid, run))
    jobs.add(t)
    t.add_done_callback(jobs.discard)
    return True

async def put(target, text, markup=None):
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramAPIError:
            pass
    else:
        await target.answer(text, reply_markup=markup)

async def show_list(target, uid, page=0):
    items = listing(uid)
    if not items:
        return await put(target, "<b>BLACK VPS</b>\nПроектов пока нет. Пришлите .py или .zip.")
    pages = (len(items) - 1) // PER_PAGE + 1
    page = min(max(page, 0), pages - 1)
    rows = [[(("● " if (uid, p) in runs else "○ ") + m["name"], f"p:{p}")] for p, m in items[page * PER_PAGE:(page + 1) * PER_PAGE]]
    if pages > 1:
        rows.append([("‹", f"l:{page - 1}"), (f"{page + 1}/{pages}", f"l:{page}"), ("›", f"l:{page + 1}")])
    await put(target, f"<b>BLACK VPS</b>\nПроектов: {len(items)}", kb(*rows))

async def show_card(target, uid, pid):
    m = meta(uid, pid)
    code = pdir(uid, pid) / "code"
    files = [f for f in code.rglob("*") if f.is_file()]
    size = sum(f.stat().st_size for f in files) // 1024
    on = (uid, pid) in runs
    text = (f"<b>{esc(m['name'])}</b>\nзапуск: <code>{esc(m['entry'])}</code>\n"
            f"файлов: {len(files)}, {size} КБ\nстатус: {'работает' if on else 'остановлен'}")
    first = ("■ Стоп", f"s:{pid}") if on else ("▶ Запустить", f"r:{pid}")
    await put(target, text, kb([first, ("Лог", f"g:{pid}")], [("Скачать", f"z:{pid}"), ("Удалить", f"d:{pid}")],
                               [("‹ Проекты", "l:0")]))

@r.message(CommandStart())
@r.message(F.text == "Помощь")
async def start(m: Message):
    await m.answer("<b>BLACK VPS</b>\nПришлите .py или .zip, из архива запускается main.py. "
                   "Несколько .py одним альбомом собираются в один проект.\n"
                   "Если рядом лежит requirements.txt, пакеты поставятся сами. "
                   "Пока проект работает, любой ваш текст уходит ему в stdin.", reply_markup=MENU)

@r.message(F.text == "Загрузить")
async def upload_hint(m: Message):
    await m.answer("Отправьте файл .py или архив .zip (до 20 МБ).")

@r.message(F.text == "Проекты")
async def projects(m: Message):
    await show_list(m, m.from_user.id)

@r.message(F.text)
async def stdin(m: Message):
    mine = [(k, v) for k, v in runs.items() if k[0] == m.from_user.id and v.proc and v.proc.stdin and not v.done]
    if not mine:
        return await m.answer("Нет запущенных проектов. Откройте «Проекты».")
    run = max(mine, key=lambda x: x[1].t0)[1]
    try:
        run.proc.stdin.write((m.text + "\n").encode())
        await run.proc.stdin.drain()
        run.add("> " + m.text + "\n")
    except (BrokenPipeError, ConnectionResetError):
        await m.answer("Процесс уже не принимает ввод.")
    try:
        await m.delete()
    except TelegramAPIError:
        pass

async def reply_created(m, name, pid):
    await m.answer(f"Загружено: <b>{esc(name)}</b>", reply_markup=kb([("▶ Запустить", f"r:{pid}"), ("Открыть", f"p:{pid}")]))

async def flush_group(m, gid):
    await asyncio.sleep(3)
    files = groups.pop(gid)
    uid = m.from_user.id
    try:
        if any(n.lower() == "main.py" for n, _, _ in files):
            name = next((c for _, _, c in files if c), None) or "набор " + time.strftime("%H%M%S")
            pid = store(uid, name[:40], {n: b for n, b, _ in files})
            await reply_created(m, name[:40], pid)
        else:
            for n, b, _ in files:
                await reply_created(m, n, store(uid, n, {n: b}))
    except ValueError as e:
        await m.answer(f"Ошибка: {esc(str(e))}")

@r.message(F.document)
async def document(m: Message):
    d = m.document
    name = Path(d.file_name or "file").name
    ext = Path(name).suffix.lower()
    if ext not in (".py", ".zip"):
        return await m.answer("Принимаю только .py и .zip")
    if d.file_size and d.file_size > MAX_FILE:
        return await m.answer("Файл больше 20 МБ, Telegram не отдаёт такие боту.")
    buf = io.BytesIO()
    await m.bot.download(d, destination=buf)
    raw = buf.getvalue()
    if m.media_group_id and ext == ".py":
        first = m.media_group_id not in groups
        groups.setdefault(m.media_group_id, []).append((name, raw, m.caption))
        if first:
            await flush_group(m, m.media_group_id)
        return
    try:
        if ext == ".zip":
            name, files = name[:-4], await asyncio.to_thread(unzip, raw)
        else:
            files = {name: raw}
        await reply_created(m, name, store(m.from_user.id, name[:40], files))
    except (ValueError, zipfile.BadZipFile, RuntimeError) as e:
        await m.answer(f"Ошибка: {esc(str(e))}")

@r.callback_query()
async def buttons(c: CallbackQuery):
    uid = c.from_user.id
    act, _, arg = c.data.partition(":")
    
    if act == "check_sub":
        await c.message.delete()
        await c.message.answer("✅ Подписка подтверждена! Отправьте /start для начала работы.")
        return await c.answer()

    if act == "l":
        await show_list(c, uid, int(arg))
        return await c.answer()
    if not PID.fullmatch(arg) or not (pdir(uid, arg) / "meta.json").is_file():
        return await c.answer("Проект не найден", show_alert=True)
    key = (uid, arg)
    if act == "p":
        await show_card(c, uid, arg)
    elif act == "r":
        if not await launch(c.bot, c.message.chat.id, uid, arg):
            return await c.answer("Уже запущен", show_alert=True)
    elif act == "rr":
        if key in runs:
            kill(runs[key])
            for _ in range(50):
                if key not in runs:
                    break
                await asyncio.sleep(0.1)
        await launch(c.bot, c.message.chat.id, uid, arg)
    elif act == "s":
        if key in runs:
            kill(runs[key])
    elif act == "g":
        log = pdir(uid, arg) / "run.log"
        if not log.is_file() or not log.stat().st_size:
            return await c.answer("Лог пуст", show_alert=True)
        await c.message.answer_document(BufferedInputFile(log.read_bytes(), filename=f"{meta(uid, arg)['name']}.log"))
    elif act == "z":
        out = io.BytesIO()
        code = pdir(uid, arg) / "code"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for f in code.rglob("*"):
                if f.is_file():
                    z.write(f, f.relative_to(code))
        await c.message.answer_document(BufferedInputFile(out.getvalue(), filename=f"{meta(uid, arg)['name']}.zip"))
    elif act == "d":
        await put(c, f"Удалить <b>{esc(meta(uid, arg)['name'])}</b>?", kb([("Да, удалить", f"x:{arg}"), ("Отмена", f"p:{arg}")]))
    elif act == "x":
        if key in runs:
            kill(runs[key])
        shutil.rmtree(pdir(uid, arg), ignore_errors=True)
        await show_list(c, uid)
    await c.answer()

async def main():
    ROOT.mkdir(parents=True, exist_ok=True)
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_routers(r) # deny больше не нужен, фильтр сам отсеет лишних
    try:
        await dp.start_polling(bot)
    finally:
        for run in runs.values():
            kill(run)

if __name__ == "__main__":
    asyncio.run(main())