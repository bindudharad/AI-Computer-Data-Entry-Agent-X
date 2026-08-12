"""Probe: does the form-panel pattern scroll move the FIELD rects in a rebuilt field map?"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.assistant import Assistant
from atlas.config import load_config
from atlas.mapping.uia_map import UiaFieldMapBuilder
from atlas.observe.uia import UiaBackend


def field_rect(builder, handle):
    fmap = builder.build(handle)
    if fmap is None:
        return None
    for n in (fmap.right_fields or []):
        if getattr(n, "automation_id", "") in ("nakshatra", "subCaste", "phiCode"):
            r = n.rect
            print(f"    {n.automation_id!r} name={n.name!r} rect=({r.left},{r.top},{r.width},{r.height}) visible={n.visible}")
    return fmap


def main() -> int:
    config = load_config()
    with Assistant(config) as assistant:
        assistant.attach_auto(title="MPF")
        sandbox = assistant._executor._sandbox
        target = sandbox.validate_target()
        print(f"attached handle={target.handle} client_rect={target.client_rect}")

        backend = UiaBackend()
        builder = UiaFieldMapBuilder()
        handle = target.handle
        rect = target.client_rect

        print("== initial field map ==")
        field_rect(builder, handle)

        containers = backend.scroll_containers(handle, rect)
        form = None
        for c in containers:
            if c.class_name == "form-panel":
                form = c
        print(f"form-panel container: percent={form.vertical_scroll_percent}")

        print("== scroll pattern 300px ==")
        ok = backend.scroll_container_pattern(form, 300, handle)
        print(f"scrolled: {ok}")
        backend.container_state(form, handle)
        print(f"percent now: {form.vertical_scroll_percent}")

        print("== field map AFTER scroll ==")
        field_rect(builder, handle)

        print("== scroll pattern 300px again ==")
        backend.scroll_container_pattern(form, 300, handle)
        backend.container_state(form, handle)
        print(f"percent now: {form.vertical_scroll_percent}")
        print("== field map AFTER 2nd scroll ==")
        field_rect(builder, handle)

    return 0


if __name__ == "__main__":
    sys.exit(main())
