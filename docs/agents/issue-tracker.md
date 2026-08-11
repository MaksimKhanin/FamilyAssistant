# Issue tracker: GitHub (через MCP)

Issues и спеки этого репозитория живут в GitHub Issues репозитория
`MaksimKhanin/FamilyAssistant`.

> **Инструменты:** используйте GitHub MCP (`mcp__github__*`). `gh` CLI в облачной
> среде исполнения не установлен — команды `gh …` из стандартного шаблона этих
> скиллов там не работают. Локально `gh` может быть доступен, но чтобы поведение
> совпадало в обеих средах, придерживайтесь MCP-инструментов.
>
> `owner` = `MaksimKhanin`, `repo` = `FamilyAssistant` во всех вызовах ниже.

## Conventions

- **Создать issue**: `issue_write` с `method: "create"`, полями `title`, `body`,
  при необходимости `labels` и `assignees`.
- **Прочитать issue**: `issue_read` с `method: "get"`; комментарии — отдельным
  вызовом `method: "get_comments"`, метки — `method: "get_labels"`.
- **Список issues**: `list_issues` с `state: "OPEN"`, фильтром `labels` и
  `fields`, чтобы не тянуть лишнее (`body` — самая тяжёлая часть ответа).
- **Комментарий**: `add_issue_comment` с `issue_number` и `body`.
- **Метки**: `issue_write` с `method: "update"` и полным набором `labels` —
  список заменяется целиком, поэтому сначала прочитайте текущие через
  `issue_read` / `method: "get_labels"`, затем отправьте объединённый набор.
- **Закрыть**: `issue_write` с `method: "update"`, `state: "closed"` и
  `state_reason: "completed"` (или `"not_planned"`).

Каждый комментарий, который вы публикуете от своего имени, заканчивайте
атрибуцией Claude Code — так ревьюеру видно авторство.

## Pull requests as a triage surface

**PRs as a request surface: no.** _(Поставьте `yes`, если внешние PR в этом репозитории
считаются заявками на фичи; флаг читает `/triage`.)_

Если `yes` — читайте PR через `pull_request_read`, список через
`list_pull_requests` / `search_pull_requests`, комментарии и метки — теми же
`add_issue_comment` и `issue_write` (номер PR передаётся как `issue_number`).

GitHub использует общее пространство номеров для issues и PR, поэтому голый
`#42` может оказаться и тем, и другим: сначала пробуйте `issue_read`, при
несоответствии — `pull_request_read`.

## When a skill says "publish to the issue tracker"

Создайте GitHub issue (`issue_write` / `create`).

## When a skill says "fetch the relevant ticket"

`issue_read` / `get` + `issue_read` / `get_comments`.

## Wayfinding operations

Используется `/wayfinder`. **Карта** — одна issue, **тикеты** — её дочерние issues.

- **Карта**: одна issue с меткой `wayfinder:map`, в теле — Destination / Notes /
  Decisions so far / Not yet specified / Out of scope.
  `issue_write` / `create` с `labels: ["wayfinder:map"]`.

- **Дочерний тикет**: обычная issue, привязанная к карте как GitHub sub-issue.
  Порядок вызовов:
  1. `issue_write` / `create` — создать тикет с меткой `wayfinder:<type>`
     (`research` / `prototype` / `grilling` / `task`);
  2. `issue_read` / `get` по номеру нового тикета — взять поле `id`
     (**database id**, не `number` и не `node_id`);
  3. `sub_issue_write` с `method: "add"`, `issue_number: <номер карты>`,
     `sub_issue_id: <id из шага 2>`.

  Если sub-issues в репозитории недоступны — добавьте тикет в task list в теле
  карты и поставьте `Part of #<номер карты>` первой строкой тела тикета.

- **Блокировки**: нативные issue dependencies GitHub через MCP **недоступны**,
  поэтому используется конвенция в теле тикета — строка

  ```
  Blocked by: #<n>, #<n>
  ```

  первой строкой тела (сразу после `Part of`, если он есть). Тикет разблокирован,
  когда все перечисленные issues закрыты. Проставляйте эти строки вторым проходом,
  после создания всех тикетов: ссылаться друг на друга они могут только получив номера.

- **Frontier query**: `issue_read` / `get_sub_issues` по карте (или task list в теле
  карты) → оставить открытые → отбросить те, у кого в `Blocked by` есть открытая
  issue, и те, у кого есть assignee. Первый в порядке карты выигрывает.

- **Claim**: `issue_write` / `update` с `assignees: ["<логин ведущего разработчика>"]` —
  первая запись в сессии, до любой работы. Логин берите из `get_me`, если тикет
  забирает агент от вашего имени.

- **Resolve**: `add_issue_comment` с ответом → `issue_write` / `update` со
  `state: "closed"`, `state_reason: "completed"` → дописать context pointer
  (гист + ссылка) в раздел Decisions so far на карте (`issue_write` / `update`
  по номеру карты с новым `body`).

### Метки, которые должны существовать

`wayfinder:map`, `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`,
`wayfinder:task`. Проверить наличие — `get_label`; если метки нет, `issue_write`
создаст issue без неё и молча потеряет ярлык, поэтому недостающие метки заведите
заранее в настройках репозитория.
