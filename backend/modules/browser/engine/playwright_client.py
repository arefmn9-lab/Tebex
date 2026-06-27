class PlaywrightClient:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self):
        if self.browser is not None:
            return self

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed") from exc

        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            channel="chrome",
            headless=self.headless,
        )
        return self

    def new_context(self, storage_state: str | dict | None = None, **kwargs):
        self.start()
        if self.context is not None:
            self.context.close()

        context_kwargs = dict(kwargs)
        if storage_state:
            context_kwargs["storage_state"] = storage_state

        self.context = self.browser.new_context(**context_kwargs)
        self.page = self.context.new_page()
        return self.context

    def get_page(self):
        self.start()
        if self.context is None:
            self.new_context()
        if self.page is None:
            self.page = self.context.new_page()
        return self.page

    def close(self):
        if self.context is not None:
            self.context.close()
            self.context = None
            self.page = None

        if self.browser is not None:
            self.browser.close()
            self.browser = None

        if self.playwright is not None:
            self.playwright.stop()
            self.playwright = None
