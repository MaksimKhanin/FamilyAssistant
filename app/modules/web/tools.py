"""Инструмент поиска в интернете.

Сводка инструмента — выдержки с адресами, и это принципиально: модель отвечает
по ним, а не по памяти, и называет источники по домену. Чего в выдержках нет,
того в ответе быть не должно — тот же принцип, что у PRODUCT_FACTS_SYSTEM
(«это чтение, а не воспоминание»).
"""
from app.agent.registry import ToolContext, ToolResult, tool
from app.core import websearch

MODULE = "web"

#: Сколько результатов пересказывается модели. Больше — не глубже: выдержки
#: дальше третьей-четвёртой обычно повторяют первые.
RESULTS_LIMIT = 4


@tool(
    name="search_web",
    module=MODULE,
    title="Поиск в интернете",
    description="""
    Поискать в интернете то, чего нет в семейных данных: новости, погоду, цены,
    адреса и часы работы, «что такое …», любые свежие или внешние факты.
    Передавай запрос так, как его искал бы человек. Сложный вопрос разбей на
    несколько вызовов с разными запросами.
    Отвечай по выдержкам и называй источники по домену («по данным …»). Чего в
    выдержках нет — не выдумывай.
    Не для еды с этикеткой: её log_meal и lookup_product ищут сами.
    """,
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Поисковый запрос, как его набрал бы человек"},
        },
        "required": ["query"],
    },
    read_only=True,
    available=lambda: websearch.client.configured,
)
def search_web(ctx: ToolContext, query: str) -> ToolResult:
    query = (query or "").strip()
    if not query:
        return ToolResult(summary="Пустой запрос — искать нечего.", ok=False)
    try:
        results = websearch.client.search(query)
    except websearch.SearchUnavailable:
        return ToolResult(
            summary="Поиск в интернете не отвечает. Скажи об этом честно и, если можешь, "
                    "ответь своими знаниями, назвав их оценкой без источника.",
            ok=False,
        )

    if not results:
        return ToolResult(
            summary=f"По запросу «{query}» ничего не нашлось. Скажи об этом честно; "
                    f"можно попробовать другой запрос.",
        )

    shown = results[:RESULTS_LIMIT]
    lines = []
    for i, r in enumerate(shown, start=1):
        lines.append(f"{i}. {r.title} — {r.domain}\n   {r.snippet}")
    return ToolResult(
        summary=f"Выдержки по запросу «{query}» (отвечай по ним, источники называй по домену):\n"
                + "\n".join(lines),
        data={"sources": [r.domain for r in shown]},
    )
