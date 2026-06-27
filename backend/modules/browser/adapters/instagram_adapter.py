from modules.browser.behavior.human_behavior import HumanBehavior
from modules.browser.engine.browser_engine import BrowserEngine


class InstagramAdapter:
    PLATFORM = "instagram"
    LOGIN_URL = "https://www.instagram.com/accounts/login/"
    INBOX_URL = "https://www.instagram.com/direct/inbox/"
    MESSAGE_BOX_SELECTOR = "div[contenteditable='true']"

    def __init__(
        self,
        browser_engine: BrowserEngine | None = None,
        behavior: HumanBehavior | None = None,
    ):
        self.browser = browser_engine or BrowserEngine()
        self.behavior = behavior or HumanBehavior()
        self.current_chat_url = None

    def login(self, username: str, password: str, account_id: str | int | None = None):
        session_account_id = account_id or username
        self.browser.load_session(session_account_id, self.PLATFORM)

        page = self.browser.open_page(self.LOGIN_URL)
        try:
            page.wait_for_selector("input[name='username']", timeout=5000)
        except self._timeout_error():
            self.browser.save_session(session_account_id, self.PLATFORM)
            return {"success": True, "status": "session_loaded"}

        self.browser.type("input[name='username']", username)
        self.browser.type("input[name='password']", password)
        self.behavior.random_delay(0.5, 1.5)
        self.browser.click("button[type='submit']")
        page.wait_for_load_state("networkidle")
        self.browser.save_session(session_account_id, self.PLATFORM)
        return {"success": True, "status": "logged_in"}

    def open_dm(self, chat_url: str):
        self.current_chat_url = chat_url
        page = self.browser.open_page(chat_url)
        page.wait_for_load_state("networkidle")
        self.behavior.random_delay(0.5, 1.5)
        return page

    def open_dm_chat(self, chat_url: str):
        return self.open_dm(chat_url)

    def send_message(self, text: str, chat_url: str | None = None):
        if chat_url is not None:
            self.open_dm(chat_url)
        if self.current_chat_url is None:
            raise ValueError("A DM chat must be opened before sending a message")

        self.browser.wait(self.MESSAGE_BOX_SELECTOR)
        self.browser.type(self.MESSAGE_BOX_SELECTOR, text)
        self.behavior.random_delay(0.2, 0.8)
        self.browser.page.keyboard.press("Enter")
        return {
            "success": True,
            "status": "sent",
            "target": self.current_chat_url,
            "message": text,
        }

    def close(self):
        self.browser.close()

    @staticmethod
    def _timeout_error():
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        except ImportError:
            return TimeoutError
        return PlaywrightTimeoutError
