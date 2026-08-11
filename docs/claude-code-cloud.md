# Claude Code: скиллы в облачной среде исполнения

Как сделать так, чтобы `/wayfinder` (и другие скиллы из плагина
[`mattpocock-skills`](https://github.com/mattpocock/skills)) работали в сессиях
Claude Code на claude.ai/code, а не только на локальной машине.

## Проблема

Облачная сессия поднимается в свежем контейнере: туда попадает клон репозитория
и настройки окружения, но **не** ваш локальный `~/.claude`. Плагины Claude Code
устанавливаются именно в `~/.claude/plugins`, поэтому локально установленный
`mattpocock-skills` в облаке недоступен, и `/wayfinder` отвечает
`Unknown command: /wayfinder`.

## Что проверялось

Все четыре варианта прогонялись в реальном облачном контейнере
(Claude Code 2.1.227):

| Вариант | Результат |
| --- | --- |
| Только `extraKnownMarketplaces` + `enabledPlugins` в `.claude/settings.json` | ❌ `Unknown command` |
| `SessionStart`-хук с `claude plugin install` | ❌ `Unknown command` в текущей сессии |
| `claude plugin marketplace add` + `claude plugin install` **до** старта сессии | ✅ работает |
| Скиллы в `.claude/skills/` репозитория | ✅ работает |

Почему первые два не работают:

- **`enabledPlugins` включает плагин, но не устанавливает его.** В документации
  это сказано прямо: *«Enabling a plugin from an external source such as a GitHub
  repository or npm package in a project's `.claude/settings.json` doesn't install
  it for other people… every path that loads plugins asks each user to install and
  trust the plugin before it runs»*. Интерактивного подтверждения в облаке нет.
- **`SessionStart`-хук срабатывает слишком поздно.** Плагин действительно
  устанавливается (появляется в `~/.claude/plugins/installed_plugins.json`), но
  скиллы к этому моменту уже загружены. Помогло бы со второй сессии — а в облаке
  каждая сессия начинается с нового контейнера, так что второй сессии не бывает.

## Как это устроено здесь

### 1. Вендоринг (основной путь, работает без настройки)

`wayfinder` и скиллы, которые он вызывает (`grilling`, `domain-modeling`,
`research`, `prototype`), лежат в `.claude/skills/`. Они загружаются напрямую из
клона репозитория — без сети, без установки, с первой сессии.

Провенанс, точный коммит апстрима и процедура обновления — в
[`.claude/skills/VENDORED.md`](../.claude/skills/VENDORED.md).

### 2. Установка плагина целиком (опционально, даёт все ~25 скиллов)

Если нужен весь набор скиллов плагина и автообновления, добавьте в **setup script
окружения** (claude.ai/code → Environments → нужное окружение → setup script) две
команды:

```bash
claude plugin marketplace add mattpocock/skills
claude plugin install mattpocock-skills@mattpocock --scope user
```

Setup script выполняется при создании контейнера, до старта Claude, — именно
поэтому он успевает, а `SessionStart`-хук нет. Требуется сетевой доступ к
github.com из окружения.

Скиллы плагина неймспейсятся именем плагина (`/mattpocock-skills:wayfinder`), так
что с вендоренным `/wayfinder` они не конфликтуют.

### 3. `.claude/settings.json`

Объявляет маркетплейс и включает плагин. В облаке одного этого недостаточно (см.
таблицу выше), но для тех, кто работает локально или в десктоп-приложении, это
избавляет от ручного `/plugin marketplace add`.

## Issue-трекер для `/wayfinder`

`/wayfinder` строит карту работы из issues, а как именно с ними работать — читает
из `docs/agents/issue-tracker.md`.

Штатный GitHub-шаблон этих скиллов целиком построен на `gh` CLI, **которого в
облачном контейнере нет** (`which gh` → пусто). Поэтому
[`docs/agents/issue-tracker.md`](./agents/issue-tracker.md) переписан под
инструменты GitHub MCP (`mcp__github__*`).

Одно следствие этого стоит помнить: нативные *issue dependencies* GitHub через MCP
недоступны, поэтому блокировки между тикетами записываются строкой
`Blocked by: #<n>, #<n>` в теле тикета, а не рёбрами графа зависимостей. В UI
GitHub фронтир поэтому визуально не подсвечивается — его считает сам агент.

Sub-issues через MCP доступны, так что карта и её дочерние тикеты связываются
штатной иерархией GitHub.

## Проверить, что всё на месте

В облачной сессии:

```
/wayfinder
```

Если отвечает `Unknown command: /wayfinder` — скиллы не загрузились: проверьте,
что `.claude/skills/wayfinder/SKILL.md` есть в клоне и что вы на ветке, где он
закоммичен.
