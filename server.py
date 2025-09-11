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

# 1) Build bulbs.json (merge seed with autodiscovery by id)
def build_config():
    seed = load_json(SEED_FILE, {})
    cfg  = load_json(CFG_FILE, {})
    # index existing by id/ip so we don't duplicate
    by_id = {v.get("id"): k for k, v in cfg.items() if v.get("id")}
    by_ip = {v.get("ip"): k for k, v in cfg.items() if v.get("ip")}
    # start with cfg (persistent)
    merged = dict(cfg)
    # ensure seed entries exist
    for name, ent in seed.items():
        merged[name] = ent
    # enrich with discovery: attach id/ip pairs
    for b in discover_bulbs():
        ip = b["ip"]
        bid = b["capabilities"].get("id")
        # if we already have this IP named, attach id
        if ip in by_ip:
            merged[by_ip[ip]]["id"] = bid
        else:
            # if ID exists somewhere, attach IP there
            if bid in by_id:
                merged[by_id[bid]]["ip"] = ip
            else:
                # unnamed bulb -> assign a temp name
                merged.setdefault(f"bulb_{ip.replace('.','_')}", {"ip": ip, "id": bid})
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5006)

