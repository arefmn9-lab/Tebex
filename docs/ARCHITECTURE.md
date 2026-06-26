# TEBEX ARCHITECTURE

## Architecture Style

Modular Monolith

---

## Main Modules

* Sales
* Communication
* Campaign
* Worker
* AI
* Automation
* Analytics
* Settings

---

## Core Flow

Lead

↓

Campaign

↓

Job

↓

Queue

↓

Worker

↓

Chrome

↓

Platform Adapter

↓

Telegram / Instagram / WhatsApp / Rubika / Bale / Eitaa / Soroush / SMS / Email

---

## Core Principle

Everything is a Job.

Examples:

* Send Message
* Scrape Users
* Extract Numbers
* Invite Members
* Reply Message
* Create Post
* Publish Story

---

## Worker Rules

Workers never communicate directly with platforms.

Workers only execute Jobs.

Workers always use Chrome.

AI controls Workers.

---

## Future Vision

The architecture must support adding new platforms without changing the core system.

Platform adapters should be plug-in based.
