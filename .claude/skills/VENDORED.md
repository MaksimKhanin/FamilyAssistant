# Vendored skills

Эти скиллы скопированы из [`mattpocock/skills`](https://github.com/mattpocock/skills)
(плагин `mattpocock-skills`, лицензия MIT — см. [LICENSE](./LICENSE)).

- **Источник:** `https://github.com/mattpocock/skills`
- **Коммит:** `84fdeffd12f2ee307994d1eb6feb48173b6e0502`
- **Версия плагина:** `1.2.3`

## Зачем копия, если есть плагин

Плагины Claude Code живут в `~/.claude/plugins` — в облачной среде исполнения
(claude.ai/code) контейнер создаётся с нуля, туда попадает только клон
репозитория, поэтому локально установленный плагин недоступен.

Одного объявления плагина в `.claude/settings.json` недостаточно: `enabledPlugins`
включает плагин, но не устанавливает его — установка требует интерактивного
подтверждения, которого в облаке нет. Проверено: с одним лишь `settings.json`
`/wayfinder` отвечает `Unknown command`.

Скиллы, лежащие в `.claude/skills/` репозитория, загружаются напрямую из клона —
без сети, без установки, с первой же сессии.

`.claude/settings.json` в этом репозитории всё равно объявляет маркетплейс и
плагин: это удобно для тех, кто работает локально или в десктоп-приложении, где
установка плагина проходит штатно.

## Что скопировано

| Скилл | Роль |
| --- | --- |
| `wayfinder` | сама команда `/wayfinder` |
| `grilling` | вызывается из wayfinder для HITL-тикетов |
| `domain-modeling` | вызывается из wayfinder вместе с grilling |
| `research` | резолвит AFK-тикеты типа `research` (сабагентом) |
| `prototype` | резолвит тикеты типа `prototype` |

Сайдкары `agents/openai.yaml` (для других агентных харнессов) не копировались.
Остальные ~20 скиллов плагина сюда не входят — они приезжают только через
установку плагина (см. `docs/claude-code-cloud.md`).

## Как обновить

```bash
git clone --depth 1 https://github.com/mattpocock/skills /tmp/mp-skills
for s in wayfinder research prototype domain-modeling; do
  rm -rf .claude/skills/$s && cp -r /tmp/mp-skills/skills/engineering/$s .claude/skills/$s
done
rm -rf .claude/skills/grilling && cp -r /tmp/mp-skills/skills/productivity/grilling .claude/skills/grilling
find .claude/skills -name agents -type d -exec rm -rf {} +
git -C /tmp/mp-skills rev-parse HEAD   # обновите коммит выше
```

Апстрим не трогали — файлы скиллов лежат как есть. Локальная адаптация только
одна и живёт снаружи: `docs/agents/issue-tracker.md` описывает операции трекера
через GitHub MCP, а не через `gh` CLI (в облачном контейнере `gh` не установлен).
