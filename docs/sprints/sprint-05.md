# Sprint 05 - Telegram Adapter Foundation

## Goal

Implement the first real messaging platform adapter.

This sprint introduces the generic adapter architecture and implements the first adapter for Telegram.

No Instagram, WhatsApp, Rubika, Bale, Eitaa or Soroush logic should be added yet.

Telegram becomes the reference implementation that every future platform will follow.

---

# Architecture Rules

Do not modify:

* Sales
* Communication
* Queue
* Worker

Only extend them.

Everything must still follow:

Job

↓

Queue

↓

Worker

↓

Platform Adapter

↓

Telegram

---

# Create New Module

backend/modules/telegram

Use the same architecture as every other module:

* models
* schemas
* repository
* services
* api

---

# Telegram Account

Create Telegram account entity.

Fields:

* id
* communication_account_id
* phone_number
* api_id
* api_hash
* session_name
* session_path
* status
* last_login
* created_at
* updated_at

---

# Telegram Session Manager

Create a service responsible for:

* create session
* load session
* close session
* reconnect session
* validate session

No messaging logic inside SessionManager.

---

# Telegram Client

Create TelegramClient service.

Responsibilities:

* connect
* disconnect
* send message
* receive updates
* resolve user
* health check

Do not add campaign logic.

---

# Adapter Layer

Create generic adapter interface.

Future adapters:

* Telegram
* Instagram
* WhatsApp
* Rubika
* Bale
* Eitaa
* Soroush
* SMS
* Email

must implement exactly the same interface.

Example methods:

* connect()
* disconnect()
* send_message()
* receive_message()
* health_check()

---

# Queue Integration

Worker receives Job.

Worker calls Adapter.

Adapter calls TelegramClient.

Worker never communicates directly with Telegram.

---

# Worker Integration

Worker receives:

Platform = Telegram

↓

Loads Account

↓

Loads Session

↓

Executes Job

↓

Updates Queue Status

---

# Message Sending

Support:

Single Message

Input:

* Account
* Target
* Text

Output:

Success / Failed

---

# Error Handling

Create generic errors:

* InvalidSession
* FloodWait
* ConnectionLost
* UserNotFound
* RateLimited

No retry logic here.

Retry remains Queue responsibility.

---

# Logging

Log:

* Login
* Logout
* Send
* Receive
* Errors

---

# Health Check

Create endpoint:

GET /telegram/health

Returns:

* Connected
* Disconnected
* Session Valid
* Session Invalid

---

# API

Create endpoints:

POST /telegram/connect

POST /telegram/disconnect

POST /telegram/send

GET /telegram/status

GET /telegram/health

---

# Database

Create Alembic migration.

Register models.

Register router.

---

# Validation

Run compile checks.

Run docker build.

Run alembic upgrade.

Start backend.

Fix every error until backend starts successfully.

Return complete summary of modified files.

---

# Important

Do NOT implement scraping.

Do NOT implement campaigns.

Do NOT implement automation.

Do NOT implement human behaviour.

Do NOT implement AI.

Only create a clean Telegram Adapter architecture that will become the template for every future platform.
