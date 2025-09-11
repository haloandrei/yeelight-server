import json, os, threading, time
from flask import Flask, request, jsonify
from yeelight import Bulb, discover_bulbs, Flow, transitions

CFG_FILE = "bulbs.json"          # runtime config (name -> {ip,id})
SEED_FILE = "bulbs.seed.json"    # your initial mapping
GROUPS_FILE = "groups.json"      # optional groups
SCENES_FILE = "scenes.json"      # optional scenes

app = Flask(__name__)

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

# 1) Build bulbs.json (merge seed with autodiscovery by
# --- helpers --------------------------------------------------------------

def _index_cfg(cfg):
    """Build quick lookups for existing config: id->name, ip->name."""
    by_id = {}
    by_ip = {}
    for name, ent in cfg.items():
        bid = ent.get("id")
        ip  = ent.get("ip")
        if bid: by_id[bid] = name
        if ip:  by_ip[ip]  = name
    return by_id, by_ip

def _new_auto_name(ip=None, bid=None):
    if bid:
        return f"bulb_{bid[-6:]}"  # last 6 hex chars of id
    if ip:
        return f"bulb_{ip.replace('.', '_')}"
    return "bulb_unknown"

# --- main merge -----------------------------------------------------------

def build_config():
    """
    Merge seed + discovery into bulbs.json **without duplicates**.
    Priority:
      1) Seed names are preserved.
      2) Discovery updates existing entries (by id, else by ip).
      3) New devices from discovery are added with auto names.
    """
    seed = load_json(SEED_FILE, {})       # friendly-name -> {ip,id}
    cfg  = load_json(CFG_FILE, {})        # runtime config

    # Start from current cfg, but ensure all seed names exist (preserve names)
    merged = dict(cfg)
    for name, ent in seed.items():
        me = merged.get(name, {})
        # Seed wins for friendly name; keep id/ip if present
        if ent.get("id"): me["id"] = ent["id"]
        if ent.get("ip"): me["ip"] = ent["ip"]
        merged[name] = me

    # Build indices before discovery
    by_id, by_ip = _index_cfg(merged)

    # Pull discovery results
    disc = discover_bulbs()  # returns list of {ip, port, capabilities:{id,...}}
    for b in disc:
        ip = b["ip"]
        bid = b.get("capabilities", {}).get("id")

        name_for_id = by_id.get(bid) if bid else None
        name_for_ip = by_ip.get(ip)  if ip  else None

        if name_for_id and name_for_ip and name_for_id != name_for_ip:
            # Same device known under two names -> consolidate into the id-name (prefer seed)
            keep = name_for_id
            drop = name_for_ip
            # Move any data from drop into keep if missing
            kept = merged.get(keep, {})
            dropped = merged.get(drop, {})
            if not kept.get("ip") and dropped.get("ip"): kept["ip"] = dropped["ip"]
            if not kept.get("id") and dropped.get("id"): kept["id"] = dropped["id"]
            merged[keep] = kept
            # Remove the duplicate
            merged.pop(drop, None)
            # Rebuild indices
            by_id, by_ip = _index_cfg(merged)

        # Re-compute names after possible consolidation
        name = by_id.get(bid) or by_ip.get(ip)
        if name:
            # Update existing entry with fresh id/ip
            ent = merged[name]
            if bid: ent["id"] = bid
            if ip:  ent["ip"] = ip
            merged[name] = ent
        else:
            # New device: give it a deterministic auto name, unless seed already had an id match
            auto = _new_auto_name(ip, bid)
            # Avoid collision
            i = 1
            base = auto
            while auto in merged:
                i += 1
                auto = f"{base}_{i}"
            merged[auto] = {"ip": ip, "id": bid}

        # Keep indices fresh
        by_id, by_ip = _index_cfg(merged)

    # Final pass: ensure unique id/ip (paranoid cleanup)
    seen_ids = {}
    seen_ips = {}
    to_delete = []
    for name, ent in merged.items():
        bid = ent.get("id")
        ip  = ent.get("ip")
        if bid:
            if bid in seen_ids and seen_ids[bid] != name:
                # keep the older (assume first is canonical); drop this duplicate
                to_delete.append(name)
            else:
                seen_ids[bid] = name
        if ip:
            if ip in seen_ips and seen_ips[ip] != name:
                to_delete.append(name)
            else:
                seen_ips[ip] = name
    for name in set(to_delete):
        merged.pop(name, None)

    save_json(CFG_FILE, merged)
    return merged


CONFIG = build_config()
GROUPS = load_json(GROUPS_FILE, {})   # e.g. {"kitchen":["kitchen_1","kitchen_2"], "tv":["tv_left","tv_right"]}
SCENES = load_json(SCENES_FILE, {})   # e.g. {"movie":[{"target":"tv","cmd":"set_bright","args":[20]}, {"target":"tv","cmd":"set_ct_abx","args":[2700,"smooth",500]}]}

# Bulb cache with music-mode sockets
class BulbPool:
    def __init__(self):
        self._pool = {}  # name -> Bulb

    def get(self, name) -> Bulb | None:
        ent = CONFIG.get(name)
        if not ent: return None
        ip = ent.get("ip")
        if not ip: return None
        b = self._pool.get(name)
        if b is None:
            b = Bulb(ip, auto_on=False, effect="smooth", duration=300)
            # try music mode (faster): opens persistent socket
            try:
                b.start_music()
            except Exception:
                pass
            self._pool[name] = b
        return b

BULBS = BulbPool()

def names_or_group(target):
    # return a list of bulb names (expand groups)
    if target in CONFIG:
        return [target]
    if target in GROUPS:
        return GROUPS[target]
    return []

def clamp(v, lo, hi): return max(lo, min(hi, v))

def read_state_for(name):
    """Return live Yeelight properties for a bulb name from CONFIG via BulbPool."""
    b = BULBS.get(name)
    if not b:
        return None
    try:
        props = b.get_properties(["power", "bright", "ct", "rgb", "hue", "sat", "color_mode"])
        # yeelight returns strings; keep them as-is so the frontend can compare 'on'/'off'
        return {
            "power": props.get("power"),
            "bright": props.get("bright"),
            "ct": props.get("ct"),
            "rgb": props.get("rgb"),
            "hue": props.get("hue"),
            "sat": props.get("sat"),
            "color_mode": props.get("color_mode"),
        }
    except Exception as e:
        print(f"[warn] read_state error for {name}: {e}")
        return None


@app.route("/api/bulbs", methods=["GET"])
def list_bulbs():
    return jsonify(CONFIG)

@app.route("/api/groups", methods=["GET"])
def list_groups():
    return jsonify(GROUPS)

@app.route("/api/scene/<scene>", methods=["POST"])
def run_scene(scene):
    steps = SCENES.get(scene)
    if not steps:
        return jsonify({"error":"unknown scene"}), 404
    for step in steps:
        targets = names_or_group(step["target"])
        for name in targets:
            b = BULBS.get(name)
            if not b: continue
            cmd = step["cmd"]
            args = step.get("args", [])
            try:
                getattr(b, cmd)(*args) if hasattr(b, cmd) else b.set_rgb(*args)
            except Exception as e:
                print("scene step error", name, cmd, e)
        # optional step delay
        time.sleep(step.get("sleep", 0)/1000 if "sleep" in step else 0)
    return jsonify({"ok": True})

@app.route("/api/power/<target>/<state>", methods=["POST"])
def power(target, state):
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    for n in names:
        b = BULBS.get(n)
        if not b: continue
        b.turn_on() if state == "on" else b.turn_off()
    return jsonify({"ok": True})

@app.route("/api/bright/<target>", methods=["POST"])
def bright(target):
    val = clamp(int(request.args.get("level", 50)), 1, 100)
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    for n in names:
        b = BULBS.get(n)
        if not b: continue
        b.set_brightness(val)
    return jsonify({"ok": True})

@app.route("/api/ct/<target>", methods=["POST"])
def ct(target):
    k = clamp(int(request.args.get("k", 4000)), 1700, 6500)
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    for n in names:
        b = BULBS.get(n)
        if not b: continue
        b.set_color_temp(k)
    return jsonify({"ok": True})

@app.route("/api/rgb/<target>", methods=["POST"])
def rgb(target):
    r = clamp(int(request.args.get("r", 255)), 0, 255)
    g = clamp(int(request.args.get("g", 255)), 0, 255)
    bl = clamp(int(request.args.get("b", 255)), 0, 255)
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    for n in names:
        b = BULBS.get(n)
        if not b: continue
        b.set_rgb(r, g, bl)
    return jsonify({"ok": True})

@app.route("/api/toggle/<target>", methods=["POST"])
def toggle(target):
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    for n in names:
        b = BULBS.get(n)
        if not b: continue
        b.toggle()
    return jsonify({"ok": True})

# Quick animation demo using Flow (works on color bulbs)
@app.route("/api/pulse/<target>", methods=["POST"])
def pulse(target):
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    f = Flow(
        count=3,
        transitions=[
            transitions.rgb_transition(255,0,0, 500),
            transitions.sleep_transition(200),
            transitions.rgb_transition(0,0,255, 500),
            transitions.sleep_transition(200),
        ],
    )
    for n in names:
        b = BULBS.get(n)
        if not b: continue
        b.start_flow(f)
    return jsonify({"ok": True})

@app.route("/api/scan", methods=["POST"])
def api_scan():
    new_cfg = build_config()
    return jsonify({"ok": True, "count": len(new_cfg), "names": list(new_cfg.keys())})

@app.route("/api/state", methods=["GET"])
def state_all():
    out = {}
    for name in CONFIG.keys():
        st = read_state_for(name)
        if st is not None:
            out[name] = st
    return jsonify(out)

@app.route("/api/state/<target>", methods=["GET"])
def state_target(target):
    # single bulb
    if target in CONFIG:
        st = read_state_for(target)
        return jsonify({target: st} if st else {})

    # group aggregate (tri-state info + member states)
    if target in GROUPS:
        members = GROUPS[target]
        states = {}
        any_on = False
        all_on = True
        any_unknown = False
        for m in members:
            st = read_state_for(m)
            states[m] = st
            if not st or st.get("power") not in ("on", "off"):
                any_unknown = True
                all_on = False
            else:
                is_on = (st["power"] == "on")
                any_on = any_on or is_on
                all_on = all_on and is_on
        tri = "on" if all_on else ("off" if (not any_on and not any_unknown) else "mixed")
        return jsonify({"group": target, "tri": tri, "members": states})

    return jsonify({"error": "unknown target"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5006)

