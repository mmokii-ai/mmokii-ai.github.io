import os
import json
import logging
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

log = logging.getLogger("SHEETS")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _get_credentials() -> Credentials:
    content = os.environ.get("GOOGLE_CREDENTIALS_CONTENT")
    if content:
        log.debug("Credentials из GOOGLE_CREDENTIALS_CONTENT")
        try:
            info = json.loads(content)
            log.debug(f"  client_email: {info.get('client_email', '?')}")
            return Credentials.from_service_account_info(info, scopes=SCOPES)
        except json.JSONDecodeError as e:
            log.error(f"❌ JSON error: {e} | начало: {content[:80]}")
            raise

    path = os.environ.get("GOOGLE_CREDENTIALS_JSON", "google_credentials.json")
    log.debug(f"Credentials из файла: {path}")
    if not os.path.exists(path):
        raise FileNotFoundError(f"Файл не найден: {path}")
    return Credentials.from_service_account_file(path, scopes=SCOPES)


class SheetsClient:
    def __init__(self):
        spreadsheet_id = os.environ.get("GOOGLE_SPREADSHEET_ID", "")
        if not spreadsheet_id:
            raise ValueError("GOOGLE_SPREADSHEET_ID не задан!")

        log.info(f"Подключение к Sheets ID: {spreadsheet_id[:20]}...")
        creds = _get_credentials()
        gc = gspread.authorize(creds)
        self.sheet = gc.open_by_key(spreadsheet_id)

        available = [ws.title for ws in self.sheet.worksheets()]
        log.info(f"✅ Таблица: {self.sheet.title} | Листы: {available}")

        self._faq     = self.sheet.worksheet("FAQ")
        self._tickets = self.sheet.worksheet("Tickets")
        self._config  = self.sheet.worksheet("Config")
        log.info("✅ FAQ / Tickets / Config найдены")

    def get_faq(self) -> list[dict]:
        rows = self._faq.get_all_records()
        log.debug(f"FAQ: {len(rows)} строк")
        return rows

    def add_faq(self, question: str, answer: str, source: str = "AI_learned") -> None:
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        self._faq.append_row([question, question, answer, source, now, 0])
        log.info(f"✅ FAQ добавлен: {question[:60]}")

    def log_ticket(self, ticket_id: str, user_id: int, user_name: str,
                   original_q: str, essence: str, ai_answer: str) -> None:
        now = datetime.now().strftime("%d.%m.%Y %H:%M")
        self._tickets.append_row([
            ticket_id, str(user_id), user_name,
            original_q[:500], essence[:300], ai_answer[:500],
            "open", "", now, now
        ])
        log.info(f"✅ Тикет {ticket_id} записан")

    def update_ticket_status(self, ticket_id: str, status: str, note: str = "") -> None:
        try:
            cell = self._tickets.find(ticket_id, in_column=1)
            if not cell:
                log.warning(f"⚠️  Тикет {ticket_id} не найден")
                return
            now = datetime.now().strftime("%d.%m.%Y %H:%M")
            self._tickets.update_cell(cell.row, 7, status)
            self._tickets.update_cell(cell.row, 8, note[:300])
            self._tickets.update_cell(cell.row, 10, now)
            log.info(f"✅ Тикет {ticket_id} → {status}")
        except Exception as e:
            log.error(f"❌ update_ticket_status: {e}")

    def increment_counter(self) -> int:
        try:
            cell = self._config.find("ticket_counter", in_column=1)
            if cell:
                current = int(self._config.cell(cell.row, 2).value or 0)
                new_val = current + 1
                self._config.update_cell(cell.row, 2, new_val)
                return new_val
            self._config.append_row(["ticket_counter", 1])
            return 1
        except Exception as e:
            log.error(f"❌ Счётчик: {e}")
            import time
            return int(time.time()) % 9999
