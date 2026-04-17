"""
iSales Support Bot v5.1
========================
Изменения:
- feedback resolved=true  → уведомление поддержке + добавление в FAQ + закрытие тикета
- feedback resolved=false → уведомление поддержке (можно писать ещё)
- user /widget/close      → пользователь закрыл тикет сам
- поддержка видит статус тикета после resolve (только "Закрыть", не "Ответить")
"""

import os
import json
import logging
import asyncio
import uuid
from datetime import datetime
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)-8s | %(name)-12s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("BOT")

# ── Конфиг ────────────────────────────────────────────────────
USE_AI               = os.environ.get("USE_AI", "0") == "1"
MAX_CLARIFY_ATTEMPTS = int(os.environ.get("MAX_CLARIFY_ATTEMPTS", "3"))
SUPPORT_PASSWORD     = os.environ.get("SUPPORT_PASSWORD", "support123")

log.info("=" * 60)
log.info("iSales Support Bot v5.1")
log.info(f"USE_AI={USE_AI}  CLARIFY={MAX_CLARIFY_ATTEMPTS}  PWD={'OK' if SUPPORT_PASSWORD else '!'}")
log.info("=" * 60)

# ── Модули ────────────────────────────────────────────────────
try:
    from sheets import SheetsClient
    sheets = SheetsClient()
    log.info("SHEETS: ✅")
except Exception as e:
    log.error(f"SHEETS: ❌ {e}")
    sheets = None

ai_available = False
AIBillingError = AIUnavailableError = Exception
if USE_AI:
    try:
        from ai import (find_answer_combined, extract_question_essence,
                        summarize_support_dialog, refresh_faq_cache,
                        get_faq_cache_status, is_faq_cache_fresh,
                        FAQ_CACHE_TTL_HOURS, AIBillingError, AIUnavailableError)
        ai_available = True
        log.info("AI: ✅")
    except Exception as e:
        log.error(f"AI: ❌ {e}")

# ── Состояние ─────────────────────────────────────────────────
widget_sessions: dict[str, dict] = {}   # session_id → session
pending_tickets: dict[str, str]  = {}   # ticket_id  → session_id
support_ws_list: list[WebSocket] = []
user_ws_map: dict[str, WebSocket] = {}  # session_id → ws

app = FastAPI(title="iSales Bot v5.1")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception:
    pass

# ── Максаккаунт закомментирован ───────────────────────────────
# MAX_BOT_TOKEN = os.environ.get("MAX_BOT_TOKEN","")
# MAX_API_BASE  = os.environ.get("MAX_API_BASE","https://platform-api.max.ru")
# WEBHOOK_URL   = os.environ.get("WEBHOOK_URL","")


# ══════════════════════════════════════════════════════════════
# FAQ КЕШ
# ══════════════════════════════════════════════════════════════

async def maybe_refresh_faq():
    if not USE_AI or not ai_available or not sheets:
        return
    try:
        from ai import is_faq_cache_fresh
        if is_faq_cache_fresh():
            return
    except Exception:
        return
    try:
        rows = sheets.get_faq()
        refresh_faq_cache(rows)
    except Exception as e:
        log.error(f"FAQ refresh: {e}")

async def faq_refresh_loop():
    if not USE_AI or not ai_available:
        return
    while True:
        await asyncio.sleep(FAQ_CACHE_TTL_HOURS * 3600)
        await maybe_refresh_faq()


# ══════════════════════════════════════════════════════════════
# ПОИСК
# ══════════════════════════════════════════════════════════════

def search_keyword(questions: list[str]) -> Optional[str]:
    if not sheets:
        return None
    try:
        try:
            from ai import _faq_raw
            rows = _faq_raw if _faq_raw else sheets.get_faq()
        except Exception:
            rows = sheets.get_faq()
    except Exception as e:
        log.error(f"FAQ read: {e}")
        return None

    combined = " ".join(questions).lower()
    best, best_score = None, 0.0
    for row in rows:
        answer = row.get("answer", "")
        if not answer:
            continue
        for field in ["question_essence", "question_full"]:
            pattern = str(row.get(field, "")).lower().strip()
            words = [w for w in pattern.split() if len(w) > 3]
            if not words:
                continue
            score = sum(1 for w in words if w in combined) / len(words)
            if score > best_score and score >= 0.6:
                best_score, best = score, answer
    return best

async def find_answer(questions: list[str]) -> tuple[Optional[str], str]:
    answer = search_keyword(questions)
    if answer:
        return answer, "keyword"
    if USE_AI and ai_available:
        await maybe_refresh_faq()
        try:
            answer, _ = await find_answer_combined(questions)
            if answer:
                return answer, "ai"
        except (AIBillingError, AIUnavailableError) as e:
            log.warning(f"AI unavailable: {e}")
        except Exception as e:
            log.error(f"AI error: {e}")
    return None, "none"

def new_ticket_id() -> str:
    if sheets:
        try:
            return f"ISALES-{sheets.increment_counter():04d}"
        except Exception:
            pass
    import time
    return f"ISALES-{int(time.time()) % 9999:04d}"


# ══════════════════════════════════════════════════════════════
# WEBSOCKET HELPERS
# ══════════════════════════════════════════════════════════════

async def push_support(event: dict):
    """Отправить событие всем подключённым специалистам."""
    dead = []
    for ws in support_ws_list:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in support_ws_list:
            support_ws_list.remove(ws)

async def push_user(session_id: str, event: dict):
    """Отправить событие конкретному пользователю."""
    ws = user_ws_map.get(session_id)
    if ws:
        try:
            await ws.send_json(event)
        except Exception:
            user_ws_map.pop(session_id, None)


def _ticket_event(session: dict, event_type: str) -> dict:
    """Сформировать событие тикета для WebSocket."""
    return {
        "type":       event_type,
        "ticket_id":  session.get("ticket_id"),
        "session_id": session.get("session_id"),
        "user_name":  session.get("user_name", "Пользователь"),
        "questions":  session.get("questions", []),
        "state":      session.get("state"),
        "created_at": session.get("created_at"),
        "messages":   session.get("chat_history", []),
    }


# ══════════════════════════════════════════════════════════════
# FAQ — СОХРАНЕНИЕ ОТВЕТА СПЕЦИАЛИСТА
# ══════════════════════════════════════════════════════════════

async def save_to_faq(session: dict):
    """Добавить Q&A в базу знаний после успешного закрытия тикета."""
    support_msgs = session.get("support_messages", [])
    if not support_msgs or not sheets:
        return
    questions = session.get("questions", [])
    q_text    = " | ".join(questions[:2]) if questions else "вопрос пользователя"
    a_text    = support_msgs[-1]

    if USE_AI and ai_available:
        try:
            original_q = questions[0] if questions else ""
            essence    = " ".join(questions)
            qa = await summarize_support_dialog(original_q, essence, support_msgs)
            if qa:
                q_text = qa["question"]
                a_text = qa["answer"]
        except Exception as e:
            log.error(f"summarize Q&A: {e}")

    try:
        sheets.add_faq(q_text, a_text, source="support")
        log.info(f"FAQ добавлен: {q_text[:60]}")
        await maybe_refresh_faq()
    except Exception as e:
        log.error(f"add_faq: {e}")


# ══════════════════════════════════════════════════════════════
# WEBSOCKET ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.websocket("/ws/user/{session_id}")
async def ws_user(ws: WebSocket, session_id: str):
    await ws.accept()
    user_ws_map[session_id] = ws
    log.info(f"WS user connected: {session_id[:8]}")
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        user_ws_map.pop(session_id, None)

@app.websocket("/ws/support")
async def ws_support(ws: WebSocket):
    await ws.accept()
    support_ws_list.append(ws)
    log.info(f"WS support connected, total={len(support_ws_list)}")
    # Отдаём текущие открытые тикеты
    open_sessions = [
        s for s in widget_sessions.values()
        if s.get("state") in ("waiting_support", "support_replied")
    ]
    if open_sessions:
        await ws.send_json({
            "type":    "init",
            "tickets": [_ticket_event(s, "init") for s in open_sessions],
        })
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        if ws in support_ws_list:
            support_ws_list.remove(ws)


# ══════════════════════════════════════════════════════════════
# WIDGET API — ПОЛЬЗОВАТЕЛЬ
# ══════════════════════════════════════════════════════════════

class UserSessionReq(BaseModel):
    user_name: str = "Пользователь"

class UserMsgReq(BaseModel):
    session_id: str
    message:    str
    user_name:  str = "Пользователь"

class FeedbackReq(BaseModel):
    session_id: str
    ticket_id:  str
    resolved:   bool

class CloseReq(BaseModel):
    session_id: str
    ticket_id:  str


@app.post("/widget/session")
async def create_session(req: UserSessionReq):
    sid = str(uuid.uuid4())
    widget_sessions[sid] = {
        "session_id":    sid,
        "user_name":     req.user_name,
        "state":         "idle",
        "questions":     [],
        "attempt":       0,
        "chat_history":  [],
        "support_messages": [],
        "created_at":    datetime.now().isoformat(),
    }
    return {
        "session_id": sid,
        "greeting":   "👋 Здравствуйте! Чем могу помочь?",
    }


@app.post("/widget/message")
async def user_message(req: UserMsgReq):
    session = widget_sessions.get(req.session_id)
    if not session:
        session = {
            "session_id": req.session_id, "user_name": req.user_name,
            "state": "idle", "questions": [], "attempt": 0,
            "chat_history": [], "support_messages": [],
            "created_at": datetime.now().isoformat(),
        }
        widget_sessions[req.session_id] = session

    text  = req.message.strip()
    state = session.get("state", "idle")
    session["user_name"] = req.user_name

    def _append_history(role, msg_text):
        now = datetime.now()
        session.setdefault("chat_history", []).append({
            "role": role, "text": msg_text,
            "time": now.strftime("%H:%M"),
            "time_iso": now.isoformat(),
        })

    _append_history("user", text)

    # Уже ждёт специалиста — пересылаем специалисту
    if state in ("waiting_support", "support_replied"):
        await push_support({
            "type":       "user_message",
            "session_id": req.session_id,
            "ticket_id":  session.get("ticket_id"),
            "user_name":  req.user_name,
            "text":       text,
            "time":       datetime.now().strftime("%H:%M"),
        })
        reply = "⏳ Ваш вопрос уже у специалиста — ожидайте ответа."
        _append_history("bot", reply)
        widget_sessions[req.session_id] = session
        return {"reply": reply, "state": state, "ticket_id": session.get("ticket_id")}

    # Тикет уже закрыт — начинаем заново
    if state == "resolved":
        session["state"]             = "idle"
        session["questions"]         = []
        session["attempt"]           = 0
        session["chat_history"]      = []
        session["support_messages"]  = []
        session["ticket_created_at"] = None

    # Накапливаем вопросы
    questions = session.get("questions", [])
    attempt   = session.get("attempt", 0)
    if session.get("state") in ("idle", ""):
        questions = [text]; attempt = 1
    else:
        questions.append(text); attempt += 1
    session["questions"] = questions
    session["attempt"]   = attempt

    answer, source = await find_answer(questions)

    if answer:
        tid = new_ticket_id()
        session["state"]          = "awaiting_feedback"
        session["ticket_id"]      = tid
        session["ticket_created_at"] = datetime.now().isoformat()
        _append_history("bot", answer)
        if sheets:
            try:
                sheets.log_ticket(tid, req.session_id[:16], req.user_name,
                                  questions[0], " | ".join(questions)[:300], answer)
            except Exception as e:
                log.error(e)
        widget_sessions[req.session_id] = session
        return {"reply": answer, "state": "awaiting_feedback",
                "ticket_id": tid, "show_feedback": True, "source": source}

    elif attempt < MAX_CLARIFY_ATTEMPTS:
        session["state"] = "clarifying"
        remaining = MAX_CLARIFY_ATTEMPTS - attempt
        if attempt == 1:
            reply = "🤔 Не нашёл точного ответа. Уточните подробнее — что именно не работает?"
        else:
            reply = f"🤔 Всё ещё не могу найти. Опишите иначе — какая ошибка или на каком шаге. (осталось попыток: {remaining})"
        _append_history("bot", reply)
        widget_sessions[req.session_id] = session
        return {"reply": reply, "state": "clarifying"}

    else:
        tid = new_ticket_id()
        session["state"]          = "waiting_support"
        session["ticket_id"]      = tid
        session["ticket_created_at"] = datetime.now().isoformat()
        pending_tickets[tid] = req.session_id
        reply = (f"📋 Тикет {tid}\n\n"
                 "Не смог найти ответ. Передаю специалисту — ответит в течение 30 минут. ⏳")
        _append_history("bot", reply)
        if sheets:
            try:
                sheets.log_ticket(tid, req.session_id[:16], req.user_name,
                                  questions[0], " | ".join(questions)[:300], "")
            except Exception as e:
                log.error(e)
        widget_sessions[req.session_id] = session
        await push_support(_ticket_event(session, "new_ticket"))
        return {"reply": reply, "state": "waiting_support", "ticket_id": tid}


@app.post("/widget/feedback")
async def user_feedback(req: FeedbackReq):
    """
    resolved=True  → уведомить поддержку, сохранить в FAQ, закрыть тикет
    resolved=False → уведомить поддержку, оставить тикет открытым
    """
    session = widget_sessions.get(req.session_id, {})

    if req.resolved:
        # ── ПОДТВЕРЖДЕНО ──────────────────────────────────────
        session["state"] = "resolved"
        widget_sessions[req.session_id] = session

        # Сохраняем в FAQ (ответ специалиста или AI-ответ)
        if session.get("support_messages"):
            await save_to_faq(session)
        elif session.get("chat_history"):
            # Ответ нашёл бот — сохраним Q&A в FAQ тоже
            if sheets:
                try:
                    questions = session.get("questions", [])
                    q = " | ".join(questions[:2])
                    # Ищем последний bot-ответ в истории
                    bot_answers = [m["text"] for m in session["chat_history"]
                                   if m["role"] == "bot" and not m["text"].startswith("🤔")]
                    if bot_answers and q:
                        sheets.add_faq(q, bot_answers[-1], source="confirmed")
                        await maybe_refresh_faq()
                        log.info(f"FAQ confirmed answer: {q[:60]}")
                except Exception as e:
                    log.error(f"FAQ confirmed: {e}")

        if sheets:
            try:
                sheets.update_ticket_status(req.ticket_id, "resolved", "user confirmed")
            except Exception as e:
                log.error(e)

        # Уведомляем поддержку
        await push_support({
            "type":      "ticket_resolved",
            "ticket_id": req.ticket_id,
            "session_id": req.session_id,
            "by":        "user",
            "message":   "✅ Пользователь подтвердил решение",
        })
        pending_tickets.pop(req.ticket_id, None)

        return {"reply": "✅ Отлично! Рад помочь. Если возникнут ещё вопросы — пишите.",
                "state": "resolved", "ticket_id": req.ticket_id}

    else:
        # ── ОТКЛОНЕНО ─────────────────────────────────────────
        # Уведомляем поддержку что ответ не подошёл
        await push_support({
            "type":      "answer_rejected",
            "ticket_id": req.ticket_id,
            "session_id": req.session_id,
            "user_name": session.get("user_name", "Пользователь"),
            "message":   "❌ Пользователь отклонил ответ — нужна помощь специалиста",
        })

        # Эскалируем если ещё не у специалиста
        if session.get("state") != "waiting_support":
            session["state"] = "waiting_support"
            pending_tickets[req.ticket_id] = req.session_id
            if sheets:
                try:
                    sheets.update_ticket_status(req.ticket_id, "escalated", "answer rejected")
                except Exception as e:
                    log.error(e)
            widget_sessions[req.session_id] = session
            # Уведомляем поддержку как новый тикет
            await push_support(_ticket_event(session, "new_ticket"))

        widget_sessions[req.session_id] = session
        return {"reply": "Понял. Специалист поддержки поможет вам.",
                "state": "waiting_support", "ticket_id": req.ticket_id}


@app.post("/widget/close")
async def user_close_ticket(req: CloseReq):
    """Пользователь сам закрыл тикет."""
    session = widget_sessions.get(req.session_id, {})
    session["state"]             = "idle"
    session["questions"]         = []
    session["attempt"]           = 0
    session["chat_history"]      = []
    session["support_messages"]  = []
    session["ticket_created_at"] = None
    widget_sessions[req.session_id] = session
    pending_tickets.pop(req.ticket_id, None)

    if sheets:
        try:
            sheets.update_ticket_status(req.ticket_id, "closed_by_user", "")
        except Exception as e:
            log.error(e)

    # Уведомляем поддержку
    await push_support({
        "type":      "ticket_closed_by_user",
        "ticket_id": req.ticket_id,
        "session_id": req.session_id,
        "message":   "🚫 Пользователь закрыл тикет",
    })
    return {"ok": True}


# ══════════════════════════════════════════════════════════════
# SUPPORT API
# ══════════════════════════════════════════════════════════════

class SupportAuthReq(BaseModel):
    password: str

class SupportReplyReq(BaseModel):
    ticket_id:  str
    session_id: str
    message:    str


@app.post("/support/auth")
async def support_auth(req: SupportAuthReq):
    if req.password == SUPPORT_PASSWORD:
        return {"ok": True}
    return Response(status_code=401,
                    content=json.dumps({"error": "Неверный пароль"}),
                    media_type="application/json")


@app.get("/support/tickets")
async def get_tickets():
    result = []
    for sid, s in widget_sessions.items():
        if s.get("state") in ("waiting_support", "support_replied", "resolved"):
            result.append(_ticket_event(s, "list"))
    return {"tickets": sorted(result, key=lambda x: x.get("created_at",""), reverse=True)}


@app.post("/support/reply")
async def support_reply(req: SupportReplyReq):
    session = widget_sessions.get(req.session_id)
    if not session:
        return Response(status_code=404,
                        content=json.dumps({"error": "Сессия не найдена"}),
                        media_type="application/json")

    t = datetime.now().strftime("%H:%M")
    session.setdefault("chat_history", []).append(
        {"role": "support", "text": req.message, "time": t})
    session.setdefault("support_messages", []).append(req.message)
    session["state"] = "support_replied"
    widget_sessions[req.session_id] = session

    if sheets:
        try:
            sheets.update_ticket_status(req.ticket_id, "support_replied", "")
        except Exception as e:
            log.error(e)

    # Пересылаем пользователю
    await push_user(req.session_id, {
        "type":      "support_reply",
        "text":      req.message,
        "time":      t,
        "ticket_id": req.ticket_id,
    })
    log.info(f"Support replied {req.ticket_id}: {req.message[:60]}")
    return {"ok": True}


class CloseTicketReq(BaseModel):
    save_faq: bool = True


@app.post("/support/close/{ticket_id}")
async def close_ticket(ticket_id: str, req: CloseTicketReq = CloseTicketReq()):
    """Специалист закрывает тикет. save_faq=True → добавляем в FAQ."""
    sid     = pending_tickets.get(ticket_id)
    session = widget_sessions.get(sid) if sid else None

    if session:
        if req.save_faq:
            await save_to_faq(session)
        if sheets:
            try:
                note = "closed by support" if req.save_faq else "closed by support (no faq)"
                sheets.update_ticket_status(ticket_id, "resolved", note)
            except Exception as e:
                log.error(e)
        session["state"]             = "idle"
        session["chat_history"]      = []
        session["support_messages"]  = []
        session["ticket_created_at"] = None
        widget_sessions[sid] = session
        pending_tickets.pop(ticket_id, None)
        await push_user(sid, {
            "type": "ticket_closed",
            "text": "✅ Специалист закрыл ваш запрос. Если нужна ещё помощь — пишите!",
            "ticket_id": ticket_id,
        })

    # Уведомляем всех специалистов что тикет закрыт
    await push_support({
        "type":      "ticket_closed",
        "ticket_id": ticket_id,
        "by":        "support",
        "save_faq":  req.save_faq,
    })
    return {"ok": True}


@app.get("/faq")
async def get_faq_public():
    """Публичный эндпоинт для отображения базы знаний в виджете."""
    if not sheets:
        return {"faq": []}
    try:
        rows = sheets.get_faq()
        # Возвращаем только нужные поля, без технических деталей Sheets
        public_rows = [
            {
                "question_full":    r.get("question_full", r.get("question_essence", "")),
                "question_essence": r.get("question_essence", ""),
                "answer":           r.get("answer", ""),
                "source":           r.get("source", ""),
            }
            for r in rows
            if r.get("answer", "").strip()  # только строки с ответом
        ]
        return {"faq": public_rows, "total": len(public_rows)}
    except Exception as e:
        log.error(f"get_faq_public: {e}")
        return {"faq": [], "total": 0}


# ══════════════════════════════════════════════════════════════
# ДИАГНОСТИКА
# ══════════════════════════════════════════════════════════════

@app.get("/health")
async def health():
    return {
        "status": "ok", "version": "5.1",
        "time": datetime.now().isoformat(),
        "use_ai": USE_AI, "ai_available": ai_available,
        "sheets": sheets is not None,
        "sessions": len(widget_sessions),
        "pending":  len(pending_tickets),
        "support_ws": len(support_ws_list),
    }

@app.get("/debug")
async def debug():
    faq_count = 0
    if sheets:
        try: faq_count = len(sheets.get_faq())
        except: pass
    states = {}
    for s in widget_sessions.values():
        st = s.get("state","?")
        states[st] = states.get(st,0) + 1
    return {
        "faq_rows": faq_count,
        "sessions": states,
        "pending":  list(pending_tickets.keys()),
    }

@app.get("/refresh-faq")
async def refresh_faq():
    if not sheets: return {"error": "no sheets"}
    try:
        rows = sheets.get_faq()
        if USE_AI and ai_available: refresh_faq_cache(rows)
        return {"ok": True, "rows": len(rows)}
    except Exception as e:
        return {"error": str(e)}

@app.on_event("startup")
async def startup():
    if USE_AI and ai_available and sheets:
        await maybe_refresh_faq()
        asyncio.create_task(faq_refresh_loop())
    log.info("Bot v5.1 ready ✅")
