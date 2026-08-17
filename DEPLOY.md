# Деплой Hoop Pro AI — пошаговый гайд (~20 минут)

Приложение готово к продакшену: waitress-сервер, SQLite (не требует отдельной БД),
Docker-образ, all-in-one фронтенд. Ниже два сценария: с Docker (рекомендуется) и без.

## Переменные окружения (API.env)

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `CLAUDEHUB_API_KEY` | Ключ LLM-агрегатора | — (обязателен) |
| `CLAUDEHUB_BASE_URL` | OpenAI-совместимый endpoint | `https://api.claudehub.fun/v1` |
| `GEMINI_API_KEY`, `OPENAI_API_KEY` | Резервные LLM | — |
| `ADMIN_TOKEN` | Токен для `/api/admin/stats` | не задан → эндпоинт выключен |
| `TRUSTED_PROXY` | `1` — доверять `X-Forwarded-For` за nginx/Caddy | выкл |
| `ANON_DAILY_LIMIT` | Планов/сутки анонимам | `3` |
| `USER_DAILY_LIMIT` | Планов/сутки пользователям | `15` |
| `LLM_AUTO_STRATEGY` | `budget` (дешёвые модели) или `premium` | `budget` |
| `COACH_DB_PATH` | Путь к SQLite (для Docker volume) | ./coach_database.sqlite3 |
| `PORT` | Порт | `8000` |

Сгенерировать `ADMIN_TOKEN`: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

## Вариант A: Docker (рекомендуется)

VPS с оплатой российскими картами: Timeweb Cloud, Reg.ru, VDSina (Ubuntu 22.04+, 1 vCPU / 1 ГБ хватает).

```bash
# 1. На сервере: установить Docker
curl -fsSL https://get.docker.com | sh

# 2. Скопировать проект (без .git и .venv)
scp -r ./basket_coach_api user@SERVER_IP:/opt/coach
# или: git clone <ваш репозиторий> /opt/coach

# 3. Создать API.env на сервере (НЕ коммитить!)
nano /opt/coach/API.env

# 4. Запустить
cd /opt/coach && docker compose up -d --build

# 5. Проверить
curl http://localhost:8000/api/me
```

База живёт в `./coach_data/` (volume) — переживает пересборку контейнера.

## Вариант B: без Docker (systemd + waitress)

```bash
sudo apt update && sudo apt install -y python3-venv fonts-dejavu-core
cd /opt/coach
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp API.env.example API.env && nano API.env   # вписать ключи
```

Юнит `/etc/systemd/system/coach.service`:

```ini
[Unit]
Description=Hoop Pro AI
After=network.target

[Service]
WorkingDirectory=/opt/coach
EnvironmentFile=/opt/coach/API.env
ExecStart=/opt/coach/.venv/bin/python app.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now coach
```

## Домен и HTTPS (Caddy, 2 команды)

```bash
sudo apt install -y caddy
# /etc/caddy/Caddyfile:
#   coach-yourdomain.ru {
#       reverse_proxy localhost:8000
#   }
sudo systemctl reload caddy
```

Caddy сам выпускает и продлевает Let's Encrypt-сертификаты. В `API.env` добавить `TRUSTED_PROXY=1`.

## После деплоя

- Метрики: `curl -H "X-Admin-Token: <ADMIN_TOKEN>" https://ваш-домен/api/admin/stats`
- Логи: `docker compose logs -f` или `/opt/coach/app.log`
- Лендинг доступен на `/landing`

## Чек-лист безопасности перед публичным запуском

- [ ] `ADMIN_TOKEN` задан (иначе статистика недоступна — это безопасно, но и бесполезно)
- [ ] `TRUSTED_PROXY=1` только если реально стоит reverse-proxy
- [ ] Квоты (`ANON_DAILY_LIMIT`) подобраны под бюджет API
- [ ] Резервная копия `coach_data/` настроена (cron + scp достаточно)
- [ ] База знаний юридически чистая (см. аудит проекта)
