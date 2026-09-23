"""Генерация ответов на отзывы через ИИ (OpenAI-совместимый API).
Сейчас подключен Groq: AI_API_BASE=https://api.groq.com/openai/v1
Модель по умолчанию: openai/gpt-oss-20b (быстрая). Качество выше: openai/gpt-oss-120b.
Nemotron Ultra подключим позже — достаточно будет сменить AI_MODEL в .env на ID модели.

Настройки берутся из файла .env:
  AI_API_KEY=...
  AI_API_BASE=https://api.groq.com/openai/v1
  AI_MODEL=openai/gpt-oss-20b

Публикация ответа в сам 2ГИС — отдельным этапом (кнопка-заглушка уже есть в интерфейсе).
"""
import os
import time
import sqlite3

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_API_BASE = os.getenv("AI_API_BASE", "https://api.openai.com/v1").strip().rstrip("/")
AI_MODEL = os.getenv("AI_MODEL", "gpt-4o-mini").strip()
AI_FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "AI_FALLBACK_MODELS", "nvidia/nemotron-3-ultra-550b-a55b,qwen/qwen3.8-27b").split(",") if m.strip()]

BRANCH_NAME_DEFAULT = "Генацвале, ресторан"

SYSTEM_PROMPT = (
    "Ты — администрация ресторана грузинской кухни. Отвечай на отзыв гостя строго "
    "в официально-деловом стиле на русском языке, на «Вы». "
    "Структура: обращение, благодарность за обратную связь, позиция по сути вопроса, "
    "приглашение посетить ресторан вновь. Подпись: «С уважением, администрация ресторана «Генацвале»». "
    "Без эмодзи, без разговорных слов, 2-4 предложения."
)


def build_user_prompt(author, rating, text, branch_name=BRANCH_NAME_DEFAULT):
    return (
        f"Ресторан: {branch_name}\n"
        f"Автор отзыва: {author}\n"
        f"Оценка: {rating}/5\n"
        f"Текст отзыва: {text[:1500] or '(без текста)'}\n\n"
        f"Составь вариант ответа от лица заведения."
    )


def stub_reply(author, rating, text):
    """Официально-деловой шаблон (заглушка)."""
    sign = "С уважением, администрация ресторана «Генацвале»."
    try:
        r = int(rating or 0)
    except Exception:
        r = 0
    if r >= 4:
        return (f"Уважаемый гость! Благодарим Вас за высокую оценку и теплый отзыв. "
                f"Будем рады видеть Вас снова. {sign}")
    if r == 3:
        return (f"Уважаемый гость! Благодарим Вас за отзыв. Ваши замечания переданы руководству. {sign}")
    return (f"Уважаемый гость! Приносим искренние извинения за доставленные неудобства. "
            f"Ваше обращение принято в работу. {sign}")


PROMPT_V = 2  # версия промпта: черновики со старой версией помечаются "старый стиль"


def call_openai_compatible(system_prompt, user_prompt, timeout=120, temperature=0.7, max_tokens=2048,
                           model=None):
    """Вызов OpenAI-совместимого /chat/completions. Возвращает текст."""
    import requests
    use_model = model or AI_MODEL
    url = f"{AI_API_BASE}/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    if "openrouter" in AI_API_BASE:
        headers["HTTP-Referer"] = "http://localhost:5000"
        headers["X-Title"] = "2GIS Reviews"
    payload = {
        "model": use_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    # gpt-oss модели Groq по умолчанию тратят токены на reasoning и обрезают ответ:
    # урезаем рассуждения чтобы весь бюджет уходил в текст вариантов
    if "gpt-oss" in use_model:
        payload["reasoning_effort"] = "low"
    if "openrouter" in AI_API_BASE:
        # reasoning-моделям урезаем раздумья: весь бюджет и время — в текст ответа, не в think
        payload["reasoning"] = {"effort": "low", "exclude": True}
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    finish = (data.get("choices") or [{}])[0].get("finish_reason", "?")
    text = data["choices"][0]["message"]["content"].strip()
    # reasoning-модели (Nemotron и др.) кладут ход мыслей в <think> — вырезаем, нужен только ответ
    import re as _re
    text = _re.sub(r"<think>.*?</think>", "", text, flags=_re.S)
    text = _re.sub(r"<think>.*$", "", text, flags=_re.S).strip()
    print(f"[AI] model={use_model} finish={finish} chars={len(text)}", flush=True)
    call_openai_compatible.last_finish = finish
    return text


call_openai_compatible.last_finish = "?"


def _looks_complete(variants, n=3):
    """Все варианты достаточной длины и заканчиваются знаком препинания/подписью."""
    if len(variants) < n:
        return False
    for v in variants:
        t = (v or "").strip()
        if len(t) < 40:
            return False
        if t[-1].isalnum():  # обрыв на полуслове
            return False
    return True


def generate_ai_reply(author, rating, text, branch_name=BRANCH_NAME_DEFAULT):
    """Одиночный ответ (совместимость). Новый код использует generate_ai_variants."""
    variants, source = generate_ai_variants(author, rating, text, branch_name, n=3)
    return (variants[0] if variants else stub_reply(author, rating, text)), source


VARIANTS_SYSTEM = (
    "Ты — администрация ресторана грузинской кухни «Генацвале». "
    "Составь РОВНО 3 варианта официального делового ответа на отзыв гостя на русском языке, "
    "обращение строго на «Вы». Без эмодзи, без разговорных и шутливых формулировок. "
    "Вариант 1 — официальный благодарственный (или извинительный при негативе), 2-3 предложения. "
    "Вариант 2 — краткий деловой (1-2 предложения). "
    "Вариант 3 — развернутый деловой с приглашением посетить ресторан вновь. "
    "Каждый вариант завершай подписью: «С уважением, администрация ресторана «Генацвале»». "
    "Формат строго:\n1. <текст>\n2. <текст>\n3. <текст>\nБез лишнего текста."
)


def variants_system(n=3, rating=0):
    """Системный промпт под нужное число вариантов и оценку.
    3-5 звезд — почти одинаковые нейтральные шаблоны. 1-2 звезды — индивидуальные по сути отзыва."""
    try:
        r = int(rating or 0)
    except Exception:
        r = 0
    word = {1: "РОВНО 1 вариант", 2: "РОВНО 2 варианта", 3: "РОВНО 3 варианта"}.get(n, f"РОВНО {n} вариантов")
    nums = "\n".join(f"{i}. <текст>" for i in range(1, n + 1))
    base = (
        "Ты — администрация ресторана грузинской кухни «Генацвале». "
        f"Составь {word} ответа на отзыв гостя на русском языке. "
        "Жесткие правила тона: обращение строго на «Вы», официально-деловой нейтральный стиль. "
        "ЗАПРЕЩЕНЫ: эмодзи, слова «вау», «ухты», «круто», «супер», «обнимаем», фразы про «всю команду», "
        "восклицательные восторги, фамильярность. Спокойный ровный тон. "
        "Каждый вариант завершай подписью: «С уважением, администрация ресторана «Генацвале»». "
        f"Формат строго:\n{nums}\nБез лишнего текста. "
    )
    if r >= 4:
        return base + ("Содержание почти шаблонное, варьируй минимально: поблагодари за высокую оценку "
                       "и отзыв, пригласи посетить ресторан вновь. Не выдумывай детали.")
    if r == 3:
        return base + ("Содержание шаблонное: поблагодари за отзыв, сухо отметь, "
                       "что замечания переданы руководству. Без эмоций.")
    return base + ("Отзыв негативный: каждый вариант ИНДИВИДУАЛЕН — извинись, сошлись на конкретные "
                   "детали из текста отзыва, укажи, что обращение передано руководству и будут приняты меры, "
                   "пригласи дать ресторану второй шанс. Все равно сухо и официально, без заискивания.")


def parse_variants(text, n=3):
    import re
    parts = re.split(r"(?m)^\s*[123]\s*[).\-.]\s*", text or "")
    out = [p.strip() for p in parts if p and p.strip()]
    # если модель вернула сплошняком — режем по строкам как запасной вариант
    if len(out) < n:
        lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
        if len(lines) >= n:
            out = lines[:n]
    return out[:n]


def stub_variants(author, rating, text):
    """3 официально-деловых шаблона пока ИИ недоступен."""
    try:
        r = int(rating or 0)
    except Exception:
        r = 0
    sign = "С уважением, администрация ресторана «Генацвале»."
    if r >= 4:
        return [
            f"Уважаемый гость! Благодарим Вас за высокую оценку и теплый отзыв о нашем ресторане. "
            f"Будем рады видеть Вас снова. {sign}",
            f"Спасибо за Ваш отзыв и высокую оценку. {sign}",
            f"Уважаемый гость! Нам очень приятно, что Вы остались довольны визитом в «Генацвале». "
            f"Ваши слова — лучшая награда для нашей команды. С нетерпением ждем Вас вновь. {sign}",
        ]
    if r == 3:
        return [
            f"Уважаемый гость! Благодарим Вас за отзыв и объективную оценку. "
            f"Ваши замечания переданы руководству, мы примем меры для улучшения сервиса. {sign}",
            f"Спасибо за обратную связь. Ваши замечания учтены. {sign}",
            f"Уважаемый гость! Спасибо, что уделили время отзыву. Нам важно понимать, "
            f"что мы можем улучшить, и Ваши пожелания уже в работе. Будем признательны "
            f"за возможность принять Вас вновь. {sign}",
        ]
    return [
        f"Уважаемый гость! Приносим искренние извинения за доставленные неудобства. "
        f"Ваше обращение передано руководству, по нему будет проведена проверка. {sign}",
        f"Приносим извинения за ситуацию. Ваше обращение принято в работу. {sign}",
        f"Уважаемый гость! Нам искренне жаль, что Ваш визит оставил негативное впечатление. "
        f"Мы детально разберем описанную Вами ситуацию с ответственной командой "
        f"и будем признательны за возможность исправить впечатление при следующем визите. {sign}",
    ]


def _short_model(model_id):
    m = (model_id or "").lower()
    if "ultra-550b" in m:
        return "ultra-free" if "free" in m else "ultra"
    if "qwen" in m:
        return "qwen"
    if "gpt-oss-120b" in m:
        return "gpt-oss-120b"
    if "gpt-oss-20b" in m:
        return "gpt-oss-20b"
    return (model_id or "?").split("/")[-1][:24]


def _gen_n(models, user_prompt, n, temperature, rating=0):
    """ОДИН запрос на модель: все n вариантов за один заход. Идем по цепочке пока не получим полные."""
    last_err, last_vs, used = None, [], ""
    for mi, model in enumerate(models):
        try:
            if mi > 0:
                print(f"[AI] fallback -> {model}", flush=True)
                time.sleep(1.0)
            raw = call_openai_compatible(variants_system(n, rating), user_prompt, timeout=120,
                                         temperature=temperature, model=model)
            vs = parse_variants(raw, n=n)
            used = model
            if _looks_complete(vs, n):
                return vs, None, model
            last_err, last_vs = "incomplete variants", vs
            print(f"[AI] {model} incomplete ({len('/'.join(vs))} chars), next...", flush=True)
        except Exception as e:
            print(f"[AI] {model} failed: {str(e)[:200]}", flush=True)
            last_err = e
    return last_vs, last_err, used


def generate_ai_variants(author, rating, text, branch_name=BRANCH_NAME_DEFAULT, n=3,
                         previous=None, temperature=0.7):
    """ОДИН запрос: все 3 варианта за один заход в одну модель (никаких отдельных запросов).
    previous — уже показанные тексты (для 'Перегенерировать'): просят другие формулировки.
    Возвращает (variants, source)."""
    user_prompt = build_user_prompt(author, rating, text, branch_name)
    if previous:
        prev = "\n".join(f"- {str(p)[:400]}" for p in previous if p)
        user_prompt += ("\n\nВажно: ниже — уже предложенные варианты, НЕ повторяйте их, "
                        "придумайте принципиально другие формулировки:\n" + prev)
        temperature = max(temperature, 1.0)
    if not AI_API_KEY:
        time.sleep(0.4)
        return stub_variants(author, rating, text)[:n], "stub"
    primary = [AI_MODEL] + [m for m in AI_FALLBACK_MODELS if m != AI_MODEL]
    try:
        rr = int(rating or 0)
    except Exception:
        rr = 0
    # шаблоны должны быть стабильными: низкая температура; индивидуальность только для 1-2 звезд
    if not previous:
        temperature = min(temperature, 0.3) if rr >= 3 else max(temperature, 0.7)
    variants, err, used = _gen_n(primary, user_prompt, n, temperature, rr)
    if not _looks_complete(variants, n):
        # один повтор тем же составом (оборвало по длине) — и всё, больше не дергаем
        print("[AI] incomplete, one retry on primary...", flush=True)
        try:
            raw = call_openai_compatible(variants_system(n, rr), user_prompt, timeout=120,
                                         temperature=1.0, model=AI_MODEL)
            v2 = parse_variants(raw, n=n)
            if _looks_complete(v2, n):
                variants, err, used = v2, None, AI_MODEL
        except Exception as e:
            err = err or e
    while len(variants) < n:
        variants += stub_variants(author, rating, text)[len(variants):n]
    variants = variants[:n]
    if _looks_complete(variants, n) and used:
        return variants, _short_model(used)
    fb = stub_variants(author, rating, text)[:n]
    fb[0] = f"{fb[0]}\n\n[ИИ API недоступен: {err}]"
    return fb, "stub_fallback"


def pack_variants(variants):
    import json as _json
    return _json.dumps(list(variants), ensure_ascii=False)


def unpack_variants(draft_text):
    """draft_text может быть JSON-массивом (новый формат) или plain-текстом (старый)."""
    import json as _json
    t = (draft_text or "").strip()
    if t.startswith("["):
        try:
            v = _json.loads(t)
            if isinstance(v, list) and v:
                return [str(x) for x in v]
        except Exception:
            pass
    return [t] if t else []


# --- хранение черновиков в SQLite (та же reviews.db) ---

def ensure_ai_table(db_path="reviews.db"):
    conn = sqlite3.connect(db_path)
    conn.execute("""
    CREATE TABLE IF NOT EXISTS ai_drafts (
        review_id TEXT PRIMARY KEY,
        draft_text TEXT,
        source TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def save_draft(db_path, review_id, draft_text, source):
    from datetime import datetime, timezone
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT OR REPLACE INTO ai_drafts (review_id, draft_text, source, created_at) VALUES (?,?,?,?)",
        (str(review_id), draft_text, source, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()


def get_drafts_map(db_path, review_ids):
    if not review_ids:
        return {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = ",".join("?" for _ in review_ids)
    rows = conn.execute(f"SELECT * FROM ai_drafts WHERE review_id IN ({q})", list(map(str, review_ids))).fetchall()
    conn.close()
    return {r["review_id"]: dict(r) for r in rows}


# --- автогенерация для НОВЫХ отзывов (появившихся после включения фичи) ---

AUTO_GEN_ENABLED = os.getenv("AUTO_GEN_ENABLED", "1").strip() not in ("0", "false", "no")
AUTO_CHECK_MINUTES = int(os.getenv("AUTO_CHECK_MINUTES", "15") or 15)
AUTO_MAX_PER_RUN = int(os.getenv("AUTO_MAX_PER_RUN", "10") or 10)


def ensure_auto_tables(db_path="reviews.db"):
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE IF NOT EXISTS known_reviews (review_id TEXT PRIMARY KEY, first_seen TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS auto_state (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()
    conn.close()
    ensure_ai_table(db_path)


def get_state(db_path, key, default=""):
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM auto_state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else default


def set_state(db_path, key, value):
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT OR REPLACE INTO auto_state (key, value) VALUES (?,?)", (key, str(value)))
    conn.commit()
    conn.close()


def mark_all_current_as_known(db_path="reviews.db"):
    """Базовая линия: все отзывы, что уже есть в базе = 'старые'. Вызывать один раз при включении."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT id FROM reviews").fetchall()
    conn.executemany("INSERT OR IGNORE INTO known_reviews (review_id, first_seen) VALUES (?,?)",
                     [(r[0], now) for r in rows])
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM known_reviews").fetchone()[0]
    conn.close()
    set_state(db_path, "baseline_at", now)
    return n


def get_new_reviews(db_path="reviews.db", limit=20):
    """Отзывы, которых еще нет в known_reviews = 'новые'. Сначала самые свежие по дате публикации."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT r.* FROM reviews r
        LEFT JOIN known_reviews k ON k.review_id = r.id
        WHERE k.review_id IS NULL
        ORDER BY r.date_created DESC LIMIT ?
    """, (limit,)).fetchall()
    conn.close()
    return [dict(x) for x in rows]


def mark_known(db_path, review_ids):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    conn.executemany("INSERT OR IGNORE INTO known_reviews (review_id, first_seen) VALUES (?,?)",
                     [(str(x), now) for x in review_ids])
    conn.commit()
    conn.close()


def auto_generate_new(db_path="reviews.db", branch_name=BRANCH_NAME_DEFAULT,
                      max_n=None, sleep_s=2.0, progress_cb=None):
    """Генерирует черновики (по 3 варианта) только для новых отзывов. Возвращает список {review_id, source}."""
    from parser_2gis import DB_PATH as _DB
    db = db_path or _DB
    ensure_auto_tables(db)
    if get_state(db, "auto_gen_enabled", "1" if AUTO_GEN_ENABLED else "0") in ("0", "false", "no"):
        return []
    limit = max_n or AUTO_MAX_PER_RUN
    newbies = get_new_reviews(db, limit=limit)
    out = []
    for i, r in enumerate(newbies):
        variants, source = generate_ai_variants(
            r.get("author") or "Гость", r.get("rating") or 0, r.get("text") or "", branch_name)
        save_draft(db, str(r["id"]), pack_variants(variants), source)
        mark_known(db, [str(r["id"])])
        out.append({"review_id": str(r["id"]), "source": source})
        if progress_cb:
            progress_cb(i + 1, len(newbies), str(r["id"]), source)
        if sleep_s and i < len(newbies) - 1:
            time.sleep(sleep_s)
    if out:
        from datetime import datetime, timezone
        set_state(db, "last_auto_gen", datetime.now(timezone.utc).isoformat())
        set_state(db, "last_auto_count", str(len(out)))
    return out
