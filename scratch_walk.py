import json

d = json.load(open("debug/mpf/uia/tree.json", encoding="utf-8"))

def r2d(rect):
    if isinstance(rect, dict):
        return rect
    if isinstance(rect, list) and len(rect) == 4:
        x1, y1, x2, y2 = rect
        return {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}
    return {}

parent_map = {}
all_nodes = {}

def index(o):
    if isinstance(o, list):
        for c in o:
            index(c)
        return
    if not isinstance(o, dict):
        return
    all_nodes[id(o)] = o
    for c in (o.get("children") or []):
        parent_map[id(c)] = id(o)
    for c in (o.get("children") or []):
        index(c)

index(d)

def find(o):
    if isinstance(o, list):
        for c in o:
            find(c)
        return
    if not isinstance(o, dict):
        return
    name = o.get("name") or ""
    aid = o.get("automation_id") or ""
    if name.startswith("Record ") and "of" not in name or aid in ("rashi", "physicalStatus", "motherName", "education"):
        # trace ancestors
        chain = []
        cur = id(o)
        while cur in parent_map:
            n = all_nodes[cur]
            chain.append((n.get("control_type"), n.get("name"), n.get("automation_id"), r2d(n.get("rect"))))
            cur = parent_map[cur]
        print("NODE", aid or repr(name), r2d(o.get("rect")))
        for c in chain[:4]:
            print("   parent:", c)
        print()
    for c in (o.get("children") or []):
        find(c)

find(d)
