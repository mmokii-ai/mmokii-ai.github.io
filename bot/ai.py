"""
AI-модуль iSales Support Bot v5.2
===================================
Поддерживает два бэкенда — переключение через переменную окружения:

  USE_DEEPSEEK=0 (по умолчанию) → Anthropic Claude Haiku
  USE_DEEPSEEK=1                 → OpenRouter / DeepSeek-Chat (бесплатно)

Все публичные функции одинаковы в обоих режимах:
  find_answer_combined, extract_question_essence,
  summarize_support_dialog, refresh_faq_cache,
  get_faq_cache_status, is_faq_cache_fresh,
  FAQ_CACHE_TTL_HOURS, AIBillingError, AIUnavailableError
"""

import os
import json
import logging
import time
from typing import Optional

import httpx

log = logging.getLogger("AI")

# ── Выбор бэкенда ─────────────────────────────────────────────────────────────

USE_DEEPSEEK = os.environ.get("USE_DEEPSEEK", "0") == "1"

# Anthropic (Haiku)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HAIKU_MODEL       = "claude-haiku-3-5-20251001"
ANTHROPIC_URL     = "https://api.anthropic.com/v1/messages"

# OpenRouter (DeepSeek)
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
DEEPSEEK_MODEL     = "deepseek/deepseek-chat"
OPENROUTER_URL     = "https://openrouter.ai/api/v1/chat/completions"

FAQ_CACHE_TTL_HOURS = float(os.environ.get("FAQ_CACHE_TTL_HOURS", "6"))

log.info("=" * 50)
log.info(f"AI BACKEND        : {'DeepSeek (OpenRouter)' if USE_DEEPSEEK else 'Claude Haiku (Anthropic)'}")
if USE_DEEPSEEK:
    log.info(f"OPENROUTER_KEY    : {'✅ ' + OPENROUTER_API_KEY[:8] + '...' if OPENROUTER_API_KEY else '❌ НЕ ЗАДАН'}")
    log.info(f"DEEPSEEK MODEL    : {DEEPSEEK_MODEL}")
else:
    log.info(f"ANTHROPIC_KEY     : {'✅ ' + ANTHROPIC_API_KEY[:8] + '...' if ANTHROPIC_API_KEY else '❌ НЕ ЗАДАН'}")
    log.info(f"HAIKU MODEL       : {HAIKU_MODEL}")
log.info(f"FAQ CACHE TTL     : {FAQ_CACHE_TTL_HOURS} ч")
log.info("=" * 50)


# ── Специальные классы ошибок ─────────────────────────────────────────────────

class AIBillingError(Exception):
    """Нет кредитов (Anthropic) или квота исчерпана. Бот переключится на keyword-поиск."""
    pass

class AIUnavailableError(Exception):
    """AI временно недоступен (сеть, таймаут, ошибка провайдера)."""
    pass


# ── FAQ-кеш ───────────────────────────────────────────────────────────────────

_faq_system_prompt: str = ""
_faq_cache_time: float  = 0.0
_faq_raw: list[dict]    = []

# Флаг ошибки баланса — чтобы не долбить API при каждом вопросе
_billing_error_reported: bool = False


def faq_cache_age_hours() -> float:
    return (time.time() - _faq_cache_time) / 3600


def is_faq_cache_fresh() -> bool:
    return _faq_cache_time > 0 and faq_cache_age_hours() < FAQ_CACHE_TTL_HOURS


def build_faq_system_prompt(faq_rows: list[dict]) -> str:
    if not faq_rows:
        return (
            "Ты — ассистент поддержки мобильного приложения iSales 3.0. "
            "База знаний пока пуста. Отвечай: {\"found\": false, \"answer\": null, \"confidence\": 0}"
        )

    rows_text = "\n".join(
        f"{i+1}. ВОПРОС: {row.get('question_essence') or row.get('question_full', '')}\n"
        f"   ОТВЕТ: {row.get('answer', '')}"
        for i, row in enumerate(faq_rows)
        if row.get("answer", "").strip()
    )

    return f"""Ты — ассистент поддержки мобильного приложения iSales 3.0 для мерчендайзеров.

БАЗА ЗНАНИЙ (FAQ):
{rows_text}

ПРАВИЛА:
1. Найди ответ в базе знаний выше по смыслу вопроса пользователя.
2. Если нашёл — верни JSON: {{"found": true, "answer": "текст ответа дословно из базы", "confidence": 0.0-1.0}}
3. Если не нашёл или уверенность < 0.6 — верни JSON: {{"found": false, "answer": null, "confidence": 0.0}}
4. Отвечай ТОЛЬКО JSON, без markdown, без объяснений.
5. Ответ бери дословно из базы знаний, не придумывай."""


def refresh_faq_cache(faq_rows: list[dict]) -> None:
    global _faq_system_prompt, _faq_cache_time, _faq_raw
    _faq_raw = faq_rows
    _faq_system_prompt = build_faq_system_prompt(faq_rows)
    _faq_cache_time = time.time()
    log.info(
        f"✅ FAQ кеш обновлён: {len(faq_rows)} строк, "
        f"prompt ≈ {len(_faq_system_prompt)} символов, "
        f"TTL {FAQ_CACHE_TTL_HOURS} ч"
    )


def get_faq_cache_status() -> dict:
    return {
        "rows": len(_faq_raw),
        "age_hours": round(faq_cache_age_hours(), 2),
        "ttl_hours": FAQ_CACHE_TTL_HOURS,
        "fresh": is_faq_cache_fresh(),
        "system_prompt_chars": len(_faq_system_prompt),
        "next_refresh_in_hours": round(max(0, FAQ_CACHE_TTL_HOURS - faq_cache_age_hours()), 2),
        "billing_error": _billing_error_reported,
        "backend": "deepseek" if USE_DEEPSEEK else "haiku",
    }


# ══════════════════════════════════════════════════════════════════════════════
# БЭКЕНД: ANTHROPIC CLAUDE HAIKU
# ══════════════════════════════════════════════════════════════════════════════

async def _call_haiku(system: str, user: str, max_tokens: int = 300) -> str:
    global _billing_error_reported

    if not ANTHROPIC_API_KEY:
        raise AIUnavailableError("ANTHROPIC_API_KEY не задан")

    if _billing_error_reported:
        raise AIBillingError("Баланс исчерпан (кешированная ошибка)")

    payload = {
        "model": HAIKU_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    log.debug(f"→ Haiku | {user[:80]}")

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(ANTHROPIC_URL, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise AIUnavailableError("Таймаут запроса к Anthropic")
    except Exception as e:
        raise AIUnavailableError(f"Сетевая ошибка Anthropic: {e}")

    if r.status_code == 200:
        _billing_error_reported = False
        result = r.json()["content"][0]["text"].strip()
        log.debug(f"← Haiku: {result[:150]}")
        return result

    try:
        err_body = r.json()
        err_msg  = err_body.get("error", {}).get("message", r.text)
    except Exception:
        err_msg = r.text[:200]

    if r.status_code == 400 and "credit balance" in err_msg.lower():
        _billing_error_reported = True
        log.error(
            "❌ НЕДОСТАТОЧНО КРЕДИТОВ ANTHROPIC!\n"
            "   Пополните баланс: console.anthropic.com → Plans & Billing\n"
            "   Или переключитесь на DeepSeek: USE_DEEPSEEK=1"
        )
        raise AIBillingError(err_msg)

    if r.status_code in (529, 500):
        raise AIUnavailableError(f"Anthropic перегружен: {r.status_code}")

    if r.status_code == 401:
        log.error("❌ Неверный ANTHROPIC_API_KEY")
        raise AIUnavailableError("Неверный API ключ Anthropic (401)")

    log.error(f"❌ Haiku HTTP {r.status_code}: {err_msg[:200]}")
    raise AIUnavailableError(f"Anthropic API error {r.status_code}")


# ══════════════════════════════════════════════════════════════════════════════
# БЭКЕНД: OPENROUTER / DEEPSEEK
# ══════════════════════════════════════════════════════════════════════════════

async def _call_deepseek(system: str, user: str, max_tokens: int = 300) -> str:
    global _billing_error_reported

    if not OPENROUTER_API_KEY:
        raise AIUnavailableError("OPENROUTER_API_KEY не задан")

    if _billing_error_reported:
        raise AIBillingError("Квота OpenRouter исчерпана (кешированная ошибка)")

    payload = {
        "model": DEEPSEEK_MODEL,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": user},
        ],
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": "https://localhost",
        "X-OpenRouter-Title": "isales-support-bot",
        "Content-Type": "application/json",
    }

    log.debug(f"→ DeepSeek | {user[:80]}")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(OPENROUTER_URL, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise AIUnavailableError("Таймаут запроса к OpenRouter")
    except Exception as e:
        raise AIUnavailableError(f"Сетевая ошибка OpenRouter: {e}")

    log.debug(f"OpenRouter status={r.status_code}")

    if r.status_code == 200:
        data = r.json()
        if "choices" in data and data["choices"]:
            _billing_error_reported = False
            result = data["choices"][0]["message"]["content"].strip()
            log.debug(f"← DeepSeek: {result[:150]}")
            return result
        if "error" in data:
            err = data["error"]
            log.error(f"❌ OpenRouter error: {err}")
            # Квота / billing
            if isinstance(err, dict) and err.get("code") in (402, "insufficient_quota"):
                _billing_error_reported = True
                raise AIBillingError(str(err))
            raise AIUnavailableError(str(err))
        raise AIUnavailableError("OpenRouter: пустой ответ без choices и error")

    try:
        err_body = r.json()
        err_msg  = str(err_body.get("error", r.text))
    except Exception:
        err_msg = r.text[:200]

    if r.status_code == 402:
        _billing_error_reported = True
        log.error("❌ OpenRouter: недостаточно кредитов (402)")
        raise AIBillingError(err_msg)

    if r.status_code == 401:
        log.error("❌ Неверный OPENROUTER_API_KEY")
        raise AIUnavailableError("Неверный API ключ OpenRouter (401)")

    if r.status_code in (429, 503):
        raise AIUnavailableError(f"OpenRouter перегружен: {r.status_code}")

    log.error(f"❌ DeepSeek HTTP {r.status_code}: {err_msg[:200]}")
    raise AIUnavailableError(f"OpenRouter API error {r.status_code}")


# ── Единая точка вызова (выбирает бэкенд автоматически) ──────────────────────

async def _call_ai(system: str, user: str, max_tokens: int = 300) -> str:
    """Вызывает нужный бэкенд в зависимости от USE_DEEPSEEK."""
    if USE_DEEPSEEK:
        return await _call_deepseek(system, user, max_tokens)
    else:
        return await _call_haiku(system, user, max_tokens)


# ══════════════════════════════════════════════════════════════════════════════
# ПУБЛИЧНЫЕ ФУНКЦИИ (одинаковые для обоих бэкендов)
# ══════════════════════════════════════════════════════════════════════════════

async def find_answer_in_faq(question: str) -> tuple[Optional[str], float]:
    """
    Ищет ответ в FAQ через AI (предзагруженный system prompt).
    Возвращает (answer, confidence).
    Пробрасывает AIBillingError / AIUnavailableError — бот обработает.
    """
    if not _faq_system_prompt:
        log.warning("FAQ кеш пуст")
        return None, 0.0

    log.info(f"AI поиск ({len(_faq_raw)} строк в кеше): {question[:60]}")

    raw = await _call_ai(_faq_system_prompt, question, max_tokens=400)

    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.error(f"❌ JSON parse: {raw[:200]}")
        return None, 0.0

    found      = data.get("found", False)
    answer     = data.get("answer")
    confidence = float(data.get("confidence", 0.0))

    log.info(f"  found={found}, confidence={confidence:.2f}")

    if found and answer and confidence >= 0.6:
        log.info(f"✅ AI ответ: {answer[:80]}")
        return answer, confidence

    return None, confidence


async def find_answer_combined(questions: list[str]) -> tuple[Optional[str], float]:
    """Поиск по нескольким вопросам сразу (диалог уточнения)."""
    if not questions:
        return None, 0.0
    if len(questions) == 1:
        return await find_answer_in_faq(questions[0])

    combined = "Пользователь задал несколько связанных вопросов:\n" + "\n".join(
        f"Сообщение {i+1}: {q}" for i, q in enumerate(questions)
    )
    return await find_answer_in_faq(combined)


async def extract_question_essence(text: str) -> str:
    """Суть вопроса для записи в тикет. При ошибке — возвращает исходный текст."""
    try:
        system = (
            "Извлеки суть вопроса в 1 предложение. "
            "Только техническая суть без эмоций. Без кавычек."
        )
        return await _call_ai(system, text, max_tokens=100)
    except (AIBillingError, AIUnavailableError):
        return text[:200]
    except Exception as e:
        log.error(f"❌ extract_question_essence: {e}")
        return text[:200]


async def summarize_support_dialog(original_q: str, essence: str,
                                    messages: list[str]) -> Optional[dict]:
    """Q&A из переписки специалиста для записи в FAQ. При ошибке — None."""
    if not messages:
        return None
    dialog = "\n".join(f"Специалист: {m}" for m in messages)
    system = (
        "Сформируй запись для базы знаний. "
        "Только JSON: {\"question\": \"...\", \"answer\": \"...\"} или null."
    )
    try:
        raw = await _call_ai(
            system,
            f"Вопрос: {original_q}\nСуть: {essence}\n\nОтветы:\n{dialog}",
            max_tokens=500
        )
        raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        if raw.lower() == "null":
            return None
        data = json.loads(raw)
        if data.get("question") and data.get("answer"):
            return data
    except (AIBillingError, AIUnavailableError) as e:
        log.warning(f"summarize_support_dialog недоступен: {e}")
    except Exception as e:
        log.error(f"❌ summarize_support_dialog: {e}")
    return None
