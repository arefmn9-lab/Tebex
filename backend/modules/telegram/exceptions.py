class TelegramError(Exception):
    pass


class InvalidSession(TelegramError):
    pass


class FloodWait(TelegramError):
    pass


class ConnectionLost(TelegramError):
    pass


class UserNotFound(TelegramError):
    pass


class RateLimited(TelegramError):
    pass
