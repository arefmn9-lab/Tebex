# Sprint 04 - Queue & Worker Engine

## Goal

Build the generic execution engine for Tebex.

No platform-specific logic is allowed.

This sprint creates the infrastructure that every future platform (Telegram, Instagram, WhatsApp, Bale, Rubika, Eitaa, Soroush, Email, SMS...) will use.

---

## Architecture Rules

Do not modify Sales.

Do not modify Communication.

Everything must remain Modular Monolith.

Everything is a Job.

---

## Create New Modules

backend/modules/queue

backend/modules/worker

---

## Queue Module

Create:

* models
* schemas
* repository
* services
* api

Queue stores jobs waiting for execution.

Fields should include:

* id
* job_id
* priority
* status
* attempts
* max_attempts
* scheduled_at
* started_at
* finished_at
* worker_id
* created_at
* updated_at

---

## Worker Module

Create:

* models
* schemas
* repository
* services
* api

Fields:

* id
* name
* status
* chrome_profile
* proxy
* platform
* current_job
* heartbeat
* last_activity
* created_at
* updated_at

---

## Queue Status

Pending

Running

Completed

Failed

Paused

Cancelled

Retry

---

## Worker Status

Idle

Busy

Offline

Disabled

Starting

Stopping

---

## Priority

High

Normal

Low

---

## Scheduler

Create a generic scheduler service.

Responsibilities:

* assign jobs
* select idle workers
* retry failed jobs
* respect priority
* support delayed execution

No real execution yet.

---

## API

Create CRUD endpoints.

Create endpoints:

GET Queue

POST Queue

PATCH Queue

GET Workers

POST Workers

PATCH Workers

---

## Database

Create Alembic migration.

Register all models.

Register routers.

---

## Validation

Run compile checks.

Run backend.

Fix every error until backend starts successfully.

Return a complete summary of modified files.
