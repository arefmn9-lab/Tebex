"""Bale Web selectors.

These selectors must be verified against the real Bale Web DOM before
production use. They are intentionally isolated from core automation logic.
"""

SEARCH_INPUT_SELECTORS = [
    "[data-testid='chat-search-input']",
    "input[placeholder*='Search']",
    "input[placeholder*='جست']",
    "input[type='search']",
]

CHAT_ITEM_SELECTORS = [
    "[data-testid='chat-list-item']",
    "[role='listitem']",
    "[data-testid*='chat']",
]

MESSAGE_INPUT_SELECTORS = [
    "[data-testid='message-input']",
    "div[contenteditable='true']",
    "textarea[placeholder*='Message']",
    "textarea[placeholder*='پیام']",
]

SEND_BUTTON_SELECTORS = [
    "[data-testid='send-message-button']",
    "button[aria-label*='Send']",
    "button[aria-label*='ارسال']",
    "button[type='submit']",
]

LOGIN_STATE_INDICATOR_SELECTORS = [
    "[data-testid='chat-list']",
    "[data-testid='conversation-list']",
    "div[role='list']",
]

MESSAGE_SENT_INDICATOR_SELECTORS = [
    "[data-testid='message-out']",
    "[data-testid='outgoing-message']",
    ".message-out",
]

SEARCH_INPUT = SEARCH_INPUT_SELECTORS[0]
CHAT_ITEM = CHAT_ITEM_SELECTORS[0]
MESSAGE_INPUT = MESSAGE_INPUT_SELECTORS[0]
SEND_BUTTON = SEND_BUTTON_SELECTORS[0]
LOGIN_STATE_INDICATOR = LOGIN_STATE_INDICATOR_SELECTORS[0]
MESSAGE_SENT_INDICATOR = MESSAGE_SENT_INDICATOR_SELECTORS[0]
