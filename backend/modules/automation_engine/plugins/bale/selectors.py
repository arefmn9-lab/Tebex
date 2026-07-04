"""Bale Web selectors.

Selectors are isolated from automation logic because Bale Web DOM details can
change independently of queue and campaign behavior.
"""

SEARCH_ICON_SELECTORS = [
    '[aria-label="Search-icon"]',
    'svg[aria-label="Search-icon"]',
]

TEXT_SEARCH_INPUT_SELECTORS = [
    'input[placeholder*="جستجو"]',
    'input[placeholder*="Search"]',
    '[role="textbox"]',
    '[contenteditable="true"]',
]

SEARCH_INPUT_SELECTORS = [
    *SEARCH_ICON_SELECTORS,
    "[data-testid='chat-search-input']",
    'input[placeholder*="Search"]',
    'input[placeholder*="جستجو"]',
    "input[type='search']",
    '[role="textbox"]',
    '[contenteditable="true"]',
]

CHAT_ITEM_SELECTORS = [
    '[aria-label="dialog-item"]',
    "[data-testid='chat-list-item']",
    "[role='listitem']",
    "[data-testid*='chat']",
]

MESSAGE_INPUT_SELECTORS = [
    "#editable-message-text",
    '[aria-label="editable-message-text"][contenteditable="true"]',
    '[contenteditable="true"]#editable-message-text',
    '[aria-label="editable-message-text"]',
    "[data-testid='message-input']",
    'div[contenteditable="true"]',
    'textarea[placeholder*="Message"]',
    'textarea[placeholder*="پیام"]',
]

SEND_BUTTON_SELECTORS = [
    "[data-testid='send-message-button']",
    'button[aria-label*="Send"]',
    'button[aria-label*="ارسال"]',
    "button[type='submit']",
]

MESSAGE_TOOLBAR_SIGNAL_SELECTORS = [
    '[aria-label="MainPlus-icon"]',
    '[aria-label="Emoji-icon"]',
]

LOGIN_STATE_INDICATOR_SELECTORS = [
    '[aria-label="dialog-item"]',
    "#editable-message-text",
    '[aria-label="editable-message-text"]',
    *SEARCH_ICON_SELECTORS,
    *MESSAGE_TOOLBAR_SIGNAL_SELECTORS,
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
