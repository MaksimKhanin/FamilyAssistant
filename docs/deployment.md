# Развёртывание

Две части: сервер (Docker) и домашний воркер рядом с камерами (обычный Python-процесс).

## Сервер

```bash
git clone <repo> && cd FamilyAssistant
cp .env.__EXAMPLE__ .env
```

Заполнить `.env`. Три секрета обязательны, случайную строку удобно получить так:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

| Переменная | Зачем |
|---|---|
| `POSTGRES_PASSWORD` | пароль БД |
| `SESSION_SECRET` | подпись сессионных cookie; поменяется — все выйдут из панели |
| `INGEST_API_KEY` | им домашний воркер представляется серверу |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | модель; без них панель работает, чат — нет |
| `TELEGRAM_BOT_TOKEN` | токен от [@BotFather](https://t.me/BotFather) |
| `PUBLIC_BASE_URL` | адрес панели снаружи — подставляется в ссылки из бота |
| `COOKIE_SECURE` | `true` только за HTTPS |

Запуск и первая семья:

```bash
docker compose up -d --build
docker compose exec web python -m scripts.seed \
    --family "Наша семья" --name Марина --username marina --password ... --relation мама
```

Поднимутся четыре контейнера: `web` (панель + ingest), `bot`, `scheduler`, `db` и
`redis`. Проверить: `curl localhost:8000/healthz`.

### HTTPS

`COOKIE_SECURE=true` только когда панель стоит за реальным TLS (nginx, Caddy,
Traefik). По простому HTTP браузер молча выбросит Secure-куку, и вход будет
«не срабатывать» без всякой ошибки — это самая частая причина «не пускает в панель».

## Домашний воркер

Ставится **на машине рядом с камерами**: RTSP по интернету гонять незачем, да и YOLO
должен работать там, где кадры и так есть.

```bash
pip install -r requirements-edge.txt
cp edge/cameras.yml.example edge/cameras.yml
```

В `edge/cameras.yml` — адреса камер (обычно sub-stream: он мельче и дешевле для
детекции), адрес сервера и тот же `INGEST_API_KEY`. Запуск:

```bash
python -m edge.main
```

Веса YOLO подтягиваются автоматически при первом запуске. Камера появится в панели
сама, после первого события.

### Как воркеру достучаться до сервера

Публиковать ingest в интернет необязательно и нежелательно. Варианты, от простого:

* **Приватная сеть** (Tailscale / WireGuard) — воркер видит сервер по внутреннему
  адресу, наружу не торчит ничего.
* **SSH reverse tunnel** — если сервер снаружи, а дом за NAT.
* **Публичный HTTPS** — тогда обязательно с TLS и длинным `INGEST_API_KEY`.

### Автозапуск (systemd)

```ini
[Unit]
Description=Family Assistant edge worker
After=network-online.target

[Service]
WorkingDirectory=/opt/FamilyAssistant
ExecStart=/opt/FamilyAssistant/.venv/bin/python -m edge.main
Restart=always
RestartSec=10
User=family

[Install]
WantedBy=multi-user.target
```

## Эксплуатация

**Логи.** `docker compose logs -f web bot scheduler`. Уровень — `LOG_LEVEL`.

**Диск.** Кадры лежат в томе `media`, ротация — по `Camera.retention_days` (14 дней по
умолчанию), делает `scheduler` ночью в 4:00. Событие в БД переживает свой снимок.

**Бэкап.** Важна база: `docker compose exec db pg_dump -U family family_assistant`.
Кадры — расходный материал, они и так удаляются через две недели.

**Обновление.** `git pull && docker compose up -d --build`. Схема БД создаётся
автоматически; **удаление** колонок автоматически не произойдёт — когда схема начнёт
меняться всерьёз, нужен Alembic.

**Смена модели.** Поменять `LLM_*` в `.env`, `docker compose up -d`. Экран «Модель и
знания» хранит выбор семьи (локальная / облачная / гибрид) — он влияет на поведение
приватности; сам endpoint задаётся переменными окружения.
