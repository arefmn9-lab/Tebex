from modules.browser.behavior.human_behavior import HumanBehavior
from modules.browser.engine.playwright_client import PlaywrightClient


class BrowserEngine:
    def __init__(
        self,
        client: PlaywrightClient | None = None,
        behavior: HumanBehavior | None = None,
    ):
        self.client = client or PlaywrightClient()
        self.behavior = behavior or HumanBehavior()

    def open_page(self, url: str):
        page = self.client.get_page()
        page.goto(url)
        return page

    def type(self, selector: str, text: str, human: bool = True):
        page = self.client.get_page()
        if human:
            self.behavior.type_text(page, selector, text)
        else:
            page.fill(selector, text)
        return page

    def click(self, selector: str):
        page = self.client.get_page()
        page.click(selector)
        return page

    def wait(self, selector: str, timeout: int = 30000):
        page = self.client.get_page()
        return page.wait_for_selector(selector, timeout=timeout)

    def close(self):
        self.client.close()
