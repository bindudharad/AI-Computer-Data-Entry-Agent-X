"""Probe the live MPF form-panel scroll container behaviour via the assistant."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.assistant import Assistant
from atlas.config import load_config
from atlas.mapping.uia_map import UiaFieldMapBuilder
from atlas.observe.uia import UiaBackend


def main() -> int:
    config = load_config()
    with Assistant(config) as assistant:
        assistant.attach_auto(title="MPF")
        sandbox = assistant._executor._sandbox
        target = sandbox.validate_target()
        print(f"attached: {target.title} handle={target.handle} client_rect={target.client_rect}")

        backend = UiaBackend()
        rect = target.client_rect
        containers = backend.scroll_containers(target.handle, rect)
        print(f"containers found: {len(containers)}")
        for c in containers:
            r = c.rect
            print(
                f"  container name={c.name!r} class={c.class_name!r} "
                f"rect=({r.left},{r.top},{r.width},{r.height}) "
                f"has_pattern={c.has_scroll_pattern} percent={c.vertical_scroll_percent} view={c.vertical_view_size} "
                f"runtime={list(c.runtime_id)}"
            )

        form = None
        for c in containers:
            if c.class_name == "form-panel":
                form = c
                break
        if form is None:
            print("form-panel not found")
            return 1

        print("\n== testing scroll_container_pattern on form-panel ==")
        info = backend._container_info(form, target.handle)
        print(f"container_info resolved: {info is not None}")
        if info is not None:
            pattern = backend._scroll_pattern(info)
            print(f"scroll pattern: {pattern is not None}")
            if pattern is not None:
                print(f"  current percent={pattern.CurrentVerticalScrollPercent}")
                print(f"  view size={pattern.CurrentVerticalViewSize}")
                print(f"  vertical scrollable={pattern.CurrentVerticallyScrollable}")

        before = backend._scroll_percent(info) if info is not None else (None, None)
        print(f"before percent={before[0]} view={before[1]}")
        ok = backend.scroll_container_pattern(form, 120, target.handle)
        print(f"scroll_container_pattern(120) -> {ok}")
        info2 = backend._container_info(form, target.handle)
        if info2 is not None:
            after = backend._scroll_percent(info2)
            print(f"after percent={after[0]} view={after[1]}")
        else:
            print("after: container info lost")

        backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
