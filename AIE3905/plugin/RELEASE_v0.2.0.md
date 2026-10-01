# v0.2.0

Natural dialogue and multi-turn draft planning now sit alongside strict record queries. Only explicit mentions/commands reply; normal group messages still feed background extraction.

Adds a bounded JSON tool loop, persistent versioned drafts, authorized natural confirmation, `/api/dialogue`, stable request replay protection, additive SQLite migration, and linked deletion/retention. Correcting a note cannot change official fields; correcting a cancellation keeps the cancelled status. Existing commands and provider configuration remain supported.

Set `dialogue.enabled` to `false` in the plugin's JSON configuration to restore the legacy conversation route. Back up the plugin and SQLite DB before upgrade. New dialogue is model-assisted: synthetic tests do not establish general accuracy or verify a real QQ round trip.
