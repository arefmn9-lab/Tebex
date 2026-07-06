"""Bale Web selectors.

Selectors are isolated from automation logic because Bale Web DOM details can
change independently of queue and campaign behavior.
"""

SEARCH_ICON_SVG_SELECTORS = [
    '[aria-label="Search-icon"]',
    'svg[aria-label="Search-icon"]',
]

SEARCH_ICON_CLICK_TARGET_SELECTORS = [
    'button:has([aria-label="Search-icon"])',
    '[role="button"]:has([aria-label="Search-icon"])',
    'div:has(> [aria-label="Search-icon"])',
    'div:has(> svg[aria-label="Search-icon"])',
    '[aria-label*="Search"]',
    '[title*="Search"]',
]

SEARCH_ICON_SELECTORS = [
    *SEARCH_ICON_CLICK_TARGET_SELECTORS,
    *SEARCH_ICON_SVG_SELECTORS,
]

TEXT_SEARCH_INPUT_SELECTORS = [
    'input[placeholder*="جستجو"]',
    'input[placeholder*="Search"]',
    'input[type="search"]',
    'input[aria-label*="Search"]',
    'input[aria-label*="جستجو"]',
    '[data-testid="chat-search-input"]',
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

SEARCH_RESULT_CANDIDATE_SELECTORS = [
    '[aria-label="dialog-item"]',
    "[data-testid='chat-list-item']",
    "[role='listitem']",
    "[data-testid*='chat']",
    '[role="button"]',
    "a",
    "div",
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

CONTACTS_PAGE_ENTRYPOINT_SELECTORS = [
    '[aria-label="Contacts-icon"]',
    'svg[aria-label="Contacts-icon"]',
    '[aria-label="BoldContacts-icon"]',
    'svg[aria-label="BoldContacts-icon"]',
]

CHAT_PAGE_ENTRYPOINT_SELECTORS = [
    '[aria-label="Chat-icon"]',
    'svg[aria-label="Chat-icon"]',
    '[aria-label="Chats-icon"]',
    'svg[aria-label="Chats-icon"]',
    '[aria-label="BoldChat-icon"]',
    'svg[aria-label="BoldChat-icon"]',
    '[aria-label="Message-icon"]',
    'svg[aria-label="Message-icon"]',
]

CONTACTS_UI_READY_SELECTORS = [
    '[aria-label="AddUser-icon"]',
    'svg[aria-label="AddUser-icon"]',
    ".ReactModal__Overlay",
    ".ReactModal__Content",
    'text=Add Contact',
]

CONTACTS_SEARCH_INPUT_SELECTORS = [
    'input[type="search"][placeholder="Search Contact..."]',
    'input[placeholder="Search Contact..."]',
    'input[type="search"]',
    'input[placeholder*="جستجو"]',
    'input[placeholder*="Search"]',
    '[role="textbox"]',
    '[contenteditable="true"]',
]

CONTACTS_RESULT_CANDIDATE_SELECTORS = [
    'div[role="list"]',
    '[role="listitem"]',
    '[role="button"]',
    '[aria-label*="contact"]',
    '[aria-label*="Contact"]',
    '[data-testid*="contact"]',
    'div[class*="contact"]',
    "a",
]

CONTACT_PROFILE_SELECTORS = [
    '[role="dialog"]',
    ".ReactModal__Content",
    '[data-testid*="profile"]',
    '[class*="profile"]',
]

CONTACT_MESSAGE_BUTTON_SELECTORS = [
    '[aria-label*="Message"]',
    '[aria-label*="Chat"]',
    '[aria-label*="پیام"]',
    'button:has-text("Message")',
    'button:has-text("Chat")',
    '[role="button"]:has-text("Message")',
    '[role="button"]:has-text("Chat")',
    '[role="button"]:has-text("پیام")',
]

ADD_CONTACT_ENTRYPOINT_SELECTORS = [
    '[aria-label="AddUser-icon"]',
    'svg[aria-label="AddUser-icon"]',
    '[aria-label="MainPlus-icon"]',
    'svg[aria-label="MainPlus-icon"]',
    '[aria-label*="Add"]',
    '[aria-label*="Contact"]',
    '[aria-label*="مخاطب"]',
    'button:has-text("Add Contact")',
    'button:has-text("افزودن مخاطب")',
]

ADD_CONTACT_MENU_ITEM_SELECTORS = [
    'text=Add Contact',
    'text=/.*Add Contact.*/',
    'button:has-text("Add Contact")',
    '[role="button"]:has-text("Add Contact")',
    '*:has-text("Add Contact")',
    'text=افزودن مخاطب',
    'text=مخاطب جدید',
    '[role="menuitem"]:has-text("Add Contact")',
    '[role="menuitem"]:has-text("افزودن مخاطب")',
    '[aria-label*="Add Contact"]',
    '[aria-label*="افزودن مخاطب"]',
]

ADD_CONTACT_MODAL_SELECTORS = [
    ".ReactModal__Overlay",
    ".ReactModal__Content",
    '[role="dialog"]',
    '[role="dialog"]:has-text("Add contact")',
    '[role="dialog"]:has-text("Add Contact")',
    'text=Add contact',
    'text=Through mobile number',
    '[role="dialog"]:has-text("افزودن مخاطب")',
    '[aria-modal="true"]',
    '[class*="modal"]',
]

ADD_CONTACT_PHONE_MODE_SELECTORS = [
    'text=Through mobile number',
    'button:has-text("Through mobile number")',
    '[role="button"]:has-text("Through mobile number")',
    '[role="tab"]:has-text("Through mobile number")',
]

ADD_CONTACT_USERNAME_MODE_SELECTORS = [
    'text=Through username',
    'button:has-text("Through username")',
    '[role="button"]:has-text("Through username")',
    '[role="tab"]:has-text("Through username")',
]

ADD_CONTACT_COUNTRY_SELECTOR_SELECTORS = [
    '.ReactModal__Content [class*="country"]',
    '.ReactModal__Content [aria-label*="Country"]',
    '.ReactModal__Content [role="button"]:has-text("+98")',
    '.ReactModal__Content :has-text("+98")',
    '.ReactModal__Content :has-text("Iran")',
]

ADD_CONTACT_NAME_INPUT_SELECTORS = [
    'input[placeholder="Name (required)"]',
    'input[placeholder*="Name (required)"]',
    'input[placeholder*="required"]',
    'input[placeholder="Name"]',
    'input[name*="first"]',
    'input[placeholder*="First"]',
    'input[placeholder*="Name"]',
    'input[placeholder*="نام"]',
]

ADD_CONTACT_NAME_INPUT_FALLBACK_SELECTORS = [
    '.ReactModal__Content input >> nth=1',
    '.ReactModal__Overlay input >> nth=1',
    '[role="dialog"] input >> nth=1',
]

ADD_CONTACT_LAST_NAME_INPUT_SELECTORS = [
    'input[name*="last"]',
    'input[placeholder*="Last"]',
    'input[placeholder*="خانوادگی"]',
]

ADD_CONTACT_PHONE_INPUT_SELECTORS = [
    'input[type="tel"]',
    'input[name*="phone"]',
    'input[placeholder="912 345 6789"]',
    'input[placeholder*="912 345 6789"]',
    'input[placeholder*="912"]',
    'input[placeholder*="345"]',
    'input[placeholder*="6789"]',
    'input[placeholder*="Mobile number"]',
    'input[placeholder*="Phone"]',
    'input[placeholder*="Mobile"]',
    'input[placeholder*="شماره"]',
    'input[placeholder*="موبایل"]',
]

ADD_CONTACT_PHONE_INPUT_FALLBACK_SELECTORS = [
    '.ReactModal__Content [class*="country"] ~ input',
    '.ReactModal__Content [aria-label*="Country"] ~ input',
    '.ReactModal__Content input:not([placeholder*="Name"])',
    '.ReactModal__Overlay input:not([placeholder*="Name"])',
    '[role="dialog"] input:not([placeholder*="Name"])',
]

ADD_CONTACT_SAVE_BUTTON_SELECTORS = [
    '.ReactModal__Content button:has-text("Add")',
    '.ReactModal__Content [role="button"]:has-text("Add")',
    '.ReactModal__Overlay button:has-text("Add")',
    '[role="dialog"] button:has-text("Add")',
    'button:has-text("Add")',
    '[role="button"]:has-text("Add")',
    'button:has-text("Save")',
    'button:has-text("Done")',
    'button:has-text("ذخیره")',
    'button:has-text("تایید")',
    '[role="button"]:has-text("Save")',
    '[role="button"]:has-text("ذخیره")',
    '[aria-label*="Save"]',
    '[aria-label*="ذخیره"]',
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
