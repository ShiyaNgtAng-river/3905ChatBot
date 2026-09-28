# v0.2.1

Fixes duplicate replies in AstrBot 4.28.1 by passing True to the host's default-LLM suppression flag. Adds a regression against the installed host ProcessStage and logs answer ID / chunk count after plugin sends.

Dialogue now distinguishes its own runtime capabilities from other speakers' claims and uses more concise, relevant responses. Completed drafts are shown directly on fallback. Existing records and configuration are preserved.
