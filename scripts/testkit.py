"""Пульт стенда: пройти сценарий панели снаружи и увидеть, чем он обернулся.

    python scripts/testkit.py health
    python scripts/testkit.py reset
    python scripts/testkit.py say marina "съел суп и салат"
    python scripts/testkit.py confirm marina
    python scripts/testkit.py state marina --tables meals,board_entries
    python scripts/testkit.py open marina /nutrition
    python scripts/testkit.py requests --bad
    python scripts/testkit.py run docs/scenarios/nutrition.yaml

Адрес и ключ берутся из окружения: `TESTKIT_URL` (по умолчанию
http://127.0.0.1:8000) и `TESTKIT_TOKEN` — тот же, с которым поднят сервер.

`run` проходит сценарий целиком: шаг за шагом, с проверками из самого файла, и
складывает полный журнал прогона в `.local/scenarios/`. В журнале лежит всё, что
понадобится разбору: реплики, вызовы инструментов, обращения к модели, снимки
данных, предупреждения сервера и записи о каждом запросе. Формат сценария —
docs/testkit.md.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from urllib.parse import quote

import httpx

BASE = os.environ.get("TESTKIT_URL", "http://127.0.0.1:8000").rstrip("/")
TOKEN = os.environ.get("TESTKIT_TOKEN", "")
JOURNAL_DIR = Path(__file__).resolve().parents[1] / ".local" / "scenarios"


class Stand:
    """Один разговор со стендом: ключ, куки участников, номера журнала."""

    def __init__(self, base: str = BASE, token: str = TOKEN):
        self.base = base.rstrip("/")
        self.token = token
        self.client = httpx.Client(timeout=120.0, follow_redirects=False)
        self.step = ""

    def _headers(self) -> Dict[str, str]:
        # Заголовки — латиница, а шаги названы по-русски: везём имя шага
        # процентным кодированием, стенд его раскодирует обратно.
        return {"X-Testkit-Token": self.token, "X-Testkit-Step": quote(self.step)}

    def call(self, method: str, path: str, **kwargs) -> Any:
        """Обращение к стенду.

        Отказ стенда (4xx) — это не крах прогона, а результат шага: «нечего
        подтверждать», «нет такого инструмента» — ровно то, что сценарий и
        проверяет. Наверх он едет разобранным, а прогон останавливает только
        то, что сломано по-настоящему: не тот ключ и обвал сервера.
        """
        response = self.client.request(method, f"{self.base}{path}",
                                       headers=self._headers(), **kwargs)
        if response.status_code in (401, 403) or response.status_code >= 500:
            raise SystemExit(f"Стенд ответил {response.status_code} на {path}: "
                             f"{response.text[:400]}")
        if response.status_code >= 400:
            detail = _detail(response)
            return {"status": response.status_code, "refused": True, "summary": detail,
                    "reply": detail}
        return response.json()

    # -- ручки -------------------------------------------------------------
    def health(self):
        return self.call("GET", "/api/testkit/health")

    def reset(self, autonomy: int = 2, seed: bool = True, traces: bool = True):
        return self.call("POST", "/api/testkit/reset",
                         json={"confirm": "wipe", "autonomy": autonomy, "seed": seed,
                               "traces": traces})

    def say(self, user: str, text: str, image_b64: str = None):
        body = {"user": user, "text": text}
        if image_b64:
            body["image_b64"] = image_b64
        return self.call("POST", "/api/testkit/say", json=body)

    def confirm(self, user: str, decision: str = "approve", pending_id=None):
        return self.call("POST", "/api/testkit/confirm",
                         json={"user": user, "decision": decision, "pending_id": pending_id or "last"})

    def tool(self, user: str, tool: str, arguments: dict = None):
        return self.call("POST", "/api/testkit/tool",
                         json={"user": user, "tool": tool, "arguments": arguments or {}})

    def tick(self, at: str = None):
        return self.call("POST", "/api/testkit/tick", json={"at": at} if at else {})

    def script(self, chat: List[dict] = None, json_replies: List[dict] = None):
        return self.call("POST", "/api/testkit/model/script",
                         json={"chat": chat or [], "json": json_replies or []})

    def state(self, user: str = None, tables: str = "", limit: int = 20, counts: bool = False):
        params = {"limit": limit, "counts": str(counts).lower()}
        if user:
            params["user"] = user
        if tables:
            params["tables"] = tables
        return self.call("GET", "/api/testkit/state", params=params)

    def traces(self, user: str = None, run_id: int = None, session: str = None, limit: int = 20):
        params: Dict[str, Any] = {"limit": limit}
        if user:
            params["user"] = user
        if run_id:
            params["run_id"] = run_id
        if session:
            params["session"] = session
        return self.call("GET", "/api/testkit/traces", params=params)

    def requests(self, since: int = 0, limit: int = 100, path: str = "", only_bad: bool = False):
        return self.call("GET", "/api/testkit/requests",
                         params={"since": since, "limit": limit, "path": path,
                                 "only_bad": str(only_bad).lower()})

    def messages(self, since: int = 0):
        return self.call("GET", "/api/testkit/messages", params={"since": since})

    def calls(self, since: int = 0, limit: int = 50):
        return self.call("GET", "/api/testkit/model/calls", params={"since": since, "limit": limit})

    def routes(self):
        return self.call("GET", "/api/testkit/routes")

    def cursor(self) -> int:
        return self.call("GET", "/api/testkit/cursor")["cursor"]

    # -- экраны ------------------------------------------------------------
    def login(self, user: str):
        """Войти за человека — дальше по экранам ходим обычным HTTP, как браузер."""
        return self.call("POST", "/api/testkit/login", json={"user": user})

    def open(self, user: str, path: str) -> dict:
        """Открыть экран глазами человека: код, заголовок, кусок текста."""
        self._become(user)
        response = self.client.get(f"{self.base}{path}",
                                   headers={"X-Testkit-Step": quote(self.step)},
                                   follow_redirects=False)
        return self._screen(user, path, response)

    def post(self, user: str, path: str, data: dict = None) -> dict:
        """Действие на экране: та же форма, что нажимает человек.

        Половина панели — это не переходы, а действия: поделиться доской,
        подтвердить оценку, переименовать раздел. Проверять их вызовом инструмента
        нельзя — у формы свой роут, свои проверки прав и своя разметка в ответе.
        """
        self._become(user)
        response = self.client.post(f"{self.base}{path}", data=data or {},
                                    headers={"X-Testkit-Step": quote(self.step)},
                                    follow_redirects=False)
        return self._screen(user, path, response)

    def _become(self, user: str):
        """Кем идём на экран. `anon` — никем: вход и приглашение открываются без входа."""
        if user in (None, "", "anon", "-"):
            self.client.cookies.clear()
            return
        self.login(user)

    def _screen(self, user: str, path: str, response) -> dict:
        body = response.text if "text/html" in response.headers.get("content-type", "") else ""
        return {
            "path": path,
            "user": user,
            "status": response.status_code,
            "location": response.headers.get("location"),
            "title": _between(body, "<title>", "</title>"),
            "text": _text(body)[:1500],
            "length": len(response.content),
        }

    def crawl(self, user: str, skip_params: bool = True) -> List[dict]:
        """Обойти все экраны панели глазами человека.

        Ищет не содержание, а обвал: пятисотку, пустую страницу, заслон там, где
        его быть не должно. Адреса с подстановкой (`/stats/{id}`) пропускаются —
        подставить в них нечего, не зная данных.
        """
        rows = []
        for route in self.routes()["routes"]:
            path = route["path"]
            if "GET" not in route["methods"] or (skip_params and "{" in path):
                continue
            if path in ("/logout",) or path.startswith(("/security/file", "/security/media")):
                continue
            self.step = f"crawl:{path}"
            rows.append(self.open(user, path))
        return rows


def _detail(response) -> str:
    try:
        return str(response.json().get("detail") or response.text[:200])
    except ValueError:
        return response.text[:200]


def _between(text: str, start: str, end: str) -> str:
    left = text.find(start)
    if left == -1:
        return ""
    right = text.find(end, left + len(start))
    return text[left + len(start):right].strip() if right != -1 else ""


def _text(html: str) -> str:
    """Грубое «как это читается»: без тегов, без скриптов, одной строкой."""
    import re

    body = re.sub(r"<(script|style|svg)\b.*?</\1>", " ", html, flags=re.S | re.I)
    body = re.sub(r"<[^>]+>", " ", body)
    body = body.replace("&nbsp;", " ").replace("&mdash;", "—").replace("&laquo;", "«")
    body = body.replace("&raquo;", "»").replace("&quot;", '"').replace("&amp;", "&")
    return re.sub(r"\s+", " ", body).strip()


# --- сценарии -------------------------------------------------------------

def load_scenario(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml
        return yaml.safe_load(raw)
    return json.loads(raw)


def check(expect: dict, step_result: dict, stand: Stand, user: str) -> List[str]:
    """Проверки шага. Возвращает список претензий — пустой значит «сошлось»."""
    problems: List[str] = []
    if not expect:
        return problems

    reply = step_result.get("reply") or step_result.get("summary") or step_result.get("text") or ""
    tools = [t.get("tool") for t in step_result.get("traces") or []]

    if "reply_contains" in expect:
        for needle in _as_list(expect["reply_contains"]):
            if needle.lower() not in reply.lower():
                problems.append(f"в ответе нет «{needle}»: {reply[:200]}")
    if "reply_not_contains" in expect:
        for needle in _as_list(expect["reply_not_contains"]):
            if needle.lower() in reply.lower():
                problems.append(f"в ответе есть лишнее «{needle}»: {reply[:200]}")
    if "tool" in expect:
        for name in _as_list(expect["tool"]):
            if name not in tools:
                problems.append(f"не вызван инструмент {name} (вызваны: {tools or '—'})")
    if expect.get("no_tools") and tools:
        problems.append(f"инструменты вызывались, а не должны были: {tools}")
    if "status" in expect and step_result.get("status") != expect["status"]:
        problems.append(f"код ответа {step_result.get('status')}, ждали {expect['status']}")
    if "location_contains" in expect:
        location = step_result.get("location") or ""
        for needle in _as_list(expect["location_contains"]):
            if needle.lower() not in location.lower():
                problems.append(f"переход ведёт не туда: «{location}», ждали «{needle}»")
    if "location_not_contains" in expect:
        location = step_result.get("location") or ""
        for needle in _as_list(expect["location_not_contains"]):
            if needle.lower() in location.lower():
                problems.append(f"переход ведёт с лишним: «{location}» содержит «{needle}»")
    if "text_not_contains" in expect:
        for needle in _as_list(expect["text_not_contains"]):
            if needle.lower() in (step_result.get("text") or "").lower():
                problems.append(f"на экране есть лишнее «{needle}»")
    if "text_contains" in expect:
        for needle in _as_list(expect["text_contains"]):
            if needle.lower() not in (step_result.get("text") or "").lower():
                problems.append(f"на экране нет «{needle}»")
    if "messages_contain" in expect:
        said = json.dumps(step_result.get("messages") or [], ensure_ascii=False).lower()
        for needle in _as_list(expect["messages_contain"]):
            if str(needle).lower() not in said:
                problems.append(f"ассистент не сказал сам «{needle}»")
    if expect.get("no_warnings") and step_result.get("warnings"):
        problems.append(f"сервер выписал предупреждения: {step_result['warnings']}")
    if step_result.get("error"):
        problems.append(f"ход упал: {step_result['error'].get('type')}: "
                        f"{step_result['error'].get('message')}")
    if "pending" in expect:
        waiting = len(step_result.get("pending") or [])
        if waiting != int(expect["pending"]):
            problems.append(f"ждущих действий {waiting}, ждали {expect['pending']}")

    for rule in _as_list(expect.get("rows") or []):
        table = rule["table"]
        rows = stand.state(user=rule.get("user", user), tables=table,
                           limit=50)["tables"].get(table, [])
        if "min" in rule and len(rows) < int(rule["min"]):
            problems.append(f"в таблице {table} строк {len(rows)}, ждали не меньше {rule['min']}")
        if "max" in rule and len(rows) > int(rule["max"]):
            problems.append(f"в таблице {table} строк {len(rows)}, ждали не больше {rule['max']}")
        if "contains" in rule or "not_contains" in rule:
            haystack = json.dumps(rows, ensure_ascii=False).lower()
            for needle in _as_list(rule.get("contains") or []):
                if str(needle).lower() not in haystack:
                    problems.append(f"в таблице {table} нет «{needle}»")
            for needle in _as_list(rule.get("not_contains") or []):
                if str(needle).lower() in haystack:
                    problems.append(f"в таблице {table} есть лишнее «{needle}»")
    return problems


def substitute(value, variables: Dict[str, str]):
    """Подставить `${имя}` в шаг: коды приглашений и номера записей рождаются по ходу."""
    if isinstance(value, str):
        for name, replacement in variables.items():
            value = value.replace("${" + name + "}", str(replacement))
        return value
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    return value


def capture(stand: Stand, rules: dict, user: str, variables: Dict[str, str]):
    """Запомнить значение из базы под именем — чтобы следующий шаг им пользовался."""
    for name, rule in (rules or {}).items():
        table = rule["table"]
        rows = stand.state(user=rule.get("user", user), tables=table, limit=50)["tables"].get(table, [])
        where = rule.get("where") or {}
        for row in reversed(rows):
            if all(str(row.get(key)) == str(value) for key, value in where.items()):
                variables[name] = row.get(rule.get("column", "id"))
                break


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def run_scenario(stand: Stand, path: Path) -> dict:
    scenario = load_scenario(path)
    name = scenario.get("name") or path.stem
    default_user = scenario.get("user") or "marina"
    print(f"\n=== {name} ({path.name})")

    if scenario.get("reset", True):
        options = scenario.get("reset") if isinstance(scenario.get("reset"), dict) else {}
        stand.reset(autonomy=int(options.get("autonomy", 2)),
                    seed=bool(options.get("seed", True)))
    model = scenario.get("model") or {}
    if model:
        stand.script(chat=model.get("chat"), json_replies=model.get("json"))

    journal = {"name": name, "file": str(path), "at": datetime.now().isoformat(), "steps": []}
    failures = 0
    variables: Dict[str, str] = {}

    for number, raw in enumerate(scenario.get("steps") or [], start=1):
        step = substitute(dict(raw), variables)
        expect = step.pop("expect", {}) or {}
        captures = step.pop("capture", {}) or {}
        label = step.pop("name", "") or _label(step)
        stand.step = f"{number:02d}:{label}"[:60]
        since = stand.cursor()
        user = step.get("user") or default_user

        if "model" in step:
            stand.script(chat=(step["model"] or {}).get("chat"),
                         json_replies=(step["model"] or {}).get("json"))
        result = _do(stand, step, user)
        capture(stand, captures, user, variables)

        problems = check(expect, result, stand, user)
        failures += len(problems)
        journal["steps"].append({
            "no": number, "name": label, "step": raw, "result": result,
            "problems": problems,
            "requests": stand.requests(since=since).get("requests", []),
            "messages": stand.messages(since=since).get("messages", []),
        })
        mark = "ok " if not problems else "БАГ"
        print(f"  {mark} {number:02d}. {label}")
        for problem in problems:
            print(f"       ↳ {problem}")

    journal["failures"] = failures
    JOURNAL_DIR.mkdir(parents=True, exist_ok=True)
    out = JOURNAL_DIR / f"{path.stem}.json"
    out.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  журнал: {out}  ·  претензий: {failures}")
    return journal


def _label(step: dict) -> str:
    for key in ("say", "open", "post", "confirm", "tool", "tick", "state"):
        if key in step:
            value = step[key]
            return f"{key}: {value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)[:50]}"
    return "шаг"


def _do(stand: Stand, step: dict, user: str) -> dict:
    """Один шаг сценария — тем же вызовом, каким его сделал бы человек."""
    if "say" in step:
        return stand.say(user, step["say"], image_b64=step.get("image_b64"))
    if "open" in step:
        return stand.open(user, step["open"])
    if "post" in step:
        return stand.post(user, step["post"], step.get("data"))
    if "confirm" in step:
        decision = step["confirm"] if isinstance(step["confirm"], str) else "approve"
        return stand.confirm(user, decision=decision, pending_id=step.get("pending_id"))
    if "tool" in step:
        return stand.tool(user, step["tool"], step.get("arguments"))
    if "tick" in step:
        return stand.tick(step["tick"] if isinstance(step["tick"], str) else None)
    if "state" in step:
        return stand.state(user=user, tables=step.get("tables", ""))
    raise SystemExit(f"Непонятный шаг сценария: {step}")


# --- разбор командной строки ----------------------------------------------

def show(value: Any):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv: List[str] = None):
    parser = argparse.ArgumentParser(description="Пульт стенда", allow_abbrev=False)
    parser.add_argument("--url", default=BASE)
    parser.add_argument("--token", default=TOKEN)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("health")
    sub.add_parser("routes")
    sub.add_parser("cursor")

    p = sub.add_parser("reset")
    p.add_argument("--autonomy", type=int, default=2)
    p.add_argument("--no-seed", action="store_true")

    p = sub.add_parser("say")
    p.add_argument("user")
    p.add_argument("text")
    p.add_argument("--brief", action="store_true", help="только реплика и инструменты")

    p = sub.add_parser("confirm")
    p.add_argument("user")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--id")

    p = sub.add_parser("tool")
    p.add_argument("user")
    p.add_argument("tool")
    p.add_argument("--arguments", default="{}")

    p = sub.add_parser("tick")
    p.add_argument("--at")

    p = sub.add_parser("state")
    p.add_argument("user", nargs="?")
    p.add_argument("--tables", default="")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--counts", action="store_true")

    p = sub.add_parser("traces")
    p.add_argument("--user")
    p.add_argument("--run", type=int)
    p.add_argument("--session")
    p.add_argument("--limit", type=int, default=5)

    p = sub.add_parser("requests")
    p.add_argument("--since", type=int, default=0)
    p.add_argument("--path", default="")
    p.add_argument("--bad", action="store_true")

    p = sub.add_parser("messages")
    p.add_argument("--since", type=int, default=0)

    p = sub.add_parser("calls")
    p.add_argument("--since", type=int, default=0)

    p = sub.add_parser("script")
    p.add_argument("file", help="JSON с полями chat/json")

    p = sub.add_parser("open")
    p.add_argument("user")
    p.add_argument("path")

    p = sub.add_parser("post")
    p.add_argument("user")
    p.add_argument("path")
    p.add_argument("--data", default="{}")

    p = sub.add_parser("crawl")
    p.add_argument("user")

    p = sub.add_parser("run")
    p.add_argument("files", nargs="+")

    args = parser.parse_args(argv)
    stand = Stand(args.url, args.token)
    if not stand.token:
        raise SystemExit("Не задан TESTKIT_TOKEN — стенд без ключа не отвечает")

    if args.command == "health":
        show(stand.health())
    elif args.command == "routes":
        show(stand.routes())
    elif args.command == "cursor":
        show({"cursor": stand.cursor()})
    elif args.command == "reset":
        show(stand.reset(autonomy=args.autonomy, seed=not args.no_seed))
    elif args.command == "say":
        result = stand.say(args.user, args.text)
        if args.brief and not result.get("refused"):
            result = {"reply": result["reply"],
                      "tools": [t["tool"] for t in result["traces"]],
                      "pending": result["pending"], "warnings": result["warnings"],
                      "error": result["error"]}
        show(result)
    elif args.command == "confirm":
        show(stand.confirm(args.user, "reject" if args.reject else "approve", args.id))
    elif args.command == "tool":
        show(stand.tool(args.user, args.tool, json.loads(args.arguments)))
    elif args.command == "tick":
        show(stand.tick(args.at))
    elif args.command == "state":
        show(stand.state(args.user, args.tables, args.limit, args.counts))
    elif args.command == "traces":
        show(stand.traces(args.user, args.run, args.session, args.limit))
    elif args.command == "requests":
        show(stand.requests(since=args.since, path=args.path, only_bad=args.bad))
    elif args.command == "messages":
        show(stand.messages(since=args.since))
    elif args.command == "calls":
        show(stand.calls(since=args.since))
    elif args.command == "script":
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        show(stand.script(chat=payload.get("chat"), json_replies=payload.get("json")))
    elif args.command == "open":
        show(stand.open(args.user, args.path))
    elif args.command == "post":
        show(stand.post(args.user, args.path, json.loads(args.data)))
    elif args.command == "crawl":
        rows = stand.crawl(args.user)
        for row in rows:
            mark = "ok " if row["status"] < 400 else "БАГ"
            print(f"{mark} {row['status']} {row['path']:38} {row['title'][:40]}")
        bad = [r for r in rows if r["status"] >= 400]
        print(f"\nэкранов: {len(rows)}, с ошибкой: {len(bad)}")
        return 1 if bad else 0
    elif args.command == "run":
        total = 0
        for name in args.files:
            total += run_scenario(stand, Path(name))["failures"]
        print(f"\nвсего претензий: {total}")
        return 1 if total else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
