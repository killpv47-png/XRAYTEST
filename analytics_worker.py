import subprocess
import os
import time
import json
import hashlib
import threading
import base64
import uuid
import secrets
import re
import sys
import shutil
import io
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse, quote

# ─────────────────────────────────────────────
# مسیرها و مقادیر پایه
# ─────────────────────────────────────────────
STATE_PATH = "killpv2_state.json"
XRAY_CONFIG_PATH = "/usr/local/etc/xray/config.json"
XRAY_LOG_PATH = "/usr/local/etc/xray/xray_runtime.log"

DEFAULT_CLEAN_IP = "172.64.149.23"
PANEL_USER_DEFAULT = "admin"
PANEL_PASS_DEFAULT = "AZHAN8585@#@#ABOL1234"
SESSION_TOKEN = secrets.token_hex(16)

SUB_REPO_NAME_DEFAULT = "your-username/your-sub-repo"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ADMIN_ID = os.environ.get("TELEGRAM_ADMIN_ID", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
SUB_REPO_TOKEN_ENV = os.environ.get("SUB_REPO_TOKEN", "")

CLOUDFLARED_BIN = "./cloudflared"
if not os.path.exists(CLOUDFLARED_BIN):
    for candidate in ["/usr/local/bin/cloudflared", "cloudflared"]:
        found = shutil.which(candidate)
        if found:
            CLOUDFLARED_BIN = found
            break

PRIVATE_TUNNEL_LOG_DIR = "/tmp/killpv2_private_tunnels"
os.makedirs(PRIVATE_TUNNEL_LOG_DIR, exist_ok=True)

STATE_LOCK = threading.Lock()

RUNTIME = {
    "tunnel_host": "127.0.0.1",
    "runner_host": "127.0.0.1",
    "main_tunnel_proc": None,
    "main_tunnel_log": "cloudflare_edge.log",
}

USER_PRIVATE_TUNNELS = {}
USER_LIVE_IPS = {}
USER_TARGET_SITES = {}
SYSTEM_LIVE_LOGS = []
RUNNER_LIVE_LOGS = ["🔄 سیستم تست رانر آماده است."]
DPI_BLOCK_LOGS = []
CHANNEL_STREAM_STATE = {"msg_id": None, "events": []}
LAST_XRAY_CONFIG_HASH = None

IP_REGEX = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):\d+')
DOMAIN_REGEX = re.compile(r'(?:tcp|udp|tls|http):([a-zA-Z0-9.-]+\.[a-zA-Z]{2,12})|->\s*([a-zA-Z0-9.-]+\.[a-zA-Z]{2,12})', re.IGNORECASE)
REAL_TRAFFIC_REGEX = re.compile(
    r'(?:uplink[:\s]+(\d+).*?downlink[:\s]+(\d+))|(?:size[:\s]+(\d+))|(?:uploaded[:\s]+(\d+))',
    re.IGNORECASE
)
DPI_RESET_REGEX = re.compile(
    r'(connection reset|reset by peer|broken pipe|EOF|closed prematurely|handshake failed|tls.*failed|i/o timeout|context deadline)',
    re.IGNORECASE
)


# ─────────────────────────────────────────────
# ابزار گیت — با لاگ واقعی خطاها (دیگه چیزی قایم نمیشه)
# ─────────────────────────────────────────────
def run_git(cmd, cwd=None):
    try:
        res = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=40)
        if res.stdout.strip():
            print(res.stdout.strip(), flush=True)
        if res.stderr.strip():
            print(res.stderr.strip(), flush=True)
        return res.returncode == 0
    except Exception as e:
        print(f"⚠️ git error: {e}", flush=True)
        return False


def git_commit_and_push(message, paths):
    run_git("git config --local user.email 'actions@github.com'")
    run_git("git config --local user.name 'GitHub Action'")
    for p in paths:
        run_git(f"git add {p}")
    check = subprocess.run("git diff --cached --quiet", shell=True)
    if check.returncode == 0:
        return True
    if not run_git(f'git commit -m "{message}"'):
        return False
    if not run_git("git push"):
        print("⚠️ push اول شکست خورد، تلاش با rebase...", flush=True)
        run_git("git pull --rebase --autostash")
        return run_git("git push")
    return True


# ─────────────────────────────────────────────
# دیتای یکپارچه — یک فایل، همه‌چیز داخلش
# ─────────────────────────────────────────────
def default_state():
    return {
        "panel_db": {
            "Main_kill_pv2_8086": {
                "uuid": str(uuid.uuid4()),
                "total_limit_bytes": 0,
                "used_bytes": 0,
                "clean_ip": DEFAULT_CLEAN_IP,
                "custom_host": "",
                "status": "OFFLINE",
                "last_active_time": 0,
                "down_speed": 0,
                "up_speed": 0,
                "created_at": int(time.time()),
                "expire_seconds": 31536000,
                "active": True,
                "coefficient": 1.0,
                "real_traffic": False,
                "max_ips": 2,
                "is_proxy_type": False,
                "use_runner_balancer": False,
                "optimization": False,
                "private_tunnel_enabled": False,
                "private_tunnel_host": ""
            }
        },
        "giveaway": {
            "max_claims": 0, "volume_value": 0.0, "volume_unit": "GB",
            "volume_gb": 0.0, "claimed_count": 0, "claimed_users": [],
            "status": "inactive", "channel_msg_id": None
        },
        "system_config": {
            "panel_user": PANEL_USER_DEFAULT,
            "panel_pass": PANEL_PASS_DEFAULT,
            "default_clean_ip": DEFAULT_CLEAN_IP,
            "traffic_coefficient": 1.0,
            "sub_repo_name": SUB_REPO_NAME_DEFAULT,
            "sub_repo_token": SUB_REPO_TOKEN_ENV,
            "telegram_bot_token": TELEGRAM_BOT_TOKEN,
            "telegram_admin_id": TELEGRAM_ADMIN_ID,
            "telegram_channel_id": TELEGRAM_CHANNEL_ID,
        },
        "combined_subs": {}
    }


def migrate_legacy_files(state):
    """اگه فایل‌های قدیمی جدا وجود داشتن، یک‌بار وارد فایل یکپارچه‌شون می‌کنیم."""
    legacy_map = {
        "panel_db.json": "panel_db",
        "giveaway_config.json": "giveaway",
        "system_config.json": "system_config",
        "combined_subs.json": "combined_subs",
    }
    changed = False
    for fname, key in legacy_map.items():
        if os.path.exists(fname):
            try:
                with open(fname, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if data:
                    state[key] = data
                    changed = True
                    print(f"📦 Migrated legacy file: {fname}", flush=True)
            except Exception as e:
                print(f"⚠️ Could not migrate {fname}: {e}", flush=True)
    return changed


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            base = default_state()
            for k in base:
                if k not in data:
                    data[k] = base[k]
            return data
        except Exception as e:
            print(f"⚠️ خواندن {STATE_PATH} شکست خورد: {e}", flush=True)
    state = default_state()
    if migrate_legacy_files(state):
        pass
    return state


def save_state(commit_msg="💾 Sync state [skip ci]"):
    with STATE_LOCK:
        try:
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(STATE, f, indent=2, ensure_ascii=False)
            os.replace(tmp, STATE_PATH)
        except Exception as e:
            print(f"⚠️ نوشتن state شکست خورد: {e}", flush=True)
            return
        git_commit_and_push(commit_msg, [STATE_PATH])


STATE = load_state()


def cfg():
    return STATE["system_config"]


def db():
    return STATE["panel_db"]


# ─────────────────────────────────────────────
# توابع کمکی
# ─────────────────────────────────────────────
def format_bytes_display(b):
    if b >= 1024 ** 3:
        return f"{b / (1024 ** 3):.2f} GB"
    if b >= 1024 ** 2:
        return f"{b / (1024 ** 2):.2f} MB"
    if b >= 1024:
        return f"{b / 1024:.2f} KB"
    return f"{b} B"


def get_server_resources():
    cpu_pct, ram_pct = 0.0, 0.0
    try:
        with open('/proc/meminfo', 'r') as f:
            m = f.read()
        t = re.search(r'MemTotal:\s+(\d+)', m)
        a = re.search(r'MemAvailable:\s+(\d+)', m)
        if t and a:
            total, avail = int(t.group(1)), int(a.group(1))
            ram_pct = ((total - avail) / total) * 100
        with open('/proc/stat', 'r') as f:
            l1 = f.readline().split()
        time.sleep(0.05)
        with open('/proc/stat', 'r') as f:
            l2 = f.readline().split()
        id1 = int(l1[4]) + int(l1[5])
        tot1 = sum(int(x) for x in l1[1:8])
        id2 = int(l2[4]) + int(l2[5])
        tot2 = sum(int(x) for x in l2[1:8])
        if tot2 - tot1 > 0:
            cpu_pct = (1 - (id2 - id1) / (tot2 - tot1)) * 100
    except Exception:
        pass
    if cpu_pct == 0.0:
        cpu_pct = secrets.randbelow(12) + 4
    if ram_pct == 0.0:
        ram_pct = secrets.randbelow(15) + 30
    return round(cpu_pct, 1), round(ram_pct, 1)


def generate_qr_png_bytes(text_data):
    try:
        import qrcode
        qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=8, border=2)
        qr.add_data(text_data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        return buf
    except Exception as e:
        print(f"⚠️ QR generation failed: {e}", flush=True)
        return None


def push_channel_event(text):
    CHANNEL_STREAM_STATE["events"].append(f"{time.strftime('%H:%M:%S')} — {text}")
    if len(CHANNEL_STREAM_STATE["events"]) > 15:
        CHANNEL_STREAM_STATE["events"] = CHANNEL_STREAM_STATE["events"][-15:]


def is_xray_core_running():
    try:
        out = subprocess.check_output("pgrep xray || pidof xray", shell=True)
        return len(out.strip()) > 0
    except Exception:
        return False


# ─────────────────────────────────────────────
# ساخت لینک‌ها — یک منبع واحد برای همه‌جا
# (رفع باگ QR: انکود درست + host/sni همیشه ست میشه)
# ─────────────────────────────────────────────
def get_effective_host(username, v):
    if v.get("private_tunnel_enabled") and v.get("private_tunnel_host", "").strip():
        return v["private_tunnel_host"].strip()
    if v.get("use_runner_balancer"):
        return RUNTIME["runner_host"]
    return v.get("custom_host", "").strip() or RUNTIME["runner_host"] or RUNTIME["tunnel_host"]


def build_vless_link(username, v, direct=False):
    t_host = get_effective_host(username, v)
    connect_host = t_host if direct else v.get("clean_ip", cfg()["default_clean_ip"])
    suffix = ""
    if v.get("optimization"):
        suffix += "_Opt"
    if v.get("private_tunnel_enabled"):
        suffix += "_Priv"
    if direct:
        suffix += "_Direct"
    remark = quote(f"{username}{suffix}", safe='')
    query = (
        f"path={quote('/killpv2', safe='')}&security=tls&encryption=none"
        f"&type=ws&host={t_host}&sni={t_host}"
    )
    return f"vless://{v.get('uuid', '')}@{connect_host}:443?{query}#{remark}"


def build_socks_link(username, v):
    user_q = quote(username, safe='')
    pass_q = quote(v.get('uuid', ''), safe='')
    remark = quote(f"{username}_Telegram_Proxy", safe='')
    return f"socks5://{user_q}:{pass_q}@{RUNTIME['tunnel_host']}:8089#{remark}"


def build_tg_proxy_link(username, v):
    return (
        f"https://t.me/socks?server={RUNTIME['tunnel_host']}&port=8089"
        f"&user={quote(username)}&pass={quote(v.get('uuid', ''))}"
    )


def build_info_link(username, v, label, value):
    t_host = get_effective_host(username, v)
    c_ip = v.get("clean_ip", cfg()["default_clean_ip"])
    remark = quote(f"{label}:{value}", safe='')
    query = f"path=%2Fkillpv2&security=tls&encryption=none&type=ws&host={t_host}&sni={t_host}"
    return f"vless://{v.get('uuid', '')}@{c_ip}:443?{query}#{remark}"


def build_payload_for_user(username, v, now):
    if not v.get("active", True):
        return "// ACCOUNT EXPIRED OR DISABLED\n"
    if v.get("is_proxy_type", False):
        return build_socks_link(username, v) + "\n" + build_tg_proxy_link(username, v) + "\n"

    total_bytes = v.get("total_limit_bytes", 0)
    used_bytes = v.get("used_bytes", 0)
    rem_bytes = max(0, total_bytes - used_bytes) if total_bytes > 0 else 0
    passed = now - v.get("created_at", now)
    rem_seconds = max(0, v.get("expire_seconds", 2592000) - passed)
    rem_d, rem_h = int(rem_seconds // 86400), int((rem_seconds % 86400) // 3600)

    lines = [
        build_vless_link(username, v, direct=False),
        build_vless_link(username, v, direct=True),
        build_info_link(username, v, "Used", format_bytes_display(used_bytes)),
        build_info_link(username, v, "Left", format_bytes_display(rem_bytes) if total_bytes > 0 else "Unlimited"),
        build_info_link(username, v, "Time", f"{rem_d}d{rem_h}h"),
    ]
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────
# تونل اصلی — با واچ‌داگ که خودش دوباره وصل می‌کنه
# ─────────────────────────────────────────────
def read_host_from_log(log_path, timeout_sec=35):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            with open(log_path, 'r') as f:
                content = f.read()
            m = re.search(r'https://([a-zA-Z0-9.-]+\.trycloudflare\.com)', content)
            if m:
                return m.group(1)
        except Exception:
            pass
        time.sleep(1)
    return None


def spawn_main_tunnel():
    try:
        subprocess.run("pkill -f 'cloudflared tunnel --url http://127.0.0.1:8080' || true", shell=True)
        time.sleep(1)
        log_path = RUNTIME["main_tunnel_log"]
        log_f = open(log_path, 'w')
        proc = subprocess.Popen(
            f"{CLOUDFLARED_BIN} tunnel --url http://127.0.0.1:8080 --no-autoupdate",
            shell=True, stdout=log_f, stderr=subprocess.STDOUT
        )
        host = read_host_from_log(log_path, 35)
        if host:
            RUNTIME["main_tunnel_proc"] = proc
            RUNTIME["tunnel_host"] = host
            with open("active_edge_host.txt", "w") as f:
                f.write(host)
            print(f"✅ Main tunnel (re)connected: {host}", flush=True)
            push_channel_event(f"🌐 تونل اصلی متصل/بازسازی شد: {host}")
            return host
        return None
    except Exception as e:
        print(f"⚠️ spawn_main_tunnel failed: {e}", flush=True)
        return None


def main_tunnel_watchdog():
    """
    هر ۲۰ ثانیه چک می‌کنه تونل اصلی زنده‌ست یا نه.
    اگه مرده باشه (یا هیچ‌وقت جواب نده)، خودش دوباره می‌سازتش
    بدون اینکه منتظر ری‌استارت کل ورک‌فلو بمونیم.
    """
    fail_count = 0
    while True:
        time.sleep(20)
        try:
            host = RUNTIME["tunnel_host"]
            if host == "127.0.0.1":
                continue
            res = subprocess.run(
                f"curl -s -o /dev/null -w '%{{http_code}}' -k --connect-timeout 5 https://{host}/killpv2",
                shell=True, capture_output=True, text=True
            )
            code = res.stdout.strip()
            if code in ["200", "301", "302", "400", "403", "404"]:
                fail_count = 0
                continue
            fail_count += 1
            print(f"⚠️ Main tunnel health check failed ({fail_count}/2), code={code}", flush=True)
            if fail_count >= 2:
                print("🔄 Rebuilding main tunnel...", flush=True)
                new_host = spawn_main_tunnel()
                if new_host:
                    fail_count = 0
                    push_subs_to_github()
        except Exception as e:
            print(f"⚠️ watchdog error: {e}", flush=True)


# ─────────────────────────────────────────────
# تونل‌های خصوصی هر کاربر
# ─────────────────────────────────────────────
def spawn_private_tunnel_for_user(username):
    try:
        kill_private_tunnel_for_user(username)
        if not CLOUDFLARED_BIN or not shutil.which(CLOUDFLARED_BIN):
            print(f"⚠️ cloudflared binary not found for {username}", flush=True)
            return None

        log_path = os.path.join(PRIVATE_TUNNEL_LOG_DIR, f"{username}_{int(time.time())}.log")
        log_f = open(log_path, 'w')
        proc = subprocess.Popen(
            f"{CLOUDFLARED_BIN} tunnel --url http://127.0.0.1:8080 --no-autoupdate",
            shell=True, stdout=log_f, stderr=subprocess.STDOUT
        )
        host = read_host_from_log(log_path, 35)
        if host:
            USER_PRIVATE_TUNNELS[username] = {"process": proc, "host": host, "log_file": log_path}
            print(f"✅ Private tunnel created for {username}: {host}", flush=True)
            push_channel_event(f"🆕 تونل اختصاصی ساخته شد برای {username}: {host}")
            return host
        try:
            proc.kill()
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"⚠️ spawn_private_tunnel_for_user failed for {username}: {e}", flush=True)
        return None


def kill_private_tunnel_for_user(username):
    entry = USER_PRIVATE_TUNNELS.get(username)
    if entry:
        try:
            entry["process"].kill()
        except Exception:
            pass
        USER_PRIVATE_TUNNELS.pop(username, None)


def private_tunnels_watchdog():
    while True:
        time.sleep(30)
        for username, entry in list(USER_PRIVATE_TUNNELS.items()):
            proc = entry.get("process")
            if proc and proc.poll() is not None:
                print(f"⚠️ تونل خصوصی {username} افتاده، دوباره می‌سازم...", flush=True)
                new_host = spawn_private_tunnel_for_user(username)
                if new_host and username in db():
                    db()[username]["private_tunnel_host"] = new_host
                    save_state()
                    push_subs_to_github()


def bootstrap_private_tunnels_on_startup():
    changed = False
    for u_name, u_data in list(db().items()):
        if u_data.get("private_tunnel_enabled") and u_data.get("active", True):
            u_data["private_tunnel_host"] = ""
            changed = True
    if changed:
        save_state()

    for u_name, u_data in list(db().items()):
        if u_data.get("private_tunnel_enabled") and u_data.get("active", True):
            print(f"🔄 Bootstrapping private tunnel for {u_name}...", flush=True)
            new_host = spawn_private_tunnel_for_user(u_name)
            u_data["private_tunnel_host"] = new_host or ""
    save_state()


# ─────────────────────────────────────────────
# پوش کردن ساب‌ها به ریپوی جدا
# ─────────────────────────────────────────────
def push_subs_to_github():
    try:
        now = int(time.time())
        temp_dir = "/tmp/sub_secure_push_8086"
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)
        os.makedirs(temp_dir, exist_ok=True)

        for k, v in db().items():
            payload = build_payload_for_user(k, v, now)
            with open(os.path.join(temp_dir, k), 'w') as sf:
                sf.write(base64.b64encode(payload.encode('utf-8')).decode('utf-8'))

        for combo_name, usernames in STATE["combined_subs"].items():
            lines = []
            for un in usernames:
                if un in db() and db()[un].get("active", True):
                    v = db()[un]
                    if v.get("is_proxy_type", False):
                        lines.append(build_socks_link(un, v))
                    else:
                        lines.append(build_vless_link(un, v, direct=False))
            payload = "\n".join(lines) + "\n"
            with open(os.path.join(temp_dir, f"combo_{combo_name}"), 'w') as sf:
                sf.write(base64.b64encode(payload.encode('utf-8')).decode('utf-8'))

        repo_name = cfg().get("sub_repo_name", "")
        repo_token = cfg().get("sub_repo_token", "")
        if repo_name and repo_token and "/" in repo_name:
            try:
                git_dir = "/tmp/git_push_8086"
                if os.path.exists(git_dir):
                    shutil.rmtree(git_dir)
                os.makedirs(git_dir, exist_ok=True)
                for item in os.listdir(temp_dir):
                    shutil.copy(os.path.join(temp_dir, item), os.path.join(git_dir, item))
                cwd = os.getcwd()
                os.chdir(git_dir)
                run_git("git init -q")
                run_git("git config --local user.email 'actions@github.com'")
                run_git("git config --local user.name 'GitHub Action'")
                run_git("git checkout -q -b main")
                run_git("git add .")
                run_git('git commit -q -m "🔗 Update Subscriptions [skip ci]"')
                remote_url = f"https://{repo_token}@github.com/{repo_name}.git"
                run_git(f'git push "{remote_url}" main --force')
                os.chdir(cwd)
                shutil.rmtree(git_dir)
            except Exception as e:
                print(f"⚠️ push_subs_to_github (sub repo) failed: {e}", flush=True)

        shutil.rmtree(temp_dir, ignore_errors=True)
    except Exception as e:
        print(f"⚠️ push_subs_to_github failed: {e}", flush=True)


# ─────────────────────────────────────────────
# انقضا و محدودیت‌ها
# ─────────────────────────────────────────────
def check_expiration_and_limits():
    now = int(time.time())
    changed = False
    for u_name, u_data in list(db().items()):
        total_limit = u_data.get("total_limit_bytes", 0)
        if total_limit > 0 and u_data.get("used_bytes", 0) >= total_limit:
            if u_data.get("active", True) or u_data.get("status") != "EXPIRED":
                u_data["active"] = False
                u_data["status"] = "EXPIRED"
                changed = True
            continue

        created_time = u_data.get("created_at", now)
        expire_seconds = u_data.get("expire_seconds", 2592000)
        if now - created_time > expire_seconds:
            if u_data.get("active", True) or u_data.get("status") != "EXPIRED":
                u_data["active"] = False
                u_data["status"] = "EXPIRED"
                changed = True
            continue

        live_ips_count = len(USER_LIVE_IPS.get(u_name, {}))
        max_allowed_ips = int(u_data.get("max_ips", 2))
        if live_ips_count > max_allowed_ips:
            if u_data.get("active", True):
                u_data["active"] = False
                u_data["status"] = "IP_LIMIT_EXCEEDED"
                changed = True
        else:
            if u_data.get("status") == "IP_LIMIT_EXCEEDED" and not u_data.get("active", True):
                u_data["active"] = True
                u_data["status"] = "OFFLINE"
                changed = True

    if changed:
        save_state()
        sync_xray_core()
        push_subs_to_github()


# ─────────────────────────────────────────────
# سینک هسته Xray — فقط وقتی واقعاً چیزی تغییر کرده ریستارت میشه
# ─────────────────────────────────────────────
def sync_xray_core(force=False):
    global LAST_XRAY_CONFIG_HASH

    vless_clients = [
        {"id": u_data.get("uuid", ""), "email": u_name, "level": 0}
        for u_name, u_data in db().items()
        if u_data.get("active", True) and not u_data.get("is_proxy_type", False)
    ]
    proxy_users = [
        {"user": u_name, "pass": u_data.get("uuid", "")}
        for u_name, u_data in db().items()
        if u_data.get("active", True) and u_data.get("is_proxy_type", False)
    ]
    any_optimized = any(
        u_data.get("optimization", False)
        for u_data in db().values() if u_data.get("active", True)
    )

    sockopt_config = {
        "tcpKeepAliveInterval": 20,
        "tcpKeepAliveIdle": 60,
        "tcpNoDelay": True,
        "domainStrategy": "UseIP" if any_optimized else "AsIs",
    }
    if any_optimized:
        sockopt_config["tcpFastOpen"] = True
        sockopt_config["tcpcongestion"] = "bbr"
        sockopt_config["tcpMptcp"] = True

    xray_json_config = {
        "log": {"loglevel": "info", "access": XRAY_LOG_PATH, "error": XRAY_LOG_PATH},
        "policy": {
            "levels": {"0": {"handshake": 4, "connIdle": 600, "uplinkOnly": 5, "downlinkOnly": 10, "bufferSize": 4}},
            "system": {"statsInboundUplink": False, "statsInboundDownlink": False}
        },
        "inbounds": [
            {
                "port": 8085, "protocol": "vless",
                "settings": {"clients": vless_clients, "decryption": "none"},
                "streamSettings": {
                    "network": "ws",
                    "wsSettings": {"path": "/killpv2", "headers": {}},
                    "sockopt": sockopt_config
                },
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"], "routeOnly": False}
            },
            {
                "port": 8089, "protocol": "socks",
                "settings": {
                    "auth": "password" if proxy_users else "noauth",
                    "accounts": proxy_users, "udp": True
                },
                "streamSettings": {"sockopt": sockopt_config},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls"]}
            }
        ],
        "outbounds": [{
            "protocol": "freedom", "tag": "direct_out",
            "settings": {"domainStrategy": "UseIP" if any_optimized else "AsIs"},
            "streamSettings": {"sockopt": sockopt_config}
        }]
    }

    config_hash = hashlib.sha256(json.dumps(xray_json_config, sort_keys=True).encode()).hexdigest()
    if not force and config_hash == LAST_XRAY_CONFIG_HASH and is_xray_core_running():
        return
    LAST_XRAY_CONFIG_HASH = config_hash

    with open(XRAY_CONFIG_PATH, 'w') as f:
        json.dump(xray_json_config, f, indent=4)

    subprocess.run("sudo fuser -k 8085/tcp || true", shell=True)
    subprocess.run("sudo fuser -k 8089/tcp || true", shell=True)
    subprocess.run(f"sudo touch {XRAY_LOG_PATH} && sudo chmod 777 {XRAY_LOG_PATH}", shell=True)
    subprocess.run(f"sudo nohup /usr/local/bin/xray -config {XRAY_CONFIG_PATH} > /dev/null 2>&1 &", shell=True)
    push_channel_event("🔄 هسته Xray ریلود شد")


# ─────────────────────────────────────────────
# ترد آنالیز لاگ Xray برای مصرف واقعی/تخمینی
# ─────────────────────────────────────────────
def xray_live_log_sniffer():
    while not os.path.exists(XRAY_LOG_PATH):
        time.sleep(1)

    log_file = open(XRAY_LOG_PATH, "r")
    log_file.seek(0, os.SEEK_END)

    while True:
        line = log_file.readline()
        if not line:
            time.sleep(0.05)
            continue
        clean_line = line.strip()
        if not clean_line:
            continue

        SYSTEM_LIVE_LOGS.append(clean_line)
        if len(SYSTEM_LIVE_LOGS) > 100:
            SYSTEM_LIVE_LOGS.pop(0)

        if DPI_RESET_REGEX.search(clean_line):
            DPI_BLOCK_LOGS.append(f"[{time.strftime('%H:%M:%S')}] {clean_line}")
            if len(DPI_BLOCK_LOGS) > 200:
                DPI_BLOCK_LOGS.pop(0)

        changed = False
        for user_name, u_data in list(db().items()):
            user_uuid = u_data.get("uuid", "")
            if user_name not in clean_line and (not user_uuid or user_uuid not in clean_line):
                continue
            if not (u_data.get("active", True) or u_data.get("status") == "IP_LIMIT_EXCEEDED"):
                continue

            u_data["last_active_time"] = time.time()
            if u_data.get("status") != "IP_LIMIT_EXCEEDED":
                u_data["status"] = "ONLINE"

            ip_match = IP_REGEX.search(clean_line)
            if ip_match:
                USER_LIVE_IPS.setdefault(user_name, {})[ip_match.group(1)] = time.time()

            domain_match = DOMAIN_REGEX.search(clean_line)
            if domain_match:
                dst = domain_match.group(1) or domain_match.group(2)
                if dst and not dst.startswith("127.") and "cloudflare" not in dst:
                    lst = USER_TARGET_SITES.setdefault(user_name, [])
                    if dst not in lst:
                        lst.append(dst)

            if not u_data.get("active", True):
                continue

            is_real = u_data.get("real_traffic", False)
            u_coef = u_data.get("coefficient", cfg().get("traffic_coefficient", 1.0))
            traffic_match = REAL_TRAFFIC_REGEX.search(clean_line)

            if is_real:
                if traffic_match:
                    uplink = int(traffic_match.group(1) or 0)
                    downlink = int(traffic_match.group(2) or 0)
                    size_val = int(traffic_match.group(3) or 0)
                    uploaded_val = int(traffic_match.group(4) or 0)
                    if uplink > 0 or downlink > 0:
                        u_data["used_bytes"] += uplink + downlink
                        u_data["down_speed"] = downlink
                        u_data["up_speed"] = uplink
                        changed = True
                    elif size_val > 0:
                        u_data["used_bytes"] += size_val
                        u_data["down_speed"] = int(size_val * 0.85)
                        u_data["up_speed"] = int(size_val * 0.15)
                        changed = True
                    elif uploaded_val > 0:
                        u_data["used_bytes"] += uploaded_val
                        u_data["down_speed"] = int(uploaded_val * 0.8)
                        u_data["up_speed"] = int(uploaded_val * 0.2)
                        changed = True
            else:
                if traffic_match:
                    uplink = int(traffic_match.group(1) or 0)
                    downlink = int(traffic_match.group(2) or 0)
                    size_val = int(traffic_match.group(3) or 0)
                    uploaded_val = int(traffic_match.group(4) or 0)
                    base_bytes = (uplink + downlink) or size_val or uploaded_val
                else:
                    base_bytes = 0
                if base_bytes > 0:
                    u_data["used_bytes"] += int(base_bytes * u_coef)
                    u_data["down_speed"] = int(base_bytes * 1.5 * u_coef)
                    u_data["up_speed"] = int(base_bytes * 0.2 * u_coef)
                else:
                    fake_bytes = secrets.randbelow(3000) + 500
                    u_data["used_bytes"] += int(fake_bytes * u_coef)
                    u_data["down_speed"] = secrets.randbelow(800000) + 200000
                    u_data["up_speed"] = secrets.randbelow(20000) + 30000
                changed = True

        if changed:
            with STATE_LOCK:
                try:
                    tmp = STATE_PATH + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        json.dump(STATE, f, indent=2, ensure_ascii=False)
                    os.replace(tmp, STATE_PATH)
                except Exception as e:
                    print(f"⚠️ quick save failed: {e}", flush=True)


def speed_and_ip_cleaner():
    while True:
        time.sleep(4)
        now = time.time()
        for u_name in list(USER_LIVE_IPS.keys()):
            for ip_addr, last_seen in list(USER_LIVE_IPS[u_name].items()):
                if now - last_seen > 10:
                    del USER_LIVE_IPS[u_name][ip_addr]

        p_changed = False
        for u_name, u_data in list(db().items()):
            if now - u_data.get("last_active_time", 0) > 8:
                if u_data.get("down_speed", 0) > 0 or u_data.get("up_speed", 0) > 0:
                    u_data["down_speed"] = 0
                    u_data["up_speed"] = 0
                    p_changed = True
            if now - u_data.get("last_active_time", 0) > 130:
                if u_data.get("status") not in ["OFFLINE", "EXPIRED", "IP_LIMIT_EXCEEDED"]:
                    u_data["status"] = "OFFLINE"
                    p_changed = True
        if p_changed:
            save_state()


# ─────────────────────────────────────────────
# HTTP Server
# ─────────────────────────────────────────────
class SanaeiMobileXuiServer(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def is_authenticated(self):
        cookies = self.headers.get('Cookie', '')
        return f"session={SESSION_TOKEN}" in cookies

    def _redirect(self, location):
        self.send_response(303)
        self.send_header('Location', location)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/terminal":
            if not self.is_authenticated():
                self.send_response(403); self.end_headers(); return
            length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(length).decode('utf-8')
            params = parse_qs(post_data)
            cmd = params.get('command', [''])[0].strip()
            output = ""
            if cmd:
                try:
                    res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=12)
                    output = res.stdout if res.stdout else res.stderr
                    if not output.strip():
                        output = "✔ دستور با موفقیت اجرا شد (بدون خروجی)."
                except subprocess.TimeoutExpired:
                    output = "❌ خطا: زمان اجرا تمام شد."
                except Exception as e:
                    output = f"💥 خطای اجرا: {e}"
            else:
                output = "⚠️ خط فرمان خالی است."
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"output": output}).encode('utf-8'))
            return

        length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(length).decode('utf-8')
        params = parse_qs(post_data)
        action = params.get('action', [''])[0]

        if self.path == "/login":
            username = params.get('username', [''])[0].strip()
            password = params.get('password', [''])[0].strip()
            if username == cfg()["panel_user"] and password == cfg()["panel_pass"]:
                self.send_response(303)
                self.send_header('Set-Cookie', f'session={SESSION_TOKEN}; Path=/; HttpOnly')
                self.send_header('Location', '/')
                self.end_headers()
            else:
                self._redirect('/?error=true')
            return

        if not self.is_authenticated():
            self._redirect('/')
            return

        if action == 'save_system_settings':
            c = cfg()
            c["panel_user"] = params.get('panel_user', [c["panel_user"]])[0].strip() or c["panel_user"]
            c["panel_pass"] = params.get('panel_pass', [c["panel_pass"]])[0].strip() or c["panel_pass"]
            c["default_clean_ip"] = params.get('default_clean_ip', [c["default_clean_ip"]])[0].strip() or c["default_clean_ip"]
            try:
                c["traffic_coefficient"] = float(params.get('traffic_coefficient', [str(c["traffic_coefficient"])])[0])
            except Exception:
                pass
            c["sub_repo_name"] = params.get('sub_repo_name', [c["sub_repo_name"]])[0].strip() or c["sub_repo_name"]
            new_token = params.get('sub_repo_token', [''])[0].strip()
            if new_token:
                c["sub_repo_token"] = new_token
            save_state("⚙️ Update system settings [skip ci]")
            push_channel_event("⚙️ تنظیمات عمومی سیستم بروزرسانی شد")
            self._redirect('/?saved=settings')
            return

        if action == 'save_telegram_settings':
            c = cfg()
            new_token = params.get('telegram_bot_token', [''])[0].strip()
            new_admin = params.get('telegram_admin_id', [''])[0].strip()
            new_channel = params.get('telegram_channel_id', [''])[0].strip()
            if new_token:
                c["telegram_bot_token"] = new_token
            if new_admin:
                c["telegram_admin_id"] = new_admin
            if new_channel:
                c["telegram_channel_id"] = new_channel
            save_state("🤖 Update telegram settings [skip ci]")
            push_channel_event("🤖 تنظیمات ربات تلگرام بروزرسانی شد")
            self._redirect('/?saved=telegram')
            return

        if action == 'build_combined_sub':
            combo_name = params.get('combo_name', [''])[0].strip() or f"combo_{int(time.time())}"
            combo_name = re.sub(r'\s+', '_', combo_name)
            selected_users = params.get('selected_users', [])
            if selected_users:
                STATE["combined_subs"][combo_name] = selected_users
                save_state("🔗 Update combined subs [skip ci]")
                push_subs_to_github()
                push_channel_event(f"🔗 ساب ترکیبی ساخته شد: {combo_name}")
            self._redirect(f'/?combo_built=1&combo_name={combo_name}')
            return

        if action == 'delete_combined_sub':
            combo_name = params.get('combo_name', [''])[0].strip()
            if combo_name in STATE["combined_subs"]:
                del STATE["combined_subs"][combo_name]
                save_state("🗑️ Delete combined sub [skip ci]")
                push_subs_to_github()
                push_channel_event(f"🗑️ ساب ترکیبی حذف شد: {combo_name}")
            self._redirect('/?combo_deleted=1')
            return

        if action == 'toggle_all_runner_balancer':
            target_state = any(not v.get("use_runner_balancer", False) for v in db().values())
            for u in db().values():
                u["use_runner_balancer"] = target_state
            save_state()
            sync_xray_core()
            push_subs_to_github()
            self._redirect('/')
            return

        if action == 'toggle_all_optimization':
            target_state = any(not v.get("optimization", False) for v in db().values())
            for u in db().values():
                u["optimization"] = target_state
            save_state()
            sync_xray_core()
            push_subs_to_github()
            self._redirect('/')
            return

        if action == 'create':
            username = params.get('username', [''])[0].strip()
            if username and username not in db():
                is_unlimited = params.get('unlimited_volume', [''])[0] == 'true'
                volume_val = float(params.get('volume_value', [0])[0] or 0)
                volume_unit = params.get('volume_unit', ['GB'])[0]
                expire_days = int(params.get('expire_days', [0])[0] or 0)
                expire_hours = int(params.get('expire_hours', [0])[0] or 0)
                total_seconds = (expire_days * 86400) + (expire_hours * 3600) or 2592000
                multiplier = 1024 ** 3 if volume_unit == 'GB' else 1024 ** 2
                final_bytes = 0 if is_unlimited else int(volume_val * multiplier)
                private_tunnel_enabled = params.get('private_tunnel_enabled', [''])[0] == 'true'

                db()[username] = {
                    "uuid": str(uuid.uuid4()),
                    "total_limit_bytes": final_bytes,
                    "used_bytes": 0,
                    "clean_ip": params.get('clean_ip', [cfg()["default_clean_ip"]])[0].strip() or cfg()["default_clean_ip"],
                    "custom_host": params.get('custom_host', [''])[0].strip(),
                    "status": "OFFLINE",
                    "last_active_time": 0,
                    "down_speed": 0, "up_speed": 0,
                    "created_at": int(time.time()),
                    "expire_seconds": total_seconds,
                    "active": True,
                    "coefficient": float(params.get('coefficient', [1.0])[0] or 1.0),
                    "real_traffic": params.get('real_traffic', [''])[0] == 'true',
                    "max_ips": int(params.get('max_ips', [2])[0] or 2),
                    "is_proxy_type": params.get('is_proxy_type', [''])[0] == 'true',
                    "use_runner_balancer": params.get('use_runner_balancer', [''])[0] == 'true',
                    "optimization": params.get('optimization', [''])[0] == 'true',
                    "private_tunnel_enabled": private_tunnel_enabled,
                    "private_tunnel_host": ""
                }
                save_state()
                sync_xray_core()
                if private_tunnel_enabled:
                    new_host = spawn_private_tunnel_for_user(username)
                    db()[username]["private_tunnel_host"] = new_host or ""
                    save_state()
                push_subs_to_github()
                push_channel_event(f"➕ کلاینت جدید: {username}")
            self._redirect('/')
            return

        if action == 'edit':
            username = params.get('username', [''])[0].strip()
            if username in db():
                u = db()[username]
                is_unlimited = params.get('unlimited_volume', [''])[0] == 'true'
                volume_val = float(params.get('volume_value', [0])[0] or 0)
                used_val = float(params.get('used_value', [0])[0] or 0)
                was_private = u.get("private_tunnel_enabled", False)
                private_tunnel_enabled = params.get('private_tunnel_enabled', [''])[0] == 'true'

                u["total_limit_bytes"] = 0 if is_unlimited else int(volume_val * 1024 ** 3)
                u["used_bytes"] = int(used_val * 1024 ** 3)
                u["clean_ip"] = params.get('clean_ip', [cfg()["default_clean_ip"]])[0].strip() or cfg()["default_clean_ip"]
                u["custom_host"] = params.get('custom_host', [''])[0].strip()
                u["coefficient"] = float(params.get('coefficient', [1.0])[0] or 1.0)
                u["real_traffic"] = params.get('real_traffic', [''])[0] == 'true'
                u["max_ips"] = int(params.get('max_ips', [2])[0] or 2)
                u["use_runner_balancer"] = params.get('use_runner_balancer', [''])[0] == 'true'
                u["optimization"] = params.get('optimization', [''])[0] == 'true'
                u["private_tunnel_enabled"] = private_tunnel_enabled

                if u.get("status") in ["EXPIRED", "IP_LIMIT_EXCEEDED"]:
                    u["active"] = True
                    u["status"] = "OFFLINE"

                if private_tunnel_enabled and not was_private:
                    new_host = spawn_private_tunnel_for_user(username)
                    u["private_tunnel_host"] = new_host or ""
                elif not private_tunnel_enabled and was_private:
                    kill_private_tunnel_for_user(username)
                    u["private_tunnel_host"] = ""

                save_state()
                sync_xray_core()
                push_subs_to_github()
                push_channel_event(f"✏️ کلاینت ویرایش شد: {username}")
            self._redirect('/')
            return

        if action == 'delete':
            username = params.get('username', [''])[0].strip()
            if username in db():
                kill_private_tunnel_for_user(username)
                del db()[username]
                USER_LIVE_IPS.pop(username, None)
                USER_TARGET_SITES.pop(username, None)
                save_state()
                sync_xray_core()
                push_subs_to_github()
                push_channel_event(f"🗑️ کلاینت حذف شد: {username}")
            self._redirect('/')
            return

        if action == 'toggle':
            username = params.get('username', [''])[0].strip()
            if username in db():
                u = db()[username]
                u["active"] = not u.get("active", True)
                if not u["active"]:
                    u["status"] = "OFFLINE"
                save_state()
                sync_xray_core()
                push_subs_to_github()
                push_channel_event(f"⚙️ {username} → {'فعال' if u['active'] else 'غیرفعال'}")
            self._redirect('/')
            return

        self._redirect('/')

    def do_GET(self):
        parsed = urlparse(self.path)
        url_path = parsed.path.strip("/")
        query = parse_qs(parsed.query)

        if url_path == "api/test_runner":
            if not self.is_authenticated():
                self.send_response(403); self.end_headers(); return
            RUNNER_LIVE_LOGS.append(f"⏱️ شروع تلاش اتصال: {time.strftime('%H:%M:%S')}")
            success = False
            try:
                if os.path.exists('active_runner_host.txt'):
                    with open('active_runner_host.txt', 'r') as f:
                        host = f.read().strip()
                else:
                    host = RUNTIME["tunnel_host"]
                    with open('active_runner_host.txt', 'w') as f:
                        f.write(host)
                res = subprocess.run(
                    f"curl -s -o /dev/null -w '%{{http_code}}' -k --connect-timeout 4 https://{host}/killpv2",
                    shell=True, capture_output=True, text=True
                )
                code = res.stdout.strip()
                if code in ["200", "301", "302", "404", "403", "400"]:
                    RUNNER_LIVE_LOGS.append(f"🟢 تانل رانر زنده! کد: {code}")
                    RUNTIME["runner_host"] = host
                    success = True
                else:
                    RUNNER_LIVE_LOGS.append(f"❌ رانر پاسخ نداد. کد: {code or 'Timeout'}")
            except Exception as e:
                RUNNER_LIVE_LOGS.append(f"💥 خطا: {e}")
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps({"success": success, "logs": RUNNER_LIVE_LOGS[-20:]}).encode('utf-8'))
            return

        if url_path == "qr":
            if not self.is_authenticated():
                self.send_response(403); self.end_headers(); return
            username = query.get('u', [''])[0]
            kind = query.get('type', ['main'])[0]
            if username not in db():
                self.send_response(404); self.end_headers(); return
            v = db()[username]
            if kind == 'tg' and v.get('is_proxy_type'):
                link = build_tg_proxy_link(username, v)
            elif v.get('is_proxy_type'):
                link = build_socks_link(username, v)
            else:
                link = build_vless_link(username, v, direct=False)
            png_buf = generate_qr_png_bytes(link)
            if not png_buf:
                self.send_response(500); self.end_headers(); return
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.end_headers()
            self.wfile.write(png_buf.getvalue())
            return

        if url_path == "api/stats":
            if not self.is_authenticated():
                self.send_response(403); self.end_headers(); return
            now = int(time.time())
            response_data = []
            total_sys_bytes = sum(v.get("used_bytes", 0) for v in db().values())
            total_online = 0
            for k, v in db().items():
                is_online = (len(USER_LIVE_IPS.get(k, {})) > 0 or v.get("status") == "ONLINE") and v.get("active", True)
                if is_online:
                    total_online += 1
                total = v.get("total_limit_bytes", 0)
                used = v.get("used_bytes", 0)
                rem = max(0, total - used) if total > 0 else 0
                pct = min(100, (used / total * 100)) if total > 0 else 0
                passed = now - v.get("created_at", now)
                rem_seconds = max(0, v.get("expire_seconds", 2592000) - passed)
                rem_d, rem_h = int(rem_seconds // 86400), int((rem_seconds % 86400) // 3600)

                if v.get("is_proxy_type", False):
                    cfg_str = build_socks_link(k, v)
                else:
                    cfg_str = build_vless_link(k, v, direct=False)

                live_ips_count = len(USER_LIVE_IPS.get(k, {}))
                status_label = "🔴 آفلاین"
                if v.get("status") == "IP_LIMIT_EXCEEDED":
                    status_label = f"🚨 سقف IP ({live_ips_count}/{v.get('max_ips', 2)})"
                elif live_ips_count > 0 and v.get("active", True):
                    status_label = f"🟢 {live_ips_count} متصل"
                elif v.get("status") == "ONLINE" and v.get("active", True):
                    status_label = "🟢 متصل"
                if not v.get("active", True) and v.get("status") != "IP_LIMIT_EXCEEDED":
                    status_label = "⏳ تمام شده" if v.get("status") == "EXPIRED" else "⚫ غیرفعال"

                ds = v.get("down_speed", 0) / 1024
                us = v.get("up_speed", 0) / 1024
                ds_str = f"{ds/1024:.1f} MB/s" if ds >= 1024 else f"{ds:.1f} KB/s"
                us_str = f"{us/1024:.1f} MB/s" if us >= 1024 else f"{us:.1f} KB/s"

                response_data.append({
                    "username": k, "status": status_label,
                    "used": format_bytes_display(used),
                    "total": format_bytes_display(total) if total > 0 else "نامحدود",
                    "remaining": format_bytes_display(rem) if total > 0 else "نامحدود",
                    "rem_days": f"{rem_d} روز و {rem_h} ساعت",
                    "progress": pct, "down_speed": ds_str, "up_speed": us_str,
                    "down_speed_raw": v.get("down_speed", 0), "up_speed_raw": v.get("up_speed", 0),
                    "config_raw": cfg_str,
                    "destinations": USER_TARGET_SITES.get(k, [])[-12:],
                    "total_raw": total, "used_raw": used,
                    "clean_ip": v.get("clean_ip", cfg()["default_clean_ip"]),
                    "custom_host": v.get("custom_host", ""),
                    "coefficient": v.get("coefficient", 1.0),
                    "real_traffic": v.get("real_traffic", False),
                    "max_ips": v.get("max_ips", 2),
                    "is_proxy_type": v.get("is_proxy_type", False),
                    "use_runner_balancer": v.get("use_runner_balancer", False),
                    "optimization": v.get("optimization", False),
                    "private_tunnel_enabled": v.get("private_tunnel_enabled", False),
                    "private_tunnel_host": v.get("private_tunnel_host", ""),
                    "active": v.get("active", True),
                })

            srv_cpu, srv_ram = get_server_resources()
            payload = {
                "total_online": total_online, "users": response_data,
                "sys_logs": SYSTEM_LIVE_LOGS[-30:], "runner_logs": RUNNER_LIVE_LOGS[-20:],
                "dpi_logs": DPI_BLOCK_LOGS[-40:],
                "server_cpu": srv_cpu, "server_ram": srv_ram,
                "total_sys_used": format_bytes_display(total_sys_bytes),
                "xray_live": is_xray_core_running(),
                "tunnel_host": RUNTIME["tunnel_host"],
                "runner_host": RUNTIME["runner_host"],
                "combined_subs": STATE["combined_subs"],
                "sub_repo_name": cfg()["sub_repo_name"],
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(payload).encode('utf-8'))
            return

        if url_path.startswith("combo/"):
            combo_name = url_path.replace("combo/", "", 1)
            if combo_name in STATE["combined_subs"]:
                lines = []
                for un in STATE["combined_subs"][combo_name]:
                    if un in db() and db()[un].get("active", True):
                        v = db()[un]
                        lines.append(build_socks_link(un, v) if v.get("is_proxy_type") else build_vless_link(un, v))
                payload = "\n".join(lines) + "\n"
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(base64.b64encode(payload.encode('utf-8')))
                return
            self.send_response(404); self.end_headers(); return

        if url_path.startswith("sub/"):
            target_user = url_path.replace("sub/", "", 1)
            if target_user in db() and db()[target_user].get("active", True):
                payload = build_payload_for_user(target_user, db()[target_user], int(time.time()))
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(base64.b64encode(payload.encode('utf-8')))
                return
            self.send_response(404); self.end_headers(); return

        if not self.is_authenticated():
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            err_msg = '❌ رمز عبور اشتباه است!' if "error=true" in self.path else ''
            self.wfile.write(render_login_page(err_msg).encode('utf-8'))
            return

        if url_path in ("", "index.html"):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(render_dashboard(self.path).encode('utf-8'))
            return

        self.send_response(404)
        self.end_headers()


# ─────────────────────────────────────────────
# رندر HTML — نسخه‌ی ساده و تضمین‌شده برای کار کردن
# ─────────────────────────────────────────────
def render_login_page(err_msg):
    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ورود | kill_pv2</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 min-h-screen flex items-center justify-center">
<div class="bg-slate-900/80 border border-indigo-500/20 rounded-2xl p-8 w-full max-w-sm">
  <h1 class="text-2xl font-black text-white text-center mb-1">🛡️ kill_pv2</h1>
  <p class="text-slate-400 text-center text-sm mb-6">پنل مدیریت هوشمند</p>
  {'<div class="bg-rose-500/10 text-rose-400 text-sm rounded-lg p-2 mb-4 text-center">' + err_msg + '</div>' if err_msg else ''}
  <form method="POST" action="/login" class="space-y-3">
    <input name="username" placeholder="نام کاربری" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-4 py-2 text-white" required>
    <input name="password" type="password" placeholder="رمز عبور" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-4 py-2 text-white" required>
    <button class="w-full bg-indigo-600 hover:bg-indigo-500 text-white font-bold rounded-lg py-2">🔓 ورود</button>
  </form>
</div>
</body></html>"""


def render_dashboard(full_path):
    saved_msg = ""
    if "saved=settings" in full_path:
        saved_msg = "✅ تنظیمات عمومی ذخیره شد!"
    elif "saved=telegram" in full_path:
        saved_msg = "✅ تنظیمات ربات ذخیره شد!"
    elif "combo_built=1" in full_path:
        saved_msg = "✅ ساب ترکیبی ساخته شد!"
    elif "combo_deleted=1" in full_path:
        saved_msg = "🗑️ ساب ترکیبی حذف شد."

    c = cfg()
    client_rows = ""
    combo_checkbox_list = ""
    for user_name, v in db().items():
        priv_badge = ""
        if v.get("private_tunnel_enabled"):
            priv_badge = f'<span class="text-purple-400">🔒 {v.get("private_tunnel_host","در حال ساخت...")}</span>'
        proxy_badge = '<span class="text-amber-400 text-xs">🔌 Telegram Proxy</span>' if v.get("is_proxy_type") else ""
        active_badge = "🟢" if v.get("active", True) else "🔴"

        qr_btn = f'<button onclick="openQr(\'{user_name}\')" class="text-xs bg-slate-800 px-2 py-1 rounded">📱 QR</button>'
        if v.get("is_proxy_type"):
            qr_btn += f'<button onclick="openQr(\'{user_name}\',\'tg\')" class="text-xs bg-amber-800 px-2 py-1 rounded mr-1">📱 QR تلگرام</button>'

        client_rows += f"""
        <div class="bg-slate-900/70 border border-slate-800 rounded-xl p-4 mb-3">
          <div class="flex justify-between items-center mb-2">
            <div class="font-bold text-white">{active_badge} {user_name} {proxy_badge}</div>
            <div class="text-xs text-slate-400">{priv_badge}</div>
          </div>
          <div class="text-xs text-slate-400 mb-2">
            مصرف: {format_bytes_display(v.get('used_bytes',0))} |
            کل: {format_bytes_display(v.get('total_limit_bytes',0)) if v.get('total_limit_bytes',0) > 0 else 'نامحدود'}
          </div>
          <div class="flex flex-wrap gap-1">
            <button onclick="copyText('{('https://raw.githubusercontent.com/'+c['sub_repo_name']+'/main/'+user_name)}')" class="text-xs bg-slate-800 px-2 py-1 rounded">🔗 ساب</button>
            {qr_btn}
            <form method="POST" action="/" style="display:inline">
              <input type="hidden" name="action" value="toggle">
              <input type="hidden" name="username" value="{user_name}">
              <button class="text-xs bg-slate-800 px-2 py-1 rounded">⚙️ فعال/غیرفعال</button>
            </form>
            <form method="POST" action="/" style="display:inline" onsubmit="return confirm('حذف شود؟')">
              <input type="hidden" name="action" value="delete">
              <input type="hidden" name="username" value="{user_name}">
              <button class="text-xs bg-rose-900 px-2 py-1 rounded">🗑️ حذف</button>
            </form>
          </div>
        </div>"""

        if v.get("active", True) and not v.get("is_proxy_type"):
            combo_checkbox_list += f'<label class="flex items-center gap-2 text-sm text-slate-300"><input type="checkbox" name="selected_users" value="{user_name}"> {user_name}</label>'

    combo_list_html = ""
    for combo_name, users_list in STATE["combined_subs"].items():
        combo_list_html += f"""
        <div class="bg-slate-900/70 border border-slate-800 rounded-xl p-3 mb-2">
          <div class="flex justify-between items-center">
            <span class="text-white font-bold">🔗 {combo_name}</span>
            <form method="POST" action="/">
              <input type="hidden" name="action" value="delete_combined_sub">
              <input type="hidden" name="combo_name" value="{combo_name}">
              <button class="text-xs bg-rose-900 px-2 py-1 rounded">🗑️</button>
            </form>
          </div>
          <div class="text-xs text-slate-400 mt-1">{', '.join(users_list[:6])}</div>
          <button onclick="copyText('https://raw.githubusercontent.com/{c['sub_repo_name']}/main/combo_{combo_name}')" class="text-xs bg-slate-800 px-2 py-1 rounded mt-2">📋 کپی لینک</button>
        </div>"""

    masked_token = (c["telegram_bot_token"][:8] + "..." + c["telegram_bot_token"][-4:]) if len(c.get("telegram_bot_token","")) > 14 else c.get("telegram_bot_token","")
    masked_repo_token = (c["sub_repo_token"][:6] + "...") if c.get("sub_repo_token") else "(تنظیم نشده)"

    return f"""<!DOCTYPE html>
<html lang="fa" dir="rtl"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>kill_pv2 Panel</title>
<script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-white">
<div class="max-w-5xl mx-auto p-4">
  <h1 class="text-2xl font-black mb-1">🛡️ kill_pv2 <span class="text-sm text-slate-400 font-normal">Smart Gateway Panel</span></h1>
  {'<div class="bg-emerald-500/10 text-emerald-400 rounded-lg p-2 my-3 text-center">' + saved_msg + '</div>' if saved_msg else ''}

  <div id="stats_bar" class="grid grid-cols-2 md:grid-cols-4 gap-3 my-4 text-center">
    <div class="bg-slate-900/70 border border-slate-800 rounded-xl p-3"><div class="text-xs text-slate-400">آنلاین</div><div id="online_count" class="text-xl font-bold">0</div></div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-xl p-3"><div class="text-xs text-slate-400">CPU</div><div id="cpu_val" class="text-xl font-bold">0%</div></div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-xl p-3"><div class="text-xs text-slate-400">RAM</div><div id="ram_val" class="text-xl font-bold">0%</div></div>
    <div class="bg-slate-900/70 border border-slate-800 rounded-xl p-3"><div class="text-xs text-slate-400">Xray</div><div id="xray_status" class="text-xl font-bold">...</div></div>
  </div>

  <details class="bg-slate-900/70 border border-slate-800 rounded-xl p-4 mb-4" open>
    <summary class="cursor-pointer font-bold">➕ ساخت کلاینت جدید</summary>
    <form method="POST" action="/" class="grid grid-cols-2 gap-2 mt-3">
      <input type="hidden" name="action" value="create">
      <input name="username" placeholder="نام کاربری" class="col-span-2 bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm" required>
      <input name="volume_value" type="number" step="0.1" placeholder="حجم (GB)" class="bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
      <select name="volume_unit" class="bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm"><option value="GB">GB</option><option value="MB">MB</option></select>
      <input name="expire_days" type="number" placeholder="روز انقضا" class="bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
      <input name="max_ips" type="number" value="2" placeholder="سقف IP" class="bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="unlimited_volume" value="true"> نامحدود</label>
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="is_proxy_type" value="true"> پروکسی تلگرام (SOCKS5)</label>
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="optimization" value="true"> ⚡ OPT</label>
      <label class="flex items-center gap-2 text-sm"><input type="checkbox" name="private_tunnel_enabled" value="true"> 🔒 تونل اختصاصی</label>
      <button class="col-span-2 bg-indigo-600 hover:bg-indigo-500 rounded-lg py-2 font-bold mt-2">⚡ ایجاد و ریلود</button>
    </form>
  </details>

  <h2 class="font-bold mb-2">👤 کلاینت‌ها</h2>
  <div id="clients_list">{client_rows}</div>

  <h2 class="font-bold mt-6 mb-2">🔗 ساب‌های ترکیبی</h2>
  <form method="POST" action="/" class="bg-slate-900/70 border border-slate-800 rounded-xl p-4 mb-3">
    <input type="hidden" name="action" value="build_combined_sub">
    <input name="combo_name" placeholder="اسم ساب ترکیبی" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm mb-2">
    <div class="grid grid-cols-2 gap-1 max-h-32 overflow-y-auto mb-2">{combo_checkbox_list or '<span class="text-slate-500 text-sm">کلاینت فعالی نیست</span>'}</div>
    <button class="bg-indigo-600 rounded-lg px-4 py-2 text-sm font-bold">🔗 ساخت</button>
  </form>
  {combo_list_html}

  <h2 class="font-bold mt-6 mb-2">🤖 تنظیمات ربات تلگرام</h2>
  <form method="POST" action="/" class="bg-slate-900/70 border border-slate-800 rounded-xl p-4 mb-6 space-y-2">
    <input type="hidden" name="action" value="save_telegram_settings">
    <div class="text-xs text-slate-500">توکن فعلی: {masked_token}</div>
    <input name="telegram_bot_token" placeholder="توکن ربات جدید" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <input name="telegram_admin_id" placeholder="چت‌آیدی ادمین" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <input name="telegram_channel_id" placeholder="آیدی کانال" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <button class="bg-indigo-600 rounded-lg px-4 py-2 text-sm font-bold">💾 ذخیره</button>
  </form>

  <h2 class="font-bold mt-6 mb-2">⚙️ تنظیمات عمومی سیستم</h2>
  <form method="POST" action="/" class="bg-slate-900/70 border border-slate-800 rounded-xl p-4 mb-10 space-y-2">
    <input type="hidden" name="action" value="save_system_settings">
    <input name="panel_user" placeholder="نام کاربری پنل" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <input name="panel_pass" placeholder="رمز عبور پنل" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <input name="default_clean_ip" value="{c['default_clean_ip']}" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <input name="sub_repo_name" value="{c['sub_repo_name']}" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <div class="text-xs text-slate-500">توکن ریپو فعلی: {masked_repo_token}</div>
    <input name="sub_repo_token" placeholder="توکن جدید ریپو ساب" class="w-full bg-slate-950 border border-slate-700 rounded-lg px-3 py-2 text-sm">
    <button class="bg-indigo-600 rounded-lg px-4 py-2 text-sm font-bold">💾 ذخیره</button>
  </form>
</div>

<div id="qr_modal" class="fixed inset-0 bg-black/80 hidden items-center justify-center z-50">
  <div class="bg-white rounded-2xl p-4 text-center">
    <img id="qr_img" src="" class="w-52 h-52 mx-auto">
    <button onclick="document.getElementById('qr_modal').style.display='none'" class="mt-3 bg-slate-800 text-white px-4 py-1 rounded-lg text-sm">بستن</button>
  </div>
</div>

<script>
function copyText(t) {{
  navigator.clipboard.writeText(t).then(()=>alert('کپی شد ✅')).catch(()=>alert(t));
}}
function openQr(username, type) {{
  let url = '/qr?u=' + encodeURIComponent(username) + (type ? '&type=' + type : '');
  document.getElementById('qr_img').src = url;
  document.getElementById('qr_modal').style.display = 'flex';
}}
async function refreshStats() {{
  try {{
    let res = await fetch('/api/stats');
    let data = await res.json();
    document.getElementById('online_count').innerText = data.total_online;
    document.getElementById('cpu_val').innerText = data.server_cpu + '%';
    document.getElementById('ram_val').innerText = data.server_ram + '%';
    document.getElementById('xray_status').innerText = data.xray_live ? '🟢' : '🔴';
  }} catch(e) {{ console.error(e); }}
}}
setInterval(refreshStats, 3000);
refreshStats();
</script>
</body></html>"""


# ─────────────────────────────────────────────
# ربات تلگرام
# ─────────────────────────────────────────────
def init_telegram_bot_service():
    c = cfg()
    if not c.get("telegram_bot_token") or "YOUR_BOT_TOKEN" in c.get("telegram_bot_token", ""):
        print("⚠️ Telegram Bot Token missing. Bot bypassed.", flush=True)
        return
    try:
        import telebot
        from telebot.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton

        bot = telebot.TeleBot(c["telegram_bot_token"])
        admin_id = c.get("telegram_admin_id", "")
        channel_id = c.get("telegram_channel_id", "")

        threading.Thread(target=channel_live_stream_worker, args=(bot, channel_id), daemon=True).start()

        @bot.message_handler(commands=['start'])
        def handle_start(message):
            chat_id_str = str(message.chat.id)
            if chat_id_str == str(admin_id) and 'claim' not in (message.text or ''):
                g = STATE["giveaway"]
                markup = ReplyKeyboardMarkup(resize_keyboard=True)
                markup.row(KeyboardButton("🚀 ایجاد چالش جدید"), KeyboardButton("📊 آمار چالش"))
                markup.row(KeyboardButton("🛠️ مدیریت وضعیت چالش"), KeyboardButton("🔒 ساخت تونل اختصاصی"))
                bot.send_message(message.chat.id,
                    f"👑 سلام!\n👥 {g['claimed_count']}/{g['max_claims']}\n⚙️ {g['status']}",
                    reply_markup=markup)
                return

            if 'claim' in (message.text or ''):
                handle_claim(bot, message)
                return

            markup = ReplyKeyboardMarkup(resize_keyboard=True)
            markup.row(KeyboardButton("📊 مشاهده کانفیگ‌های من"), KeyboardButton("ℹ️ راهنما"))
            bot.send_message(message.chat.id, "👋 سلام! برای دریافت کانفیگ رایگان از لینک چالش استفاده کن.", reply_markup=markup)

        def handle_claim(bot, message):
            g = STATE["giveaway"]
            chat_id_str = str(message.chat.id)
            if g.get("status") != "active" or g["max_claims"] == 0:
                bot.send_message(message.chat.id, "❌ چالشی فعال نیست!")
                return
            if chat_id_str in g["claimed_users"]:
                bot.send_message(message.chat.id, "⚠️ قبلاً دریافت کردی!")
                return
            if g["claimed_count"] >= g["max_claims"]:
                bot.send_message(message.chat.id, "🏁 ظرفیت تموم شد.")
                return

            i = 1
            while f"primeconfigfree_{i}" in db():
                i += 1
            new_username = f"primeconfigfree_{i}"
            final_bytes = int(g["volume_gb"] * 1024 ** 3)
            db()[new_username] = {
                "uuid": str(uuid.uuid4()), "total_limit_bytes": final_bytes, "used_bytes": 0,
                "clean_ip": cfg()["default_clean_ip"], "custom_host": "", "status": "OFFLINE",
                "last_active_time": 0, "down_speed": 0, "up_speed": 0,
                "created_at": int(time.time()), "expire_seconds": 2592000, "active": True,
                "coefficient": 1.0, "real_traffic": False, "max_ips": 2, "is_proxy_type": False,
                "use_runner_balancer": False, "optimization": True,
                "private_tunnel_enabled": False, "private_tunnel_host": "",
                "tg_user_id": chat_id_str
            }
            g["claimed_count"] += 1
            g["claimed_users"].append(chat_id_str)
            if g["claimed_count"] >= g["max_claims"]:
                g["status"] = "finished"

            save_state()
            sync_xray_core()
            push_subs_to_github()
            push_channel_event(f"🎁 کلیم شد: {new_username}")

            v = db()[new_username]
            vless_link = build_vless_link(new_username, v)
            sub_link = f"https://raw.githubusercontent.com/{cfg()['sub_repo_name']}/main/{new_username}"
            bot.send_message(message.chat.id, f"🎉 تبریک!\n👤 {new_username}\n\n📋 {vless_link}\n\n🔗 {sub_link}")
            qr_buf = generate_qr_png_bytes(vless_link)
            if qr_buf:
                bot.send_photo(message.chat.id, qr_buf, caption=f"📱 QR {new_username}")

        @bot.message_handler(func=lambda m: m.text == "📊 مشاهده کانفیگ‌های من")
        def handle_user_stats(message):
            chat_id_str = str(message.chat.id)
            found = [(k, v) for k, v in db().items() if str(v.get("tg_user_id", "")) == chat_id_str]
            if not found:
                bot.send_message(message.chat.id, "⚠️ کانفیگی برای شما یافت نشد.")
                return
            resp = "📊 کانفیگ‌های شما:\n\n"
            for u_name, v in found:
                link = build_vless_link(u_name, v)
                sub_link = f"https://raw.githubusercontent.com/{cfg()['sub_repo_name']}/main/{u_name}"
                resp += f"{'🟢' if v.get('active') else '🔴'} {u_name}\n📋 {link}\n🔗 {sub_link}\n──────\n"
            bot.send_message(message.chat.id, resp)

        @bot.message_handler(func=lambda m: m.text == "ℹ️ راهنما")
        def handle_help(message):
            bot.send_message(message.chat.id, "ℹ️ اندروید: v2rayNG | آیفون: FoXray | ویندوز: v2rayN")

        @bot.message_handler(func=lambda m: str(m.chat.id) == str(admin_id) and m.text == "🔒 ساخت تونل اختصاصی")
        def handle_admin_build_tunnel(message):
            active_users = [k for k, v in db().items() if v.get("active", True) and not v.get("is_proxy_type")]
            if not active_users:
                bot.send_message(message.chat.id, "❌ کاربر فعالی نیست.")
                return
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(*[InlineKeyboardButton(u, callback_data=f"build_tunnel_{u}") for u in active_users[:20]])
            bot.send_message(message.chat.id, "👤 برای کدام کاربر تونل بسازم؟", reply_markup=markup)

        @bot.message_handler(func=lambda m: str(m.chat.id) == str(admin_id))
        def handle_admin_menu(message):
            g = STATE["giveaway"]
            if message.text == "🚀 ایجاد چالش جدید":
                msg_s = bot.send_message(message.chat.id, "🔢 ظرفیت چالش؟")
                bot.register_next_step_handler(msg_s, process_capacity_step)
            elif message.text == "📊 آمار چالش":
                bot.send_message(message.chat.id, f"👥 {g['claimed_count']}/{g['max_claims']}\n⚙️ {g['status']}")
            elif message.text == "🛠️ مدیریت وضعیت چالش":
                mk = InlineKeyboardMarkup()
                if g.get("status") == "active":
                    mk.add(InlineKeyboardButton("🛑 لغو", callback_data="tg_camp_cancel"))
                elif g.get("status") == "cancelled":
                    mk.add(InlineKeyboardButton("🟢 فعال‌سازی", callback_data="tg_camp_activate"))
                mk.add(InlineKeyboardButton("🗑️ حذف کامل", callback_data="tg_camp_delete"))
                bot.send_message(message.chat.id, f"⚙️ وضعیت: {g.get('status')}", reply_markup=mk)

        def process_capacity_step(message):
            try:
                capacity = int(message.text.strip())
                msg_s = bot.send_message(message.chat.id, "💾 مقدار حجم؟")
                bot.register_next_step_handler(msg_s, lambda m: process_volume_step(m, capacity))
            except Exception:
                bot.send_message(message.chat.id, "❌ عدد وارد کن.")

        def process_volume_step(message, capacity):
            try:
                volume_val = float(message.text.strip())
                mk = InlineKeyboardMarkup()
                mk.add(
                    InlineKeyboardButton("GB", callback_data=f"tg_unit_GB_{capacity}_{volume_val}"),
                    InlineKeyboardButton("MB", callback_data=f"tg_unit_MB_{capacity}_{volume_val}")
                )
                bot.send_message(message.chat.id, "📐 واحد؟", reply_markup=mk)
            except Exception:
                bot.send_message(message.chat.id, "❌ نامعتبر.")

        @bot.callback_query_handler(func=lambda call: True)
        def handle_callbacks(call):
            if str(call.message.chat.id) != str(admin_id):
                return

            if call.data.startswith("build_tunnel_"):
                target_user = call.data.replace("build_tunnel_", "", 1)
                if target_user not in db():
                    bot.answer_callback_query(call.id, "❌ کاربر یافت نشد!")
                    return
                bot.answer_callback_query(call.id, "🔄 در حال ساخت...")
                bot.edit_message_text(f"🔄 در حال ساخت تونل برای {target_user}...", call.message.chat.id, call.message.message_id)

                def do_build():
                    db()[target_user]["private_tunnel_enabled"] = True
                    new_host = spawn_private_tunnel_for_user(target_user)
                    if new_host:
                        db()[target_user]["private_tunnel_host"] = new_host
                        save_state()
                        sync_xray_core()
                        push_subs_to_github()
                        result = f"✅ تونل ساخته شد!\n👤 {target_user}\n🌐 {new_host}"
                    else:
                        result = f"❌ ساخت تونل برای {target_user} ناموفق بود."
                    try:
                        bot.edit_message_text(result, call.message.chat.id, call.message.message_id)
                    except Exception:
                        pass

                threading.Thread(target=do_build, daemon=True).start()
                return

            g = STATE["giveaway"]
            if call.data.startswith("tg_unit_"):
                parts = call.data.split("_")
                unit, capacity, volume_val = parts[2], int(parts[3]), float(parts[4])
                volume_gb = volume_val if unit == "GB" else volume_val / 1024.0
                STATE["giveaway"] = {
                    "max_claims": capacity, "volume_value": volume_val, "volume_unit": unit,
                    "volume_gb": volume_gb, "claimed_count": 0, "claimed_users": [],
                    "status": "active", "channel_msg_id": None
                }
                save_state()
                bot_info = bot.get_me()
                share_url = f"https://t.me/{bot_info.username}?start=claim"
                mk = InlineKeyboardMarkup()
                mk.add(InlineKeyboardButton("🎁 دریافت رایگان", url=share_url))
                if channel_id:
                    bot.send_message(channel_id, f"🚀 چالش جدید!\n👥 ظرفیت: {capacity}\n💾 {volume_val} {unit}", reply_markup=mk)
                push_channel_event(f"🚀 چالش جدید: {capacity} / {volume_val}{unit}")
                bot.answer_callback_query(call.id, "✅ ایجاد شد!")
            elif call.data == "tg_camp_cancel":
                g["status"] = "cancelled"; save_state()
                bot.answer_callback_query(call.id, "لغو شد.")
            elif call.data == "tg_camp_activate":
                g["status"] = "active"; save_state()
                bot.answer_callback_query(call.id, "فعال شد.")
            elif call.data == "tg_camp_delete":
                STATE["giveaway"] = default_state()["giveaway"]; save_state()
                bot.answer_callback_query(call.id, "حذف شد.")

        threading.Thread(target=lambda: bot.infinity_polling(timeout=20, long_polling_timeout=10), daemon=True).start()
        print("🤖 TELEGRAM BOT RUNNING", flush=True)
    except Exception as e:
        print(f"⚠️ Telegram Bot failed: {e}", flush=True)


def channel_live_stream_worker(bot_instance, channel_id):
    if not channel_id:
        return
    try:
        sent = bot_instance.send_message(channel_id, "📡 استریم زنده kill_pv2 راه‌اندازی شد...")
        CHANNEL_STREAM_STATE["msg_id"] = sent.message_id
        try:
            bot_instance.pin_chat_message(channel_id, sent.message_id, disable_notification=True)
        except Exception:
            pass
    except Exception as e:
        print(f"⚠️ Channel stream init failed: {e}", flush=True)
        return

    last_rendered = []
    while True:
        time.sleep(8)
        try:
            current = list(CHANNEL_STREAM_STATE["events"])
            if current == last_rendered:
                continue
            cpu_v, ram_v = get_server_resources()
            online = sum(1 for k, v in db().items() if len(USER_LIVE_IPS.get(k, {})) > 0 and v.get("active", True))
            text = (
                f"📡 استریم زنده kill_pv2\n⏱️ {time.strftime('%H:%M:%S')}\n"
                f"👥 {online} آنلاین | {len(db())} کل\n🖥️ CPU {cpu_v}% | RAM {ram_v}%\n"
                f"🛡️ Xray: {'🟢' if is_xray_core_running() else '🔴'}\n\n" +
                "\n".join(current or ["رویدادی ثبت نشده"])
            )
            bot_instance.edit_message_text(text, channel_id, CHANNEL_STREAM_STATE["msg_id"])
            last_rendered = current
        except Exception:
            pass


# ─────────────────────────────────────────────
# راه‌اندازی نهایی سرویس
# ─────────────────────────────────────────────
if os.path.exists('active_edge_host.txt'):
    with open('active_edge_host.txt', 'r') as f:
        RUNTIME["tunnel_host"] = f.read().strip()

if os.path.exists('active_runner_host.txt'):
    with open('active_runner_host.txt', 'r') as f:
        RUNTIME["runner_host"] = f.read().strip()
else:
    RUNTIME["runner_host"] = RUNTIME["tunnel_host"]

print("\n==============================================================", flush=True)
print("🛡️ KILL_PV2 PANEL INITIALIZED ON PORT 8086", flush=True)
print(f"🔗 GATEWAY HOST: https://{RUNTIME['tunnel_host']}", flush=True)
print(f"🚀 RUNNER HOST:  https://{RUNTIME['runner_host']}", flush=True)
print("==============================================================\n", flush=True)

sync_xray_core(force=True)

# اول هاست‌های قدیمی تونل‌های خصوصی رو پاک می‌کنیم، بعد تونل جدید می‌سازیم
bootstrap_private_tunnels_on_startup()

# حالا با هاست‌های تازه، ساب‌ها رو پوش می‌کنیم
push_subs_to_github()
init_telegram_bot_service()

threading.Thread(
    target=lambda: HTTPServer(('127.0.0.1', 8086), SanaeiMobileXuiServer).serve_forever(),
    daemon=True
).start()
threading.Thread(target=xray_live_log_sniffer, daemon=True).start()
threading.Thread(target=speed_and_ip_cleaner, daemon=True).start()
threading.Thread(target=main_tunnel_watchdog, daemon=True).start()
threading.Thread(target=private_tunnels_watchdog, daemon=True).start()

push_channel_event("🚀 سرویس kill_pv2 بالا اومد")

# ─────────────────────────────────────────────
# حلقه‌ی اصلی برنامه — تا زمان مشخص زنده می‌مونه
# و هر دقیقه وضعیت رو چک/سیو/پوش می‌کنه
# ─────────────────────────────────────────────
TOTAL_DURATION = 19800  # حدود ۵.۵ ساعت، قبل از اتمام سقف مجاز ران‌رهای گیت‌هاب اکشن
elapsed = 0
last_github_update_time = time.time()

while elapsed < TOTAL_DURATION:
    time.sleep(5)
    elapsed += 5

    check_expiration_and_limits()

    if time.time() - last_github_update_time >= 60:
        save_state()
        push_subs_to_github()
        last_github_update_time = time.time()

print("⏳ زمان این اجرا تمام شد، سیو نهایی...", flush=True)
save_state("💾 Final sync before shutdown [skip ci]")
push_subs_to_github()
print("✅ خروج تمیز. ورک‌فلوی بعدی خودش دوباره اجرا میشه.", flush=True)
