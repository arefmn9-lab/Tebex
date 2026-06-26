class PlaywrightClient:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def start(self):
        if self._browser is not None:
            return self

        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("Playwright is not installed") from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(
            channel="chrome",
            headless=self.headless,
        )
        return self

    def new_context(self, storage_state: str | dict | None = None, **kwargs):
        self.start()
        context_kwargs = dict(kwargs)
        if storage_state:
            context_kwargs["storage_state"] = storage_state

        self._context = self._browser.new_context(**context_kwargs)
        self._page = self._context.new_page()
        return self._context

    def get_page(self):
        self.start()
        if self._context is None:
            self.new_context()
        if self._page is None:
            self._page = self._context.new_page()
        return self._page

    def close(self):
        if self._context is not None:
            self._context.close()
            self._context = None
            self._page = None

        if self._browser is not None:
            self._browser.close()
            self._browser = None

        if self._playwright is not None:
            self._playwright.stop()
            self._playwright = None
