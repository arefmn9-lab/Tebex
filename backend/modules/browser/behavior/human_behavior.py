import random
import time


class HumanBehavior:
    def random_delay(self, minimum: float = 1.0, maximum: float = 5.0):
        delay = random.uniform(minimum, maximum)
        time.sleep(delay)
        return delay

    def type_text(
        self,
        page,
        selector: str,
        text: str,
        minimum_delay_ms: int = 100,
        maximum_delay_ms: int = 800,
    ):
        page.click(selector)
        for character in text:
            page.keyboard.type(
                character,
                delay=self.jitter(minimum_delay_ms, maximum_delay_ms),
            )
        return page

    def jitter(self, minimum: int = 100, maximum: int = 800):
        return random.randint(minimum, maximum)
