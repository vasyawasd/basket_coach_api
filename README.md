# 🏀 Basketball Coach AI (Hoop Pro API)

**Basketball Coach AI** — это высокопроизводительный сервис и веб-приложение для автоматической генерации персонализированных тренировочных планов для баскетболистов на базе методической базы знаний (**RAG**) и многоуровневого каскада нейросетевых моделей (**LLM Cascade Engine**).

---

## 📑 Содержание
1. [Возможности](#-возможности)
2. [Архитектура и стек технологий](#-архитектура-и-стек-технологий)
3. [Структура проекта](#-структура-проекта)
4. [Быстрый запуск и установка](#-быстрый-запуск-и-установка)
5. [Переменные окружения](#-переменные-окружения)
6. [API Документация](#-api-документация)
7. [База знаний и RAG (Retrieval-Augmented Generation)](#-база-знаний-и-rag)
8. [Каскад нейросетей и защита токенов](#-каскад-нейросетей-и-защита-токенов)
9. [Безопасность и хранение данных](#-безопасность-и-хранение-данных)
10. [Экспорт в PDF и панель метрик](#-экспорт-в-pdf-и-панель-метрик)
11. [Тестирование](#-тестирование)
12. [Деплой и продакшен](#-деплой-и-продакшен)

---

## ✨ Возможности

- **Персонализированные тренировочные циклы:** Генерация подробного расписания (разминка, плиометрика, технические упражнения, СФП/ОФП, заминка, рекомендации по восстановлению) с учетом роста, веса, позиции, целей и ограничений по здоровью игрока.
- **Научно обоснованная база знаний:** RAG-поиск по 8 фундаментальным книгам (Верхошанский, Supertraining, Triphasic Training, Vertical Jump Bible и др.) — более 4 500 страниц методик.
- **Интеллектуальный каскад нейросетей:** Автоматический выбор и переключение между моделями (Claude, DeepSeek, Qwen, Gemini, GPT-4o) с защитой от сбоев и контролем расходов.
- **Предварительное зондирование (Ping Probe):** 1-токенная проверка доступности моделей перед отправкой тяжелого промпта — 0 перерасхода токенов на зависших провайдерах.
- **Адаптация планов:** Интерактивное уточнение плана тренировок (смена акцента, замена упражнений при травмах, регулировка интенсивности).
- **Экспорт в PDF:** Выгрузка красиво оформленного плана тренировок для печати и отправки спортсменам.
- **Безопасность Enterprise-уровня:** 3-ступенчатый хэшинг паролей (SHA-256 + Salt + Bcrypt), SQLite с WAL-режимом, OWASP security headers, скользящий rate limiting и дневные лимиты.

---

## 🏛 Архитектура и стек технологий

```
  ┌─────────────────────────────────────────────────────────────┐
  │                 Frontend (SPA / index.html)                 │
  │     Конструктор параметров, интерактивный план, PDF-экспорт │
  └──────────────────────────────┬──────────────────────────────┘
                                 │ HTTP / JSON REST API
  ┌──────────────────────────────▼──────────────────────────────┐
  │                   Серверный слой (Backend)                  │
  │   • app.py (Flask / Waitress WSGI)                          │
  │   • main.py (FastAPI / Uvicorn - совместимый слой)          │
  │   • OWASP Security Headers (CSP, Frame-Options, HSTS)       │
  │   • Rate Limiting & Quota Manager (rate_limiter.py)         │
  └──────────────┬───────────────────────────────┬──────────────┘
                 │                               │
  ┌──────────────▼──────────────┐ ┌──────────────▼──────────────┐
  │    База данных (db.py)      │ │   Поисковый движок (rag.py) │
  │  • SQLite (WAL mode)        │ │  • 4 555 страниц (Markdown) │
  │  • Хэшированные сессии      │ │  • Инвертированный индекс   │
  │  • Параметризованный SQL    │ │  • Кросс-языковой словарь   │
  └─────────────────────────────┘ └──────────────┬──────────────┘
                                                 │
                                  ┌──────────────▼──────────────┐
                                  │  LLM Cascade (llm_service)  │
                                  │  • budget / premium         │
                                  │  • 1-token Ping Probes      │
                                  │  • ClaudeHub / Gemini / OAI │
                                  └─────────────────────────────┘
```

- **Backend:** Python 3.11+ (Flask, Waitress, FastAPI, Pydantic)
- **Database:** SQLite3 (WAL-режим, Foreign Keys, индексы)
- **RAG & Search:** Inverted Index BM25-подобный поиск, PyPDF, Markdown Index
- **Security:** Bcrypt, Hashlib, Secrets, HMAC, OWASP Headers
- **PDF Engine:** ReportLab
- **Frontend:** Vanilla HTML5, CSS3 (Modern Dark Theme), JavaScript (Fetch, SPA)
- **DevOps:** Docker, Docker Compose, Caddy / Nginx

---

## 📁 Структура проекта

```
basket_coach_api/
├── app.py                  # Основной Flask API сервер (Waitress для продакшена)
├── main.py                 # FastAPI реализация API (альтернативный ASGI сервер)
├── auth.py                 # Модуль авторизации (Bcrypt, токены, сессии)
├── db.py                   # Подключение и схема SQLite (WAL-режим, транзакции)
├── plan_service.py         # Бизнес-логика генерации и адаптации планов
├── rag.py                  # RAG-движок, инвертированный индекс и поиск по книгам
├── llm_service.py          # LLM каскад, зондирование (Ping Probes), вызовы моделей
├── rate_limiter.py         # SQLite скользящий rate limiter и управление квотами
├── pdf_export.py           # Генератор брендированных PDF-документов тренировок
├── metrics.py              # Сбор и хранение продуктовых метрик и статистики
├── convert_pdf_to_md.py    # Утилита парсинга PDF-книг в чистый Markdown
├── index.html              # Одностраничное веб-приложение (SPA)
├── landing.html            # Промо-страница платформы
├── run_tests.py            # Полный набор интеграционных тестов безопасности и API
├── requirements.txt        # Зависимости проекта
├── Dockerfile              # Docker-образ приложения
├── docker-compose.yml      # Конфигурация запуска в контейнерах
├── DEPLOY.md               # Пошаговая инструкция по деплою на сервер
├── .env.example            # Пример переменных окружения
├── knowledge_base/         # Исходные методические книги в PDF
└── knowledge_base_md/      # Очищенная база знаний в формате Markdown
```

---

## 🚀 Быстрый запуск и установка

### 1. Клонирование репозитория и окружение
```bash
git clone https://github.com/vasyawasd/basket_coach_api.git
cd basket_coach_api

# Создание виртуального окружения
python -m venv .venv

# Активация:
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# Установка зависимостей
pip install -r requirements.txt
```

### 2. Настройка конфигурации
Скопируйте пример файла конфигурации:
```bash
cp .env.example API.env
```
Откройте `API.env` и укажите ваш API-ключ (например, `CLAUDEHUB_API_KEY`).

### 3. Запуск сервера
```bash
python app.py
```
Сервер запустится на **http://127.0.0.1:8000** (или `http://0.0.0.0:8000` в продакшен-режиме).

---

## ⚙️ Переменные окружения

Конфигурация считывается из файла `API.env` или системных переменных:

| Переменная | Описание | Значение по умолчанию |
|---|---|---|
| `CLAUDEHUB_API_KEY` | API-ключ к агрегатору моделей | — *(обязателен для LLM)* |
| `CLAUDEHUB_BASE_URL` | Endpoint OpenAI-совместимого API | `https://api.claudehub.fun/v1` |
| `GEMINI_API_KEY` | Ключ Google Gemini (резервный) | — |
| `OPENAI_API_KEY` | Ключ OpenAI (резервный) | — |
| `LLM_AUTO_STRATEGY` | Стратегия каскада (`budget` или `premium`) | `budget` |
| `ANON_DAILY_LIMIT` | Суточный лимит планов для гостей | `3` |
| `USER_DAILY_LIMIT` | Суточный лимит планов для аккаунтов | `15` |
| `ADMIN_TOKEN` | Секретный токен для просмотра метрик | — *(если пуст, `/api/admin/stats` закрыт)* |
| `TRUSTED_PROXY` | `1` — учитывать `X-Forwarded-For` за Nginx/Caddy | `0` |
| `PORT` | Порт сервера | `8000` |
| `COACH_DB_PATH` | Путь к файлу SQLite | `./coach_database.sqlite3` |
| `FLASK_DEV` | `1` — запуск дев-сервера вместо Waitress | `0` |

---

## 📡 API Документация

### Аутентификация и пользователи

#### `POST /api/register`
Регистрация нового пользователя.
- **Body:**
  ```json
  {
    "username": "coach_ivan",
    "password": "SecretPassword123"
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "status": "success",
    "username": "coach_ivan",
    "token": "dGhpcy1pcy1hLXNlY3VyZS10b2tlbg..."
  }
  ```

#### `POST /api/login`
Авторизация и получение сессионного токена.
- **Body:** `{ "username": "...", "password": "..." }`
- **Response (200 OK):** `{ "status": "success", "username": "...", "token": "..." }`

#### `GET /api/me`
Проверка текущей авторизации.
- **Headers:** `Authorization: Bearer <TOKEN>`
- **Response (200 OK):** `{ "authenticated": true, "username": "coach_ivan" }`

#### `POST /api/logout`
Отзыв сессионного токена.
- **Headers:** `Authorization: Bearer <TOKEN>`
- **Response (200 OK):** `{ "status": "success" }`

---

### Генерация и работа с планами

#### `POST /generate_plan`
Инициализация асинхронной генерации плана тренировок.
- **Headers (опционально):** `Authorization: Bearer <TOKEN>`
- **Body:**
  ```json
  {
    "height": 195,
    "weight": 88,
    "position": "SG / Атакующий защитник",
    "goal": [
      "🚀 Прыжок и вертикальный взрыв (Vertical Jump)",
      "⚡ Дриблинг и контроль мяча"
    ],
    "days_per_week": 4,
    "injuries": "Тендинопатия связки надколенника (колено прыгуна)",
    "model": "auto"
  }
  ```
- **Response (200 OK):**
  ```json
  {
    "status": "processing",
    "task_id": "87eae0f9-3d1f-4b44-933e-b6a8ce3248aa",
    "poll_token": "pt_9f1a2b3c..."
  }
  ```

#### `GET /plan_status/<task_id>`
Опрос статуса выполнения задачи (Long-polling фронтенда).
- **Headers:** `X-Poll-Token: <POLL_TOKEN>` или `Authorization: Bearer <TOKEN>`
- **Response в процессе (200 OK):**
  ```json
  { "status": "processing", "owner": "coach_ivan" }
  ```
- **Response по готовности (200 OK):**
  ```json
  {
    "status": "success",
    "source": "claudehub-api (claude-sonnet-5)",
    "usage": { "prompt_tokens": 1250, "completion_tokens": 820, "total_tokens": 2070 },
    "data": {
      "summary": "Программа развития взрывной силы и дриблинга с учетом разгрузки надколенника",
      "safety_notes": ["Изометрические удержания в начале для снижения боли", "Контроль приземления"],
      "schedule": [
        {
          "day": "День 1",
          "focus": "Плиометрика низкой ударности и изометрия",
          "exercises": [
            { "name": "Испанский присед (удержание)", "sets": "4", "reps": "45 сек", "notes": "Угол 90°" },
            { "name": "Pound Dribble с утяжеленным мячом", "sets": "3", "reps": "30 сек на руку", "notes": "Максимальная сила удара" }
          ]
        }
      ]
    }
  }
  ```

---

### История тренировок

#### `GET /api/history`
Получение истории созданных планов текущего пользователя.
- **Headers:** `Authorization: Bearer <TOKEN>`
- **Response:** `{ "status": "success", "history": [ ... ] }`

#### `DELETE /api/history/<item_id>`
Удаление записи из истории.
- **Headers:** `Authorization: Bearer <TOKEN>`
- **Response:** `{ "status": "success" }`

#### `DELETE /api/history`
Полная очистка истории пользователя.

---

### Экспорт и статистика

#### `POST /api/pdf`
Генерация PDF-версии плана тренировок.
- **Body:** `{ "payload": { ... }, "apiResult": { ... } }`
- **Response:** Двоичный поток `application/pdf` с заголовком `Content-Disposition: attachment`.

#### `GET /api/admin/stats`
Продуктовая и техническая аналитика системы.
- **Headers:** `X-Admin-Token: <ADMIN_TOKEN>`
- **Response:**
  ```json
  {
    "total_generations": 142,
    "unique_users_today": 18,
    "avg_generation_time_sec": 14.2,
    "tokens_consumed_today": 34820,
    "errors_count": 0
  }
  ```

---

## 📚 База знаний и RAG

Модуль [`rag.py`](file:///c:/Users/User/Desktop/basket_coach_api/rag.py) реализует локальный полнотекстовый поиск по оцифрованной методической литературе:

1. **Оцифровка:** Все 8 книг из папки `knowledge_base/` конвертированы в структурированный Markdown (`knowledge_base_md/`) без шума и колонтитулов.
2. **Инвертированный индекс:** При старте строится словарь по 35 000+ терминам над 4 555 страницами.
3. **Кросс-языковой маппинг:** Русские термины (*прыжок, колено, тендинопатия, взрывная сила, дриблинг*) автоматически связываются с англоязычными аналогами (*vertical jump, patellar tendon, RFD, plyometrics*).
4. **Концентрация контекста:** Поисковик отбирает топ-4 наиболее релевантные страницы и формирует сжатый контекст (до 4 000 символов), обеспечивая тренеру доступ к точным научным протоколам без раздувания стоимости запросов к LLM.

---

## 🧠 Каскад нейросетей и защита токенов

Модуль [`llm_service.py`](file:///c:/Users/User/Desktop/basket_coach_api/llm_service.py) обеспечивает надежность и экономичность:

```
                      [Новый запрос на план]
                                 │
                 ┌───────────────▼───────────────┐
                 │  1-Token Ping Probe (5 сек)   │
                 └───────────────┬───────────────┘
                                 │
               ┌─────────────────┴─────────────────┐
       [Модель ответила]                   [Таймаут / 502 / Ошибка]
               │                                   │
    [Генерация полного плана]           [Мгновенный переход вниз по каскаду]
               │                         (0 токенов потрачено на промпт)
        [Результат JSON]
```

### Иерархии моделей:
- **`budget` (по умолчанию):**
  `qwen3.5-flash` (~0.003 ₽) $\rightarrow$ `deepseek-v4-flash` (~0.0002 ₽) $\rightarrow$ `deepseek-v4-pro` $\rightarrow$ `claude-sonnet-5` $\rightarrow$ `claude-opus-5`
- **`premium`:**
  `claude-opus-5` $\rightarrow$ `claude-sonnet-5` $\rightarrow$ `deepseek-v4-pro` $\rightarrow$ `deepseek-v4-flash` $\rightarrow$ `qwen3.5-flash`

---

## 🔒 Безопасность и хранение данных

- **Хранение паролей (3-Stage Hashing):**
  1. Генерация индивидуальной 16-байтовой соли (`os.urandom(16).hex()`).
  2. Предварительное хэширование SHA-256 binary digest (обход ограничения Bcrypt в 72 байта).
  3. Адаптивное хэширование Bcrypt с фактором трудоемкости `rounds=12`.
- **Сессии:** Токены генерируются через `secrets.token_urlsafe(32)`, в БД сохраняется только SHA-256 хэш токена.
- **База данных:** SQLite в режиме WAL (`Write-Ahead Logging`) с пулом соединений и 100% параметризованными запросами (`?`), исключающими SQL Injection.
- **Защита от DoS и Brute-Force:** Скользящие лимиты (Rate Limiter) на уровне БД, ограничение размера тела запроса (2 МБ), дневные квоты.
- **Заголовки безопасности:** `Content-Security-Policy`, `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`.

---

## 🧪 Тестирование

Проект покрыт автоматическим набором интеграционных тестов:

```bash
# Запуск полного набора тестов
python run_tests.py
```

### Проверяемые сценарии:
1. Валидация 3-этапного алгоритма хэширования паролей (SHA256 $\rightarrow$ Salt $\rightarrow$ Bcrypt).
2. Наличие обязательных HTTP-заголовков безопасности OWASP (CSP, X-Frame-Options, nosniff).
3. Регистрация пользователя и корректность схемы SQLite.
4. Авторизация, проверка отклонения неверных паролей (HTTP 401) и проверка Bearer токенов.
5. Асинхронная генерация тренировочного плана через каскад с зондированием (Ping Probe).
6. Синхронизация и сохранение истории тренировок в SQLite.
7. Безопасность удаления записей из истории (проверка прав владельца).
8. Завершение сессии (Logout) и аннулирование токена.

---

## 🚢 Деплой и продакшен

Подробный гайд по развертыванию доступен в файле [`DEPLOY.md`](file:///c:/Users/User/Desktop/basket_coach_api/DEPLOY.md).

### Быстрый деплой через Docker Compose:
```bash
# 1. Сборка и запуск контейнеров в фоновом режиме
docker compose up -d --build

# 2. Просмотр логов
docker compose logs -f
```

### Запуск через Systemd + Caddy (без Docker):
1. Установите зависимости в `.venv`.
2. Создайте юнит `/etc/systemd/system/coach.service` с запуском `app.py` через `waitress`.
3. Настройте Caddyfile для автоматического HTTPS-проксирования на порт `8000`.

---

## 📄 Лицензия
Проект распространяется для внутреннего и коммерческого использования в спортивной и тренерской деятельности.
