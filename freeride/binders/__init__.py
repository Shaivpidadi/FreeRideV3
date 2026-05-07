"""Per-agent binder helpers — write one URL into one config file.

These are deliberately *not* a generic Consumer plugin abstraction (see
the design plan). Each binder is a small, ad-hoc adapter that knows
exactly how to point its specific agent at the FreeRide gateway. The
common pattern is:

* Locate the agent's config file (env-var override > default path)
* Read it (atomic; preserve all unrelated keys)
* Set the gateway URL + api_key="any"
* Atomic-write back

The contract is one function per agent, signature::

    def bind(gateway_url: str, *, api_key: str = "any") -> str

Returning a one-line status string the CLI prints.
"""
