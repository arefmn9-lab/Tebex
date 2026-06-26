from modules.browser.engine.browser_engine import BrowserEngine
from modules.browser.session.session_manager import SessionManager


class InstagramAdapter:
    LOGIN_URL = "https://www.instagram.com/accounts/login/"
    INBOX_URL = "https://www.instagram.com/direct/inbox/"

    def __init__(
        self,
        browser_engine: BrowserEngine | None = None,
        session_manager: SessionManager | None = None,
    ):
        self.browser = browser_engine or BrowserEngine()
        self.sessions = session_manager or SessionManager()

    def login(self, account_id: str | int, username: str, password: str):
        storage_state = self.sessions.load_session("instagram", account_id)
        if storage_state:
            self.browser.client.new_context(storage_state=storage_state)

        page = self.browser.open_page(self.LOGIN_URL)
        page.wait_for_selector("input[name='username']", timeout=30000)
        self.browser.type("input[name='username']", username)
        self.browser.type("input[name='password']", password)
        self.browser.click("button[type='submit']")
        page.wait_for_load_state("networkidle")
        self.sessions.save_session("instagram", account_id, page.context)
        return {"success": True, "status": "logged_in"}

    def open_dm_chat(self, chat_url: str):
        page = self.browser.open_page(chat_url)
        page.wait_for_load_state("networkidle")
        return page

    def send_message(self, chat_url: str, text: str):
        page = self.open_dm_chat(chat_url)
        self.browser.wait("div[contenteditable='true']")
        self.browser.type("div[contenteditable='true']", text)
        page.keyboard.press("Enter")
        return {"success": True, "status": "sent", "target": chat_url, "message": text}

    def close(self):
        self.browser.close()
