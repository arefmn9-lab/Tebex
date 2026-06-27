from modules.browser.behavior.human_behavior import HumanBehavior
from modules.browser.engine.playwright_client import PlaywrightClient
from modules.browser.session.session_manager import SessionManager


class BrowserEngine:
    def __init__(self, client=None, behavior=None, session_manager=None):
        self.client = client or PlaywrightClient()
        self.behavior = behavior or HumanBehavior()
        self.sessions = session_manager or SessionManager()
        self.page = self.client.get_page()

    def open_page(self, url: str):
        self.page.goto(url)
        return self.page

    def type(self, selector: str, text: str, human: bool = True):
        if human:
            self.behavior.type_text(self.page, selector, text)
        else:
            self.page.fill(selector, text)
        return self.page

    def click(self, selector: str):
        self.page.click(selector)
        return self.page

    def wait(self, selector: str, timeout: int = 30000):
        return self.page.wait_for_selector(selector, timeout=timeout)

    def save_session(self, account_id: str | int, platform: str):
        return self.sessions.save_session(account_id, platform, self.page.context)

    def load_session(self, account_id: str | int, platform: str):
        self.sessions.create_context(self.client, account_id, platform)
        self.page = self.client.get_page()
        return self.page

    def close(self):
        self.client.close()
