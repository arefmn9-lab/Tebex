# TEBEX PROJECT CONTEXT

## Project

Name: Tebex

Tebex is an AI-first Omnichannel CRM and Communication Platform for clinics.

This project is designed for long-term scalability.

Technology Stack:

* FastAPI
* PostgreSQL
* SQLAlchemy
* Alembic
* Docker
* Python

Architecture:

Modular Monolith

Every feature must be placed inside modules.

Example:

modules/
sales/
communication/
campaign/
worker/
ai/

---

## Core Rule

Everything is a Job.

Sending Message = Job

Scraping = Job

Posting = Job

Reply = Job

Future features must never bypass the Job Engine.

---

## Execution Flow

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

Chrome Browser

↓

Platform Adapter

↓

Telegram

Instagram

WhatsApp

Rubika

Bale

Eitaa

Soroush

SMS

Email

---

## Worker

Workers always use Chrome.

Each Worker owns:

Chrome Profile

Cookies

Session

Proxy

Health

Statistics

Queue

Logs

Workers never communicate directly with Platforms.

Workers execute Jobs only.

---

## AI

AI controls:

Worker selection

Queue

Scheduling

Load balancing

Retry

Health

Campaign optimization

Lead scoring

---

## Communication

Communication Engine must support:

Telegram

Instagram

WhatsApp

Rubika

Bale

Eitaa

Soroush

SMS

Email

without redesigning the architecture.

---

## Instagram

Version 1

Messaging

Scraper

Accounts

Version 2

Posting

Stories

Reels

Comments

Likes

Explore

Feed

Profile Manager

---

## Coding Rules

Repository Pattern

Service Layer

Schemas

Routes

No Business Logic inside Routes.

Alembic for Database Changes.

Every change must keep backward compatibility.

Never redesign the architecture.

Always follow this document.
