# CLAUDE.md

## Agent skills

### Issue tracker

Issues живут в GitHub Issues репозитория `MaksimKhanin/FamilyAssistant`; операции
выполняются через GitHub MCP, а не через `gh` CLI. См. `docs/agents/issue-tracker.md`.

### Domain docs

Single-context: один `CONTEXT.md` и `docs/adr/` в корне репозитория.
См. `docs/agents/domain.md`.

### Стенд

Пройти пользовательский путь целиком, найти поломку, разобрать её до причины —
`docs/agents/testing.md`. Ручки `/api/testkit`, пульт `scripts/testkit.py`,
формат сценариев и разбор затыков — `docs/testkit.md`.

## Claude Code в облачной среде

Как `/wayfinder` и остальные вендоренные скиллы подключены для cloud-сессий —
см. `docs/claude-code-cloud.md`.
