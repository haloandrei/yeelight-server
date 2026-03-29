import json, os, threading, time, subprocess, socket
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from flask import Flask, request, jsonify
from yeelight import Bulb, discover_bulbs, Flow, transitions

CFG_FILE = "bulbs.json"          # runtime config (name -> {ip,id})
SEED_FILE = "bulbs.seed.json"    # your initial mapping
GROUPS_FILE = "groups.json"      # optional groups
SCENES_FILE = "scenes.json"      # optional scenes
STATE_FILE = "state.json"        # persisted bulb state (name -> {power,bright,ct,rgb,...})
PRESENCE_FILE = "presence.json"  # presence automation config
ROUTINES_FILE = "routines.json"  # routine configs

app = Flask(__name__)

def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

STATE_LOCK = threading.Lock()
PRESENCE_LOCK = threading.Lock()
ROUTINES_LOCK = threading.Lock()
STATE_CACHE_LOCK = threading.Lock()
STATE_REFRESH_LOCK = threading.Lock()
BULB_LOCKS_LOCK = threading.Lock()
COMMAND_BACKOFF_LOCK = threading.Lock()

def _env_int(name, fallback):
    raw = os.getenv(name)
    if raw is None:
        return fallback
    try:
        return int(raw)
    except (TypeError, ValueError):
        return fallback

ACTION_WORKERS = max(2, _env_int("ACTION_WORKERS", 8))
STATE_CACHE_MS = max(0, _env_int("STATE_CACHE_MS", 1200))
USE_MUSIC_MODE = os.getenv("YEE_USE_MUSIC_MODE", "0").strip().lower() in ("1", "true", "yes", "on")
COMMAND_TIMEOUT_SEC = max(0.2, _env_int("COMMAND_TIMEOUT_MS", 2500) / 1000.0)
COMMAND_LOCK_TIMEOUT_SEC = max(0.05, _env_int("COMMAND_LOCK_TIMEOUT_MS", 250) / 1000.0)
COMMAND_QUOTA_BACKOFF_SEC = max(0.2, _env_int("COMMAND_QUOTA_BACKOFF_MS", 1500) / 1000.0)
DISCOVERY_TIMEOUT_SEC = max(1, _env_int("DISCOVERY_TIMEOUT_SEC", 2))
COMMAND_PORT = max(1, _env_int("YEE_PORT", 55443))
CONTROL_RETRY_DELAY_SEC = max(0.05, _env_int("CONTROL_RETRY_DELAY_MS", 320) / 1000.0)
POWER_BURST_GAP_SEC = max(0.01, _env_int("POWER_BURST_GAP_MS", 180) / 1000.0)
COLOR_WAKE_DELAY_SEC = max(0.02, _env_int("COLOR_WAKE_DELAY_MS", 180) / 1000.0)

STATE_CACHE = {"at": 0.0, "data": {}}
BULB_LOCKS = {}
COMMAND_BACKOFF_UNTIL = {}
APPLY_EXECUTOR = ThreadPoolExecutor(max_workers=ACTION_WORKERS)

def _cmd_output(cmd):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        return (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")
    except Exception:
        return ""

def _neigh_entry(ip, iface=None):
    cmd = ["ip", "neigh", "show", ip]
    if iface:
        cmd += ["dev", iface]
    out = _cmd_output(cmd).strip()
    if not out:
        return None
    line = out.splitlines()[0]
    parts = line.split()
    lladdr = None
    if "lladdr" in parts:
        idx = parts.index("lladdr")
        if idx + 1 < len(parts):
            lladdr = parts[idx + 1]
    state = parts[-1] if parts else ""
    return {"state": state, "lladdr": lladdr, "line": line}

def _ping_ip(ip, iface=None):
    try:
        cmd = ["ping", "-c", "1", "-W", "1"]
        if iface:
            cmd += ["-I", iface]
        cmd.append(ip)
        res = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return res.returncode == 0
    except Exception:
        return False

def _parse_time_hhmm(value):
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute

def _time_in_window(now_minutes, start_minutes, end_minutes):
    if start_minutes is None or end_minutes is None:
        return False
    if start_minutes <= end_minutes:
        return start_minutes <= now_minutes < end_minutes
    # window crosses midnight
    return now_minutes >= start_minutes or now_minutes < end_minutes

def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def _rgb_int_to_list(rgb_int):
    if rgb_int is None:
        return None
    return [(rgb_int >> 16) & 255, (rgb_int >> 8) & 255, rgb_int & 255]

def _normalize_rgb(value):
    if isinstance(value, list) and len(value) == 3:
        try:
            return [int(max(0, min(255, v))) for v in value]
        except (TypeError, ValueError):
            return None
    rgb_int = _to_int(value)
    return _rgb_int_to_list(rgb_int)

def _merge_state(live, persisted):
    if not persisted:
        return live
    if not live:
        return persisted
    merged = dict(persisted)
    for k, v in live.items():
        if v is not None:
            merged[k] = v
    return merged

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
        # Seed only bootstraps missing fields; discovery/runtime stays authoritative.
        if ent.get("id") and not me.get("id"): me["id"] = ent["id"]
        if ent.get("ip") and not me.get("ip"): me["ip"] = ent["ip"]
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
PERSISTED = load_json(STATE_FILE, {})
GROUPS = load_json(GROUPS_FILE, {})   # e.g. {"kitchen":["kitchen_1","kitchen_2"], "tv":["tv_left","tv_right"]}
SCENES = load_json(SCENES_FILE, {})   # e.g. {"movie":[{"target":"tv","cmd":"set_bright","args":[20]}, {"target":"tv","cmd":"set_ct_abx","args":[2700,"smooth",500]}]}

DEFAULT_PRESENCE = {
    "enabled": False,
    "device_name": "",
    "device_mac": "",
    "device_iface": "",
    "start_time": "19:00",
    "end_time": "06:00",
    "target": "all",
    "routine": "boost",
    "poll_interval_sec": 30,
    "cooldown_sec": 0,
}

def _ensure_presence_defaults(cfg):
    merged = dict(DEFAULT_PRESENCE)
    if isinstance(cfg, dict):
        for k in merged:
            if k in cfg:
                merged[k] = cfg[k]
    return merged

PRESENCE_CONFIG = _ensure_presence_defaults(load_json(PRESENCE_FILE, {}))
PRESENCE_STATUS = {
    "present": False,
    "last_seen": 0,
    "last_trigger": 0,
    "last_error": "",
    "window_active": False,
    "seen_absent": False,
    "absent_since": 0,
}

DEFAULT_ROUTINES = {
    "sleep": {
        "target": "all",
        "duration_min": 30,
        "start_bright": 80,
        "end_bright": 10,
        "start_ct": 3500,
        "end_ct": 2200,
    },
    "wake": {
        "target": "all",
        "duration_min": 30,
        "start_bright": 10,
        "end_bright": 100,
        "start_ct": 2200,
        "end_ct": 5000,
    },
    "boost": {
        "target": "all",
        "duration_min": 2,
        "start_bright": 20,
        "end_bright": 100,
        "start_ct": 2700,
        "end_ct": 6000,
    },
}

def _ensure_routines_defaults(cfg):
    merged = json.loads(json.dumps(DEFAULT_ROUTINES))
    if isinstance(cfg, dict):
        for name, base in merged.items():
            incoming = cfg.get(name)
            if isinstance(incoming, dict):
                for key in base:
                    if key in incoming:
                        base[key] = incoming[key]
    return merged

ROUTINES_CONFIG = _ensure_routines_defaults(load_json(ROUTINES_FILE, {}))
ROUTINES_STATUS = {"running": {}, "last_run": {}, "running_targets": {}}
ROUTINES_CANCEL = {}

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
            # Music mode is optional; default off for stability on bulbs that enforce strict client quotas.
            if USE_MUSIC_MODE:
                try:
                    b.start_music()
                except Exception:
                    pass
            self._pool[name] = b
        return b

    def clear(self):
        for bulb in self._pool.values():
            try:
                bulb.stop_music()
            except Exception:
                pass
        self._pool = {}

    def evict(self, name):
        bulb = self._pool.pop(name, None)
        if not bulb:
            return
        try:
            bulb.stop_music()
        except Exception:
            pass

BULBS = BulbPool()

def save_state():
    with STATE_LOCK:
        save_json(STATE_FILE, PERSISTED)

def update_persisted(name, patch):
    if not name or not isinstance(patch, dict):
        return
    with STATE_LOCK:
        current = PERSISTED.get(name)
        if not isinstance(current, dict):
            current = {}
        if "rgb" in patch:
            patch["rgb"] = _normalize_rgb(patch.get("rgb"))
        changed = False
        for k, v in patch.items():
            if v is None:
                continue
            if current.get(k) != v:
                current[k] = v
                changed = True
        if changed:
            PERSISTED[name] = current
            save_json(STATE_FILE, PERSISTED)

def get_persisted(name):
    with STATE_LOCK:
        ent = PERSISTED.get(name)
        return dict(ent) if isinstance(ent, dict) else None

def device_present(name, device_mac=None, device_iface=None):
    if not name:
        return False
    name = str(name).strip()
    if not name:
        return False
    name_lower = name.lower()

    resolved_ip = None
    for host in (name, f"{name}.local"):
        try:
            resolved_ip = socket.gethostbyname(host)
            break
        except Exception:
            continue

    if resolved_ip:
        entry = _neigh_entry(resolved_ip, device_iface)
        if entry:
            if device_mac and entry.get("lladdr") and entry["lladdr"].lower() != device_mac.lower():
                return False
            if entry["state"] in ("REACHABLE", "DELAY", "PROBE"):
                return True
        _ping_ip(resolved_ip, device_iface)
        entry = _neigh_entry(resolved_ip, device_iface)
        if entry:
            if device_mac and entry.get("lladdr") and entry["lladdr"].lower() != device_mac.lower():
                return False
            return entry["state"] in ("REACHABLE", "DELAY", "PROBE")
        return False

    if device_mac or device_iface:
        return False

    neigh = _cmd_output(["ip", "neigh", "show"])
    if neigh and name_lower in neigh.lower():
        return True

    arp = _cmd_output(["arp", "-a"])
    if arp and name_lower in arp.lower():
        return True

    return False

def _presence_targets(target):
    if not target or target == "all":
        return list(CONFIG.keys())
    return names_or_group(target)

def _bulb_lock(name):
    with BULB_LOCKS_LOCK:
        lock = BULB_LOCKS.get(name)
        if lock is None:
            lock = threading.Lock()
            BULB_LOCKS[name] = lock
        return lock

def invalidate_state_cache():
    with STATE_CACHE_LOCK:
        STATE_CACHE["at"] = 0.0
        STATE_CACHE["data"] = {}

def _quota_backoff_remaining(name):
    now = time.time()
    with COMMAND_BACKOFF_LOCK:
        until = COMMAND_BACKOFF_UNTIL.get(name, 0.0)
    if until <= now:
        return 0.0
    return until - now

def _set_quota_backoff(name):
    with COMMAND_BACKOFF_LOCK:
        COMMAND_BACKOFF_UNTIL[name] = time.time() + COMMAND_QUOTA_BACKOFF_SEC

def _discovery_state_from_caps(caps):
    if not isinstance(caps, dict):
        return None
    color_mode = _to_int(caps.get("color_mode"))
    ct = _to_int(caps.get("ct"))
    rgb = _normalize_rgb(caps.get("rgb"))
    if color_mode == 2:
        rgb = None
    elif color_mode in (1, 3):
        ct = None
    return {
        "power": caps.get("power"),
        "bright": _to_int(caps.get("bright")),
        "ct": ct,
        "rgb": rgb,
        "hue": _to_int(caps.get("hue")),
        "sat": _to_int(caps.get("sat")),
        "color_mode": color_mode,
    }

def _refresh_state_snapshot(force=False):
    now = time.time()
    cache_ttl = STATE_CACHE_MS / 1000.0
    if not force and cache_ttl > 0:
        with STATE_CACHE_LOCK:
            cached_at = STATE_CACHE.get("at", 0.0)
            cached_data = STATE_CACHE.get("data")
            if cached_data and (now - cached_at) <= cache_ttl:
                return cached_data

    # Keep only one in-flight discovery refresh to prevent fan-out spikes.
    acquired = STATE_REFRESH_LOCK.acquire(blocking=False)
    if not acquired:
        with STATE_CACHE_LOCK:
            cached_data = STATE_CACHE.get("data") or {}
            if cached_data:
                return cached_data
        fallback = {}
        for name in CONFIG.keys():
            st = get_persisted(name)
            if st:
                fallback[name] = st
        return fallback

    try:
        discovered = discover_bulbs(timeout=DISCOVERY_TIMEOUT_SEC)
        by_id = {}
        by_ip = {}
        for item in discovered:
            ip = item.get("ip")
            caps = item.get("capabilities", {})
            bid = caps.get("id")
            if bid:
                by_id[bid] = item
            if ip:
                by_ip[ip] = item

        snapshot = {}
        cfg_changed = False
        for name, ent in CONFIG.items():
            match = None
            bid = ent.get("id")
            ip = ent.get("ip")
            if bid and bid in by_id:
                match = by_id[bid]
            elif ip and ip in by_ip:
                match = by_ip[ip]

            live = None
            if match:
                caps = match.get("capabilities", {})
                live = _discovery_state_from_caps(caps)
                seen_ip = match.get("ip")
                if seen_ip and ent.get("ip") != seen_ip:
                    ent["ip"] = seen_ip
                    CONFIG[name] = ent
                    cfg_changed = True

            persisted = get_persisted(name) or {}
            merged = _merge_state(live or {}, persisted)
            if merged:
                snapshot[name] = merged
            elif persisted:
                snapshot[name] = persisted

        if cfg_changed:
            save_json(CFG_FILE, CONFIG)

        with STATE_CACHE_LOCK:
            STATE_CACHE["at"] = time.time()
            STATE_CACHE["data"] = snapshot
        return snapshot
    except Exception as e:
        print(f"[warn] state discovery refresh failed: {e}")
        with STATE_CACHE_LOCK:
            cached_data = STATE_CACHE.get("data") or {}
            if cached_data:
                return cached_data
        fallback = {}
        for name in CONFIG.keys():
            st = get_persisted(name)
            if st:
                fallback[name] = st
        return fallback
    finally:
        STATE_REFRESH_LOCK.release()

def refresh_bulb_ip(name):
    ent = CONFIG.get(name)
    if not ent:
        return False
    bid = ent.get("id")
    if not bid:
        return False
    try:
        for b in discover_bulbs(timeout=DISCOVERY_TIMEOUT_SEC):
            did = b.get("capabilities", {}).get("id")
            if did != bid:
                continue
            new_ip = b.get("ip")
            if not new_ip:
                return False
            if ent.get("ip") != new_ip:
                ent["ip"] = new_ip
                CONFIG[name] = ent
                save_json(CFG_FILE, CONFIG)
            BULBS.evict(name)
            print(f"[net] refreshed {name} ip={new_ip}")
            return True
    except Exception as e:
        print(f"[warn] refresh_bulb_ip error for {name}: {e}")
    return False

def _send_command_once(name, method, params):
    ent = CONFIG.get(name)
    if not ent:
        return False, "unknown_target"

    backoff = _quota_backoff_remaining(name)
    if backoff > 0:
        return False, f"quota_backoff_{backoff:.2f}s"

    lock = _bulb_lock(name)
    if not lock.acquire(timeout=COMMAND_LOCK_TIMEOUT_SEC):
        return False, "busy"

    sock = None
    try:
        ip = ent.get("ip")
        if not ip and refresh_bulb_ip(name):
            ip = CONFIG.get(name, {}).get("ip")
        if not ip:
            return False, "ip_missing"

        req = {
            "id": int(time.time() * 1000) % 1_000_000_000,
            "method": method,
            "params": params,
        }
        payload = (json.dumps(req, separators=(",", ":")) + "\r\n").encode()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(COMMAND_TIMEOUT_SEC)
        sock.connect((ip, COMMAND_PORT))
        sock.sendall(payload)

        try:
            raw = sock.recv(4096)
        except socket.timeout:
            return False, "timeout"

        if not raw:
            return False, "empty_response"

        line = raw.decode(errors="ignore").strip().splitlines()[0]
        try:
            parsed = json.loads(line)
        except Exception:
            return False, "invalid_response"

        if isinstance(parsed, dict) and parsed.get("error"):
            err_obj = parsed.get("error")
            if isinstance(err_obj, dict):
                msg = err_obj.get("message") or str(err_obj)
            else:
                msg = str(err_obj)
            msg_text = str(msg)
            if "client quota exceeded" in msg_text.lower():
                _set_quota_backoff(name)
            return False, msg_text

        return True, None
    except Exception as e:
        text = str(e)
        if "timed out" in text.lower():
            return False, "timeout"
        return False, text
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        lock.release()

def _run_with_retry(name, action_name, op, retries=1, retry_delay=0.05, refresh_ip=False):
    attempts = max(1, int(retries))
    delay = max(0.0, float(retry_delay))
    last_error = "unknown_error"
    for attempt in range(1, attempts + 1):
        ok, err = op(name)
        if ok:
            return True, None
        last_error = str(err or "unknown_error")
        print(f"[warn] {action_name} error target={name} attempt={attempt}/{attempts}: {last_error}")
        if attempt < attempts:
            err_low = last_error.lower()
            if refresh_ip and ("ip_missing" in err_low or "network" in err_low or "refused" in err_low or "timeout" in err_low):
                refresh_bulb_ip(name)
            if delay > 0:
                time.sleep(delay * attempt)
    return False, last_error

def _apply_targets(names, action_name, op, retries=1, retry_delay=0.05, refresh_ip=False):
    success = []
    errors = []

    def _task(n):
        return n, _run_with_retry(
            n,
            action_name,
            op,
            retries=retries,
            retry_delay=retry_delay,
            refresh_ip=refresh_ip,
        )

    future_to_name = {APPLY_EXECUTOR.submit(_task, n): n for n in names}
    for fut, fallback_name in future_to_name.items():
        try:
            n, (ok, err) = fut.result()
            if ok:
                success.append(n)
            else:
                errors.append({"target": n, "error": err})
        except Exception as e:
            n = fallback_name or "unknown"
            errors.append({"target": n, "error": str(e)})
    return success, errors

def _routine_targets(target):
    if not target or target == "all":
        return list(CONFIG.keys())
    return names_or_group(target)

def _routine_worker(name, cfg, cancel_event):
    try:
        ROUTINES_STATUS["running"][name] = True
        targets = _routine_targets(cfg.get("target"))
        if not targets:
            return
        ROUTINES_STATUS["running_targets"][name] = targets
        duration_min = _to_int(cfg.get("duration_min")) or 30
        duration_sec = max(60, duration_min * 60)
        steps = max(1, int(duration_sec))
        interval = 1

        start_bright = clamp(_to_int(cfg.get("start_bright")) or 1, 1, 100)
        end_bright = clamp(_to_int(cfg.get("end_bright")) or 1, 1, 100)
        start_ct = clamp(_to_int(cfg.get("start_ct")) or 1700, 1700, 6500)
        end_ct = clamp(_to_int(cfg.get("end_ct")) or 1700, 1700, 6500)

        for i in range(steps):
            if cancel_event.is_set():
                break
            t = i / (steps - 1) if steps > 1 else 1
            bright = int(round(start_bright + (end_bright - start_bright) * t))
            ct = int(round(start_ct + (end_ct - start_ct) * t))
            for n in targets:
                b = BULBS.get(n)
                if not b:
                    continue
                try:
                    if cancel_event.is_set():
                        break
                    b.turn_on()
                    b.set_brightness(bright)
                    b.set_color_temp(ct)
                    update_persisted(n, {"power": "on", "bright": bright, "ct": ct, "color_mode": 2})
                except Exception as e:
                    print(f"[warn] routine error for {name}:{n} {e}")
            time.sleep(interval)
    finally:
        ROUTINES_STATUS["running"][name] = False
        ROUTINES_STATUS["running_targets"][name] = []
        ROUTINES_STATUS["last_run"][name] = time.time()

def start_routine(name, override=None):
    with ROUTINES_LOCK:
        cfg = ROUTINES_CONFIG.get(name)
        if not isinstance(cfg, dict):
            return False
        running = ROUTINES_STATUS.get("running", {})
        if any(running.values()):
            print(f"[routine] start blocked: another routine already running (requested={name})")
            return False
        if running.get(name):
            return False
        cfg_copy = dict(cfg)
    if isinstance(override, dict):
        for key, value in override.items():
            if value is not None:
                cfg_copy[key] = value
    print(f"[routine] start {name} target={cfg_copy.get('target')} duration_min={cfg_copy.get('duration_min')}")
    cancel_event = threading.Event()
    ROUTINES_CANCEL[name] = cancel_event
    threading.Thread(target=_routine_worker, args=(name, cfg_copy, cancel_event), daemon=True).start()
    return True

def stop_routine(name):
    cancel = ROUTINES_CANCEL.get(name)
    if cancel:
        print(f"[routine] stop {name}")
        cancel.set()
        return True
    return False

def stop_routines_for_targets(targets):
    if not targets:
        return
    running_targets = ROUTINES_STATUS.get("running_targets", {})
    for routine_name, routine_targets in running_targets.items():
        if not ROUTINES_STATUS.get("running", {}).get(routine_name):
            continue
        if not routine_targets:
            continue
        if any(t in routine_targets for t in targets):
            print(f"[routine] stop due to power off targets={targets} routine={routine_name}")
            stop_routine(routine_name)

def _presence_trigger(cfg):
    targets = _presence_targets(cfg.get("target"))
    if not targets:
        return False
    routine_name = cfg.get("routine") or "boost"
    if routine_name and routine_name in ROUTINES_CONFIG:
        running = bool(ROUTINES_STATUS.get("running", {}).get(routine_name))
        if running:
            return True
        if start_routine(routine_name, {"target": cfg.get("target")}):
            return True
    for n in targets:
        b = BULBS.get(n)
        if not b:
            continue
        try:
            b.turn_on()
            update_persisted(n, {"power": "on"})
        except Exception as e:
            print(f"[warn] presence power error for {n}: {e}")
    return True

def _presence_loop():
    while True:
        with PRESENCE_LOCK:
            cfg = dict(PRESENCE_CONFIG)
        poll = _to_int(cfg.get("poll_interval_sec")) or DEFAULT_PRESENCE["poll_interval_sec"]

        if cfg.get("enabled"):
            now = datetime.now()
            now_minutes = now.hour * 60 + now.minute
            start_minutes = _parse_time_hhmm(cfg.get("start_time"))
            end_minutes = _parse_time_hhmm(cfg.get("end_time"))
            if start_minutes is None or end_minutes is None:
                PRESENCE_STATUS["present"] = False
                PRESENCE_STATUS["last_error"] = "invalid_time_window"
                time.sleep(poll)
                continue
            window_active = _time_in_window(now_minutes, start_minutes, end_minutes)
            prev_window_active = bool(PRESENCE_STATUS.get("window_active"))
            if not window_active:
                PRESENCE_STATUS["present"] = False
                PRESENCE_STATUS["last_error"] = ""
                PRESENCE_STATUS["window_active"] = False
                PRESENCE_STATUS["seen_absent"] = False
                PRESENCE_STATUS["absent_since"] = 0
                time.sleep(poll)
                continue
            if not prev_window_active:
                PRESENCE_STATUS["window_active"] = True
                PRESENCE_STATUS["seen_absent"] = False
                PRESENCE_STATUS["absent_since"] = 0

            if not cfg.get("device_name"):
                PRESENCE_STATUS["present"] = False
                PRESENCE_STATUS["last_error"] = "device_name_missing"
                time.sleep(poll)
                continue

            present = device_present(
                cfg.get("device_name"),
                cfg.get("device_mac"),
                cfg.get("device_iface"),
            )
            was_present = bool(PRESENCE_STATUS.get("present"))
            PRESENCE_STATUS["present"] = present
            if present:
                PRESENCE_STATUS["last_seen"] = time.time()
                if not was_present:
                    if not PRESENCE_STATUS.get("seen_absent"):
                        print(f"[presence] skip (present at window start) device={cfg.get('device_name')}")
                    else:
                        cooldown = max(0, _to_int(cfg.get("cooldown_sec")) or 0)
                        absent_since = PRESENCE_STATUS.get("absent_since") or 0
                        absent_for = (time.time() - absent_since) if absent_since else 0
                        if cooldown > 0 and absent_for < cooldown:
                            remaining = int(cooldown - absent_for)
                            print(f"[presence] cooldown active: wait {remaining}s before reconnect trigger")
                            PRESENCE_STATUS["last_error"] = "cooldown_wait"
                        else:
                            print(f"[presence] trigger device={cfg.get('device_name')} target={cfg.get('target')} routine={cfg.get('routine')}")
                            triggered = _presence_trigger(cfg)
                            if triggered:
                                PRESENCE_STATUS["last_trigger"] = time.time()
                                PRESENCE_STATUS["last_error"] = ""
            else:
                PRESENCE_STATUS["last_error"] = ""
                if was_present or not PRESENCE_STATUS.get("seen_absent"):
                    PRESENCE_STATUS["absent_since"] = time.time()
                PRESENCE_STATUS["seen_absent"] = True
        time.sleep(poll)

def names_or_group(target):
    # return a list of bulb names (expand groups)
    if target == "all":
        return list(CONFIG.keys())
    if target in CONFIG:
        return [target]
    if target in GROUPS:
        return GROUPS[target]
    return []

def clamp(v, lo, hi): return max(lo, min(hi, v))

def read_state_for(name, snapshot=None):
    source = snapshot if isinstance(snapshot, dict) else _refresh_state_snapshot(force=False)
    if isinstance(source, dict) and name in source:
        return source.get(name)
    return get_persisted(name)


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
    invalidate_state_cache()
    return jsonify({"ok": True})

@app.route("/api/power/<target>/<state>", methods=["POST"])
def power(target, state):
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    is_on = (state == "on")
    burst_raw = _to_int(request.args.get("burst"))
    retries_raw = _to_int(request.args.get("retries"))
    burst = clamp(1 if burst_raw is None else burst_raw, 1, 3)
    retries = clamp(2 if retries_raw is None else retries_raw, 1, 4)

    def _op(name):
        for i in range(burst):
            ok, err = _send_command_once(
                name,
                "set_power",
                ["on" if is_on else "off", "sudden", 0],
            )
            if not ok:
                return False, err
            if burst > 1 and i < burst - 1:
                time.sleep(POWER_BURST_GAP_SEC)
        return True, None

    success, errors = _apply_targets(
        names,
        "power",
        _op,
        retries=retries,
        retry_delay=CONTROL_RETRY_DELAY_SEC,
        refresh_ip=True,
    )
    for n in success:
        update_persisted(n, {"power": "on" if is_on else "off"})
    invalidate_state_cache()
    if not is_on:
        stop_routines_for_targets(names)
    return jsonify({
        "ok": len(errors) == 0,
        "applied": len(success),
        "failed": len(errors),
        "burst": burst,
        "retries": retries,
        "errors": errors,
    })

@app.route("/api/bright/<target>", methods=["POST"])
def bright(target):
    val_raw = _to_int(request.args.get("level"))
    val = clamp(50 if val_raw is None else val_raw, 1, 100)
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    retries_raw = _to_int(request.args.get("retries"))
    retries = clamp(2 if retries_raw is None else retries_raw, 1, 4)

    def _op(name):
        return _send_command_once(name, "set_bright", [val, "sudden", 0])

    success, errors = _apply_targets(
        names,
        "bright",
        _op,
        retries=retries,
        retry_delay=CONTROL_RETRY_DELAY_SEC,
        refresh_ip=True,
    )
    for n in success:
        update_persisted(n, {"bright": val})
    invalidate_state_cache()
    return jsonify({"ok": len(errors) == 0, "applied": len(success), "failed": len(errors), "errors": errors})

@app.route("/api/ct/<target>", methods=["POST"])
def ct(target):
    k_raw = _to_int(request.args.get("k"))
    k = clamp(4000 if k_raw is None else k_raw, 1700, 6500)
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    retries_raw = _to_int(request.args.get("retries"))
    retries = clamp(2 if retries_raw is None else retries_raw, 1, 4)

    def _op(name):
        return _send_command_once(name, "set_ct_abx", [k, "sudden", 0])

    success, errors = _apply_targets(
        names,
        "ct",
        _op,
        retries=retries,
        retry_delay=CONTROL_RETRY_DELAY_SEC,
        refresh_ip=True,
    )
    for n in success:
        update_persisted(n, {"ct": k, "color_mode": 2})
    invalidate_state_cache()
    return jsonify({"ok": len(errors) == 0, "applied": len(success), "failed": len(errors), "errors": errors})

@app.route("/api/rgb/<target>", methods=["POST"])
def rgb(target):
    r_raw = _to_int(request.args.get("r"))
    g_raw = _to_int(request.args.get("g"))
    b_raw = _to_int(request.args.get("b"))
    r = clamp(255 if r_raw is None else r_raw, 0, 255)
    g = clamp(255 if g_raw is None else g_raw, 0, 255)
    bl = clamp(255 if b_raw is None else b_raw, 0, 255)
    rgb_value = (r << 16) | (g << 8) | bl
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    wake_raw = (request.args.get("wake") or "1").strip().lower()
    wake = wake_raw not in ("0", "false", "no", "off")
    retries_raw = _to_int(request.args.get("retries"))
    retries = clamp(2 if retries_raw is None else retries_raw, 1, 4)

    def _op(name):
        woke = False
        if wake:
            st = get_persisted(name) or {}
            if st.get("power") == "off":
                ok, err = _send_command_once(name, "set_power", ["on", "sudden", 0])
                if not ok:
                    # Fallback for bulbs that are reachable but flaky on raw sockets.
                    b = BULBS.get(name)
                    if not b:
                        return False, err
                    try:
                        b.turn_on()
                    except Exception:
                        return False, err
                woke = True
                if COLOR_WAKE_DELAY_SEC > 0:
                    time.sleep(COLOR_WAKE_DELAY_SEC)
        ok, err = _send_command_once(name, "set_rgb", [rgb_value, "sudden", 0])
        if ok:
            return True, None

        # Last fallback for stubborn bulbs while preserving bounded retries.
        b = BULBS.get(name)
        if not b:
            return False, err
        try:
            if wake and not woke:
                b.turn_on()
                if COLOR_WAKE_DELAY_SEC > 0:
                    time.sleep(COLOR_WAKE_DELAY_SEC)
            b.set_rgb(r, g, bl)
            return True, None
        except Exception:
            return False, err

    success, errors = _apply_targets(
        names,
        "rgb",
        _op,
        retries=retries,
        retry_delay=CONTROL_RETRY_DELAY_SEC,
        refresh_ip=True,
    )
    for n in success:
        patch = {"rgb": [r, g, bl], "color_mode": 1}
        if wake:
            patch["power"] = "on"
        update_persisted(n, patch)
    invalidate_state_cache()
    return jsonify({"ok": len(errors) == 0, "applied": len(success), "failed": len(errors), "errors": errors})

@app.route("/api/toggle/<target>", methods=["POST"])
def toggle(target):
    names = names_or_group(target)
    if not names: return jsonify({"error":"unknown target"}), 404
    retries_raw = _to_int(request.args.get("retries"))
    retries = clamp(2 if retries_raw is None else retries_raw, 1, 4)

    def _op(name):
        return _send_command_once(name, "toggle", [])

    success, errors = _apply_targets(
        names,
        "toggle",
        _op,
        retries=retries,
        retry_delay=CONTROL_RETRY_DELAY_SEC,
        refresh_ip=True,
    )
    for n in success:
        prev = get_persisted(n)
        if prev and prev.get("power") in ("on", "off"):
            update_persisted(n, {"power": "off" if prev.get("power") == "on" else "on"})
    invalidate_state_cache()
    return jsonify({"ok": len(errors) == 0, "applied": len(success), "failed": len(errors), "errors": errors})

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
    invalidate_state_cache()
    return jsonify({"ok": True})

@app.route("/api/scan", methods=["POST"])
def api_scan():
    global CONFIG
    new_cfg = build_config()
    CONFIG = new_cfg
    BULBS.clear()
    invalidate_state_cache()
    return jsonify({"ok": True, "count": len(new_cfg), "names": list(new_cfg.keys())})

@app.route("/api/state", methods=["GET"])
def state_all():
    return jsonify(_refresh_state_snapshot(force=False))

@app.route("/api/state/<target>", methods=["GET"])
def state_target(target):
    snapshot = _refresh_state_snapshot(force=False)

    # single bulb
    if target in CONFIG:
        st = read_state_for(target, snapshot=snapshot)
        return jsonify({target: st} if st else {})

    # group aggregate (tri-state info + member states)
    if target in GROUPS:
        members = GROUPS[target]
        states = {}
        any_on = False
        all_on = True
        any_unknown = False
        for m in members:
            st = read_state_for(m, snapshot=snapshot)
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


@app.route("/api/presence", methods=["GET"])
def presence_get():
    with PRESENCE_LOCK:
        cfg = dict(PRESENCE_CONFIG)
    status = dict(PRESENCE_STATUS)
    return jsonify({"config": cfg, "status": status})

@app.route("/api/presence", methods=["POST"])
def presence_set():
    payload = request.get_json(silent=True) or {}
    allowed = {"enabled", "device_name", "device_mac", "device_iface", "start_time", "end_time", "target", "routine", "poll_interval_sec", "cooldown_sec"}
    with PRESENCE_LOCK:
        for key in allowed:
            if key in payload:
                PRESENCE_CONFIG[key] = payload[key]
        # normalize defaults for any missing keys
        merged = _ensure_presence_defaults(PRESENCE_CONFIG)
        PRESENCE_CONFIG.clear()
        PRESENCE_CONFIG.update(merged)
        save_json(PRESENCE_FILE, PRESENCE_CONFIG)
    return jsonify({"ok": True, "config": dict(PRESENCE_CONFIG)})


@app.route("/api/routines", methods=["GET"])
def routines_get():
    with ROUTINES_LOCK:
        cfg = json.loads(json.dumps(ROUTINES_CONFIG))
    status = json.loads(json.dumps(ROUTINES_STATUS))
    return jsonify({"config": cfg, "status": status})

@app.route("/api/routines", methods=["POST"])
def routines_set():
    payload = request.get_json(silent=True) or {}
    with ROUTINES_LOCK:
        for name, base in DEFAULT_ROUTINES.items():
            incoming = payload.get(name)
            if not isinstance(incoming, dict):
                continue
            current = ROUTINES_CONFIG.get(name, {})
            for key in base:
                if key in incoming:
                    current[key] = incoming[key]
            ROUTINES_CONFIG[name] = current
        ROUTINES_CONFIG.update(_ensure_routines_defaults(ROUTINES_CONFIG))
        save_json(ROUTINES_FILE, ROUTINES_CONFIG)
    return jsonify({"ok": True, "config": json.loads(json.dumps(ROUTINES_CONFIG))})

@app.route("/api/routine/<name>/start", methods=["POST"])
def routine_start(name):
    target = request.args.get("target")
    override = {"target": target} if target else None
    ok = start_routine(name, override)
    if not ok:
        return jsonify({"error": "unable to start"}), 400
    return jsonify({"ok": True})

@app.route("/api/routine/<name>/stop", methods=["POST"])
def routine_stop(name):
    ok = stop_routine(name)
    if not ok:
        return jsonify({"error": "unable to stop"}), 400
    return jsonify({"ok": True})


_presence_thread = threading.Thread(target=_presence_loop, daemon=True)
_presence_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5006)
