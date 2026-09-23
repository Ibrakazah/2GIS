# 2ГИС · Отзывы «Генацвале» (Уральск) + ответы ИИ

Белый сайт-список отзывов ресторана `Генацвале` с 2ГИС + генерация официально-деловых
вариантов ответа через ИИ (Nemotron Ultra / Qwen через OpenRouter).

- Источник отзывов: https://go.2gis.com/Ql70j → LIVE напрямую с `public-api.reviews.2gis.com`, без базы отзывов
- Сортировка всегда по дате публикации (`date_created`)
- SQLite (`reviews.db`, создается сам) хранит только черновики ИИ
- Новые отзывы определяются сравнением с первым заходом; фоновая автогенерация каждые 15 мин

## Запуск локально

```powershell
pip install -r requirements.txt
copy .env.example .env   # вписать AI_API_KEY
python app.py            # открыть http://localhost:5000 (или run.bat)
```

Переменные `.env` (см. `.env.example`):

| Ключ | Пример |
|---|---|
| `AI_API_KEY` | `sk-or-v1-...` (OpenRouter) |
| `AI_API_BASE` | `https://openrouter.ai/api/v1` |
| `AI_MODEL` | `nvidia/nemotron-3-ultra-550b-a55b:free` |
| `AI_FALLBACK_MODELS` | `nvidia/nemotron-3-ultra-550b-a55b,qwen/qwen3.8-27b` |
| `LIVE_MODE` / `LIVE_CACHE_TTL` | `1` / `60` |
| `AUTO_CHECK_MINUTES` / `AUTO_MAX_PER_RUN` | `15` / `10` |
| `HOST` / `PORT` | `127.0.0.1` / `5000` |

## Деплой

```bash
docker build -t gis-reviews .
docker run -p 5000:5000 --env-file .env gis-reviews
```

Или любой PaaS (`Procfile` уже есть): `gunicorn --bind 0.0.0.0:$PORT --workers 1 --timeout 120 app:app`,
переменные окружения — из `.env`. Секреты только через env, `.env` в git не коммитится.
