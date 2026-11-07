# Secrets

---
description: Security rules for file access
globs: *
alwaysApply: true
---

- NEVER read, reference, or modify .env files.
- Use .env.example for variable names. Never hardcode secret values.
- Never print an API key, a webhook signing secret or a
  service-role token into chat, a log line or a commit.
- If a task needs a real secret value, stop and ask.