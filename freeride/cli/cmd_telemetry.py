"""``freeride telemetry`` — manage the default-on beacon."""

from __future__ import annotations

from freeride.core import telemetry


def cmd_telemetry(args) -> int:
    state = getattr(args, "state", None)

    if state == "on":
        telemetry.set_enabled(True)
        telemetry.mark_disclosure_shown()  # explicit on => already disclosed
        print("freeride telemetry: ENABLED")
        print()
        print("This installation will POST the following payload hourly to:")
        print(f"  {telemetry.beacon_url()}")
        print()
        print("Payload (verbatim):")
        print(telemetry.preview_payload())
        print()
        print("Run `freeride telemetry off` to stop.")
        return 0

    if state == "off":
        telemetry.set_enabled(False)
        print("freeride telemetry: DISABLED")
        print("No beacon will be sent. (Local stats keep accumulating; they never leave the box.)")
        return 0

    # No args — status + payload preview
    enabled = telemetry.is_enabled()
    print(f"freeride telemetry: {'ENABLED' if enabled else 'DISABLED'} (default: ENABLED)")
    print()
    print(f"Endpoint: {telemetry.beacon_url()}")
    print(f"Installation ID: {telemetry.installation_id()}")
    print()
    print("Payload that would be sent (audit current state):")
    print(telemetry.preview_payload())
    print()
    print("Toggle with: freeride telemetry on  /  freeride telemetry off")
    return 0
