import random
import re
from datetime import timedelta

from modules.account_control.models import Account, AccountStatus


class WarmupEngine:
    CASUAL_MESSAGES = (
        "Hey, how are you?",
        "Hope your day is going well.",
        "Thanks for the update.",
        "That sounds good.",
        "I will check and get back to you.",
        "Sure, that works.",
        "Got it, thank you.",
    )
    LINK_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)

    @classmethod
    def is_warmup_account(cls, account: Account):
        return account.status in {AccountStatus.NEW, AccountStatus.WARMING_UP}

    @classmethod
    def daily_limit_for(cls, account: Account):
        age_days = account.warmup_age_days
        if age_days <= 3:
            return random.randint(1, 3)
        if age_days <= 7:
            return random.randint(5, 10)
        if age_days <= 14:
            return random.randint(10, 25)
        return account.daily_message_limit

    @classmethod
    def update_account_status(cls, account: Account):
        if account.status in {AccountStatus.BANNED, AccountStatus.RISK}:
            return account.status
        if account.warmup_age_days > 14:
            account.status = AccountStatus.ACTIVE
        elif account.status == AccountStatus.NEW:
            account.status = AccountStatus.WARMING_UP
        return account.status

    @classmethod
    def prepare_payload(cls, account: Account, payload: dict):
        if not cls.is_warmup_account(account):
            return payload

        prepared = dict(payload)
        message = str(prepared.get("message") or prepared.get("text") or "")
        previous_message = account.metadata.get("last_message")

        if cls.LINK_PATTERN.search(message):
            return {
                **prepared,
                "blocked": True,
                "block_reason": "warmup accounts cannot send links",
            }

        if previous_message and previous_message.strip().lower() == message.strip().lower():
            message = cls.generate_warmup_message(exclude=previous_message)

        if len(message) > 180:
            message = message[:177].rstrip() + "..."

        prepared["message"] = message
        prepared["text"] = message
        prepared.setdefault("metadata", {})
        prepared["metadata"] = {
            **prepared["metadata"],
            "warmup_applied": True,
            "warmup_delay_seconds": cls.random_delay_seconds(),
        }
        return prepared

    @classmethod
    def generate_warmup_message(cls, exclude: str | None = None):
        choices = [
            message
            for message in cls.CASUAL_MESSAGES
            if message.strip().lower() != str(exclude or "").strip().lower()
        ]
        return random.choice(choices or list(cls.CASUAL_MESSAGES))

    @staticmethod
    def random_delay_seconds():
        return random.randint(30, 300)

    @staticmethod
    def random_delay():
        return timedelta(seconds=WarmupEngine.random_delay_seconds())
