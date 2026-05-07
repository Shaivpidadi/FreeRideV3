"""v2 compatibility shims.

This subpackage preserves v2's CLI behavior (``freeride auto``, ``list``,
``switch``, ``status``, ``refresh``, ``fallbacks``, ``rotate``, watcher)
so existing v2 users get an in-place upgrade with no surprises. It writes
OpenClaw config files, just like v2 did.

Phase 5+ may rename or repurpose these commands once the gateway model
is the canonical FreeRide UX. Until then, every v2 user invocation must
land here and produce the same OpenClaw config v2 would have written.

Decision D9 (the design plan): the ``freeride auto`` CLI surface is
frozen.
"""
