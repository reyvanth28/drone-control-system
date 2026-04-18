import socket
import struct
import os
import threading
import subprocess
import time
import json
import logging
import csv
import queue
from datetime import datetime
from flask import Flask, jsonify, send_from_directory, request

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
DISCOVERY_PORT  = 5001
IMAGE_PORT      = 5000
TELEMETRY_PORT  = 6000
ESP_PORT        = 7000
WEB_PORT        = 8080

BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR       = os.path.join(BASE_DIR, 'received_images')

LOG_IMAGES      = 'log_images.csv'
LOG_TELEMETRY   = 'log_telemetry.csv'

os.makedirs(IMAGE_DIR, exist_ok=True)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ─────────────────────────────────────────────
# AI CONFIG  ← only thing you need to change
# ─────────────────────────────────────────────
from dotenv import load_dotenv
import os

# IMPORTANT: always load from project root
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "..", ".env"))

GROQ = os.getenv("GROQ")

if not GROQ:
    raise ValueError("GROQ not found in .env")

AI_MODEL = "openai/gpt-oss-120b"
AI_INTERVAL = 5
AI_REPORT_EVERY = 30




# ─────────────────────────────────────────────
# ASYNC LOGGING QUEUES
# ─────────────────────────────────────────────
telemetry_queue = queue.Queue()
image_queue     = queue.Queue()

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def kill_port(port):
    subprocess.run(f'fuser -k {port}/tcp', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(f'fuser -k {port}/udp', shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.3)

def cleanup():
    print(f'[{now()}] [~] Purging locked ports...')
    for p in [IMAGE_PORT, DISCOVERY_PORT, TELEMETRY_PORT, WEB_PORT, ESP_PORT]:
        kill_port(p)

# ─────────────────────────────────────────────
# CSV DATABASES
# ─────────────────────────────────────────────
def init_csv_logs():
    if not os.path.exists(LOG_IMAGES):
        with open(LOG_IMAGES, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow(['Filename', 'Captured At', 'Received At', 'Size (bytes)'])

    if not os.path.exists(LOG_TELEMETRY):
        with open(LOG_TELEMETRY, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'Timestamp', 'Roll', 'Pitch', 'Yaw',
                'Roll Rate', 'Pitch Rate', 'Yaw Rate',
                'GPS Fix', 'GPS Lat', 'GPS Lon', 'GPS Alt', 'GPS Sats', 'GPS HDOP', 'GPS Speed',
                'Global Lat', 'Global Lon', 'Global Alt', 'Relative Alt',
                'Vel N', 'Vel E', 'Vel D', 'Heading',
                'Airspeed', 'Groundspeed', 'Altitude', 'Climb', 'Throttle',
                'Voltage', 'Current', 'Battery %', 'CPU Load',
                'Armed', 'Mode',
                'EKF Vel', 'EKF Pos H', 'EKF Pos V', 'EKF Compass', 'EKF Flags',
                'Vib X', 'Vib Y', 'Vib Z',
                'Baro Press', 'Baro Temp',
                'Local X', 'Local Y', 'Local Z',
                'Accel X', 'Accel Y', 'Accel Z',
                'Gyro X', 'Gyro Y', 'Gyro Z',
                'Mag X', 'Mag Y', 'Mag Z',
                'Wind Dir', 'Wind Speed',
                'Charging Status'
            ])

init_csv_logs()

def log_worker():
    while True:
        if not telemetry_queue.empty():
            with open(LOG_TELEMETRY, 'a', newline='') as f:
                writer = csv.writer(f)
                while not telemetry_queue.empty():
                    d, charging = telemetry_queue.get()
                    writer.writerow([
                        d.get('timestamp',''),
                        d.get('roll',''), d.get('pitch',''), d.get('yaw',''),
                        d.get('roll_rate',''), d.get('pitch_rate',''), d.get('yaw_rate',''),
                        d.get('gps_fix',''), d.get('gps_lat',''), d.get('gps_lon',''),
                        d.get('gps_alt',''), d.get('gps_sats',''), d.get('gps_hdop',''), d.get('gps_speed',''),
                        d.get('global_lat',''), d.get('global_lon',''), d.get('global_alt',''), d.get('relative_alt',''),
                        d.get('vel_n',''), d.get('vel_e',''), d.get('vel_d',''), d.get('heading',''),
                        d.get('airspeed',''), d.get('groundspeed',''), d.get('altitude',''),
                        d.get('climb',''), d.get('throttle',''),
                        d.get('voltage',''), d.get('current',''), d.get('battery_pct',''), d.get('cpu_load',''),
                        str(d.get('armed','')), d.get('mode',''),
                        d.get('ekf_vel',''), d.get('ekf_pos_h',''), d.get('ekf_pos_v',''),
                        d.get('ekf_compass',''), d.get('ekf_flags',''),
                        d.get('vib_x',''), d.get('vib_y',''), d.get('vib_z',''),
                        d.get('baro_press',''), d.get('baro_temp',''),
                        d.get('local_x',''), d.get('local_y',''), d.get('local_z',''),
                        d.get('accel_x',''), d.get('accel_y',''), d.get('accel_z',''),
                        d.get('gyro_x',''), d.get('gyro_y',''), d.get('gyro_z',''),
                        d.get('mag_x',''), d.get('mag_y',''), d.get('mag_z',''),
                        d.get('wind_dir',''), d.get('wind_speed',''),
                        charging
                    ])
                    telemetry_queue.task_done()

        if not image_queue.empty():
            with open(LOG_IMAGES, 'a', newline='') as f:
                writer = csv.writer(f)
                while not image_queue.empty():
                    filename, received_at, size = image_queue.get()
                    try:
                        parts = filename.replace('.jpg', '').split('_')
                        captured = f"{parts[1]}_{parts[2]}"
                    except:
                        captured = 'unknown'
                    writer.writerow([filename, captured, received_at, size])
                    image_queue.task_done()

        time.sleep(0.5)

# ─────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────
latest_telemetry = {}
charging_status  = 'Unknown'
received_images  = []
hidden_before    = 0
state_lock       = threading.Lock()

# ─────────────────────────────────────────────
# AI STATE
# ─────────────────────────────────────────────
ai_state = {
    "status":      "STANDBY",        # OK / WARNING / CRITICAL / STANDBY
    "alerts":      [],               # list of alert strings
    "predictions": [],               # list of prediction strings
    "summary":     "Awaiting telemetry data...",
    "battery_health": "—",
    "vibration_health": "—",
    "stability_score": "—",
    "last_updated": "—",
    "flight_report": "",
    "report_updated": "—",
    "history": []                    # last 20 snapshots for mini-graph
}
ai_lock = threading.Lock()
flight_snapshots = []   # rolling buffer for report generation

# ─────────────────────────────────────────────
# AI ENGINE
# ─────────────────────────────────────────────
from groq import Groq
client = Groq(api_key=GROQ)
def call_ai(prompt):
    try:
        response = client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=600
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"AI_ERROR: {e}"
def parse_realtime_response(text):
    """Parse structured AI response into fields."""
    parsed = {
        "status": "OK",
        "alerts": [],
        "predictions": [],
        "summary": "",
        "battery_health": "—",
        "vibration_health": "—",
        "stability_score": "—"
    }
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("STATUS:"):
            val = line.replace("STATUS:", "").strip().upper()
            if "CRITICAL" in val:
                parsed["status"] = "CRITICAL"
            elif "WARNING" in val:
                parsed["status"] = "WARNING"
            else:
                parsed["status"] = "OK"
        elif line.startswith("ALERTS:"):
            raw = line.replace("ALERTS:", "").strip()
            if raw.lower() not in ["none", "—", "-", ""]:
                parsed["alerts"] = [a.strip() for a in raw.split(",") if a.strip()]
        elif line.startswith("PREDICTIONS:"):
            raw = line.replace("PREDICTIONS:", "").strip()
            if raw.lower() not in ["none", "—", "-", ""]:
                parsed["predictions"] = [p.strip() for p in raw.split(",") if p.strip()]
        elif line.startswith("BATTERY:"):
            parsed["battery_health"] = line.replace("BATTERY:", "").strip()
        elif line.startswith("VIBRATION:"):
            parsed["vibration_health"] = line.replace("VIBRATION:", "").strip()
        elif line.startswith("STABILITY:"):
            parsed["stability_score"] = line.replace("STABILITY:", "").strip()
        elif line.startswith("SUMMARY:"):
            parsed["summary"] = line.replace("SUMMARY:", "").strip()
    if not parsed["summary"]:
        parsed["summary"] = text[:120]
    return parsed


def ai_realtime_loop():
    """Runs every AI_INTERVAL seconds — reads telemetry, calls AI, updates ai_state."""
    global flight_snapshots
    last_report_time = time.time()

    while True:
        time.sleep(AI_INTERVAL)
        try:
            with state_lock:
                telem = dict(latest_telemetry)
                cs = charging_status

            if not telem or not telem.get("timestamp"):
                continue

            # Snapshot for report buffer
            telem["charging"] = cs
            flight_snapshots.append(telem)
            if len(flight_snapshots) > 300:
                flight_snapshots = flight_snapshots[-300:]

            # ── REALTIME ANALYSIS ──────────────────────
            prompt = f"""You are an expert drone flight safety AI for Team Planetoops.
Analyze this MAVLink telemetry snapshot and respond EXACTLY in this format (one value per line, no extra text):

STATUS: <OK|WARNING|CRITICAL>
ALERTS: <comma-separated issues, or None>
PREDICTIONS: <comma-separated upcoming risks, or None>
BATTERY: <health description in ≤8 words>
VIBRATION: <health description in ≤8 words>
STABILITY: <score 0-100 with one word label>
SUMMARY: <one sentence overall status>

Telemetry data:
- Armed: {telem.get('armed', '?')}
- Mode: {telem.get('mode', '?')}
- Altitude: {telem.get('altitude', '?')} m
- Roll: {telem.get('roll', '?')}°  Pitch: {telem.get('pitch', '?')}°  Yaw: {telem.get('yaw', '?')}°
- Airspeed: {telem.get('airspeed', '?')} m/s  Groundspeed: {telem.get('groundspeed', '?')} m/s  Climb: {telem.get('climb', '?')} m/s
- Voltage: {telem.get('voltage', '?')} V  Current: {telem.get('current', '?')} A  Battery: {telem.get('battery_pct', '?')}%
- Vib X/Y/Z: {telem.get('vib_x','?')} / {telem.get('vib_y','?')} / {telem.get('vib_z','?')}
- Baro Pressure: {telem.get('baro_press', '?')} hPa  Temp: {telem.get('baro_temp', '?')}°C
- GPS Lat/Lon: {telem.get('gps_lat','?')} / {telem.get('gps_lon','?')}
- Throttle: {telem.get('throttle','?')}%  Heading: {telem.get('heading','?')}°
- Charging/Docking: {cs}
"""
            response_text = call_ai(prompt)
            parsed = parse_realtime_response(response_text)

            ts = datetime.now().strftime("%H:%M:%S")
            with ai_lock:
                ai_state["status"]           = parsed["status"]
                ai_state["alerts"]           = parsed["alerts"]
                ai_state["predictions"]      = parsed["predictions"]
                ai_state["summary"]          = parsed["summary"]
                ai_state["battery_health"]   = parsed["battery_health"]
                ai_state["vibration_health"] = parsed["vibration_health"]
                ai_state["stability_score"]  = parsed["stability_score"]
                ai_state["last_updated"]     = ts
                # Keep last 20 status points for history sparkline
                ai_state["history"].append({
                    "t": ts,
                    "status": parsed["status"],
                    "batt": telem.get("battery_pct", 0),
                    "vib": telem.get("vib_z", 0),
                    "alt": telem.get("altitude", 0)
                })
                if len(ai_state["history"]) > 20:
                    ai_state["history"] = ai_state["history"][-20:]

            print(f'[{now()}] [AI] {parsed["status"]} — {parsed["summary"]}')

            # ── PERIODIC FULL REPORT ───────────────────
            if time.time() - last_report_time >= AI_REPORT_EVERY and len(flight_snapshots) >= 3:
                last_report_time = time.time()
                sample = flight_snapshots[-min(15, len(flight_snapshots)):]
                report_prompt = f"""You are a drone flight analyst for Team Planetoops.
Generate a concise flight report based on the last {len(sample)} telemetry snapshots.

Use this exact structure:
OVERVIEW: <2 sentences about flight status>
BATTERY: <battery trend and estimated remaining flight time>
MOTOR_HEALTH: <vibration analysis and motor assessment>
ANOMALIES: <any detected issues, or None>
SAFETY_SCORE: <0-100>/100
RECOMMENDATIONS: <2-3 actionable bullet points>

Telemetry snapshots (most recent last):
{json.dumps(sample, indent=2)}
"""
                report_text = call_ai(report_prompt)
                with ai_lock:
                    ai_state["flight_report"]  = report_text
                    ai_state["report_updated"] = datetime.now().strftime("%H:%M:%S")
                print(f'[{now()}] [AI] Flight report updated.')

        except Exception as e:
            print(f'[{now()}] [AI] Error: {e}')


# ─────────────────────────────────────────────
# RECVALL
# ─────────────────────────────────────────────
def recvall(conn, n):
    data = bytearray()
    while len(data) < n:
        packet = conn.recv(n - len(data))
        if not packet:
            return None
        data.extend(packet)
    return bytes(data)

# ─────────────────────────────────────────────
# DISCOVERY
# ─────────────────────────────────────────────
def handle_discovery():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', DISCOVERY_PORT))
    print(f'[{now()}] [*] Discovery service active on UDP:{DISCOVERY_PORT}')
    while True:
        try:
            msg, addr = s.recvfrom(1024)
            if msg in [b'PI_IMAGE_SENDER', b'TELEMETRY_CLIENT']:
                s.sendto(b'BASE_SERVER_HERE', addr)
        except Exception:
            pass

# ─────────────────────────────────────────────
# IMAGE RECEIVER
# ─────────────────────────────────────────────
def image_session(conn, addr):
    conn.settimeout(15.0)
    with conn:
        while True:
            try:
                raw_fn_len = recvall(conn, 4)
                if not raw_fn_len: break
                fn_len = struct.unpack('>I', raw_fn_len)[0]

                filename = recvall(conn, fn_len)
                if not filename: break
                filename = filename.decode()

                raw_size = recvall(conn, 4)
                if not raw_size: break
                size = struct.unpack('>I', raw_size)[0]

                data = recvall(conn, size)
                if not data: break

                filepath = os.path.join(IMAGE_DIR, filename)
                received_at = now()

                with open(filepath, 'wb') as f:
                    f.write(data)

                print(f'[{received_at}] [+] Image payload received: {filename} ({size} bytes)')
                conn.sendall(b'ACK')

                image_queue.put((filename, received_at, size))

                with state_lock:
                    received_images.append({
                        'filename': filename,
                        'received_at': received_at,
                        'size': size
                    })
            except Exception:
                break

def handle_images():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', IMAGE_PORT))
    s.listen(5)
    print(f'[{now()}] [*] Image server ready on TCP:{IMAGE_PORT}')
    while True:
        conn, addr = s.accept()
        threading.Thread(target=image_session, args=(conn, addr), daemon=True).start()

# ─────────────────────────────────────────────
# TELEMETRY RECEIVER
# ─────────────────────────────────────────────
def telemetry_session(conn, addr):
    global latest_telemetry, charging_status
    conn.settimeout(5.0)
    with conn:
        while True:
            try:
                raw_len = recvall(conn, 4)
                if not raw_len: break
                length = struct.unpack('>I', raw_len)[0]

                if length > 1_000_000: break

                payload = recvall(conn, length)
                if not payload: break

                d = json.loads(payload.decode())

                with state_lock:
                    latest_telemetry = d
                    cs = charging_status

                telemetry_queue.put((d, cs))

            except (json.JSONDecodeError, socket.timeout):
                break
            except Exception:
                break

def handle_telemetry():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(('0.0.0.0', TELEMETRY_PORT))
    s.listen(5)
    print(f'[{now()}] [*] Telemetry server ready on TCP:{TELEMETRY_PORT}')
    while True:
        conn, addr = s.accept()
        threading.Thread(target=telemetry_session, args=(conn, addr), daemon=True).start()

# ─────────────────────────────────────────────
# ESP32 CHARGING STATUS
# ─────────────────────────────────────────────
esp_app = Flask('esp_receiver')

@esp_app.route('/charging', methods=['POST'])
def receive_charging():
    global charging_status
    data = request.get_json(force=True, silent=True) or {}
    status = data.get('status', 'Unknown')
    with state_lock:
        charging_status = status
    return jsonify({'ok': True}), 200

def run_esp_server():
    print(f'[{now()}] [*] ESP receiver running on 0.0.0.0:{ESP_PORT}')
    esp_app.run(host='0.0.0.0', port=ESP_PORT, threaded=True, use_reloader=False)

# ─────────────────────────────────────────────
# WEBSITE — TEAM PLANETOOPS + AI INTEL TAB
# ─────────────────────────────────────────────
web_app = Flask('dashboard', static_folder=IMAGE_DIR)

@web_app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Planetoops Ground Control</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700;900&family=Rajdhani:wght@500;700&display=swap');

  :root {
    --bg-main: #020204;
    --bg-card: rgba(10, 12, 18, 0.85);
    --border: #1f2233;
    --accent: #00e5ff;
    --accent-glow: rgba(0, 229, 255, 0.5);
    --text-main: #e2e8f0;
    --text-muted: #4a5568;
    --danger: #ff003c;
    --success: #00ff88;
    --warning: #ffaa00;
    --ai-color: #bf00ff;
    --ai-glow: rgba(191, 0, 255, 0.5);
  }

  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family: 'Rajdhani', sans-serif; background: var(--bg-main); color: var(--text-main); overflow-x: hidden; }

  /* ── BOOTLOADER ── */
  #loader { position: fixed; inset: 0; background: #000; z-index: 9999; display: flex; flex-direction: column; justify-content: center; align-items: center; transition: opacity 0.4s ease-out; overflow: hidden;}
  .radar-ring { width: 300px; height: 300px; border-radius: 50%; border: 1px solid rgba(0,229,255,0.2); position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); box-shadow: 0 0 40px rgba(0,229,255,0.1); }
  .radar-ring::before { content: ''; position: absolute; top: 0; left: 0; right: 0; bottom: 0; border-radius: 50%; background: conic-gradient(from 0deg, transparent 70%, rgba(0,229,255,0.8) 100%); animation: sweep 2s linear infinite; }
  .radar-ring::after { content: ''; position: absolute; top: 50%; left: 50%; width: 2px; height: 100%; background: rgba(0,229,255,0.3); transform: translate(-50%, -50%); }
  .radar-ring-inner { width: 150px; height: 150px; border-radius: 50%; border: 1px dashed rgba(0,229,255,0.5); position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); animation: spin-reverse 4s linear infinite;}
  .glitch-wrapper { position: relative; margin-bottom: 20px; z-index: 10; }
  .glitch { font-family: 'Orbitron', sans-serif; font-size: 5rem; font-weight: 900; color: #fff; text-shadow: 0 0 10px var(--accent-glow); position: relative; letter-spacing: 8px;}
  .glitch::before, .glitch::after { content: "PLANETOOPS"; position: absolute; top: 0; left: 0; width: 100%; height: 100%; opacity: 0.8; }
  .glitch::before { color: var(--accent); z-index: -1; animation: glitch-anim 0.3s cubic-bezier(.25, .46, .45, .94) both infinite; }
  .glitch::after { color: var(--danger); z-index: -2; animation: glitch-anim 0.3s cubic-bezier(.25, .46, .45, .94) reverse both infinite; }
  .terminal-box { width: 600px; height: 150px; border: 1px solid var(--border); background: rgba(0,0,0,0.8); z-index: 10; font-family: 'Consolas', monospace; font-size: 14px; color: var(--success); padding: 15px; overflow: hidden; position: relative; margin-top: 20px;}
  .terminal-box::after { content: ''; position: absolute; top: 0; left: 0; width: 100%; height: 100%; background: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(0,255,136,0.05) 2px, rgba(0,255,136,0.05) 4px); pointer-events: none;}
  .term-line { margin-bottom: 4px; text-shadow: 0 0 5px rgba(0,255,136,0.5); }
  .load-bar-container { width: 600px; height: 6px; background: #111; margin-top: 20px; position: relative; overflow: hidden; z-index: 10; border: 1px solid #333;}
  .load-bar-fill { height: 100%; width: 0%; background: var(--accent); box-shadow: 0 0 15px var(--accent-glow); transition: width 0.1s; }
  @keyframes sweep { 100% { transform: rotate(360deg); } }
  @keyframes spin-reverse { 100% { transform: translate(-50%, -50%) rotate(-360deg); } }
  @keyframes glitch-anim {
    0% { transform: translate(0) } 20% { transform: translate(-3px, 3px) }
    40% { transform: translate(-3px, -3px) } 60% { transform: translate(3px, 3px) }
    80% { transform: translate(3px, -3px) } 100% { transform: translate(0) }
  }

  /* ── NAVBAR ── */
  .navbar { background: rgba(5, 5, 8, 0.95); padding: 15px 30px; display:flex; justify-content: space-between; align-items:center; border-bottom: 1px solid rgba(0,229,255,0.3); backdrop-filter: blur(10px); position: sticky; top: 0; z-index: 100; box-shadow: 0 4px 30px rgba(0,229,255,0.1);}
  .navbar h1 { font-family: 'Orbitron', sans-serif; font-size:24px; font-weight: 900; color: #fff; letter-spacing: 2px;}
  .navbar h1 span { color: var(--accent); text-shadow: 0 0 15px var(--accent-glow); }
  .tabs { display: flex; gap: 15px; }
  .tab { padding: 8px 20px; background: transparent; border-radius: 2px; cursor:pointer; font-size:16px; font-weight: 700; color: var(--text-muted); border: 1px solid var(--border); transition: 0.3s; text-transform: uppercase; letter-spacing: 1px; position: relative; overflow: hidden;}
  .tab.active { background: rgba(0, 229, 255, 0.1); color: var(--accent); border-color: var(--accent); box-shadow: inset 0 0 10px var(--accent-glow); }
  .tab.ai-tab.active { background: rgba(191,0,255,0.1); color: var(--ai-color); border-color: var(--ai-color); box-shadow: inset 0 0 10px var(--ai-glow); }
  .tab:hover:not(.active) { background: #1a1d2d; color: #fff; }

  /* ── PAGES ── */
  .page { display:none; padding: 30px; max-width: 1600px; margin: 0 auto; }
  .page.active { display:block; animation: fade-in 0.5s ease-out; }
  @keyframes fade-in { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

  /* ── CARDS ── */
  .dashboard-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 25px; }
  .card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 4px; padding: 25px; box-shadow: 0 10px 30px rgba(0,0,0,0.8); position: relative; overflow: hidden; }
  .card::before { content: ""; position: absolute; top: 0; left: 0; width: 3px; height: 100%; background: var(--accent); opacity: 0.8; box-shadow: 0 0 10px var(--accent); }
  .card.ai-card::before { background: var(--ai-color); box-shadow: 0 0 10px var(--ai-color); }
  .card h3 { font-family: 'Orbitron', sans-serif; font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 2px; border-bottom: 1px dashed var(--border); padding-bottom: 12px; margin-bottom: 18px; display: flex; align-items: center; gap: 10px;}
  .card h3::before { content: "■"; color: var(--accent); font-size: 10px; }
  .card.ai-card h3::before { color: var(--ai-color); }
  .data-row { display: flex; justify-content: space-between; margin-bottom: 14px; font-size: 16px; align-items: center;}
  .data-label { color: #6e7681; font-weight: 500; }
  .data-val { color: #fff; font-weight: 700; font-family: 'Consolas', monospace; font-size: 18px; text-shadow: 0 0 5px rgba(255,255,255,0.3);}
  .status-armed { color: var(--danger); text-shadow: 0 0 10px rgba(255,0,60,0.8); }
  .status-safe  { color: var(--success); text-shadow: 0 0 10px rgba(0,255,136,0.8); }

  /* ── ARTIFICIAL HORIZON ── */
  .attitude-container { display: flex; align-items: center; justify-content: center; margin: 30px 0; }
  .ah-bezel { width: 180px; height: 180px; border-radius: 50%; border: 6px solid #1f2233; position: relative; overflow: hidden; background: #222; box-shadow: inset 0 0 30px rgba(0,0,0,0.9), 0 0 25px rgba(0,229,255,0.15); }
  .ah-ball { position: absolute; top: -50%; left: -50%; width: 200%; height: 200%; transition: transform 0.1s linear; }
  .ah-sky { width: 100%; height: 50%; background: linear-gradient(to bottom, #005c97, #363795); }
  .ah-ground { width: 100%; height: 50%; background: linear-gradient(to bottom, #42275a, #734b6d); border-top: 2px solid #00e5ff; box-shadow: inset 0 2px 10px rgba(0,229,255,0.5);}
  .ah-crosshair { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); width: 90px; height: 2px; background: #ff003c; z-index: 10; box-shadow: 0 0 8px #ff003c; }
  .ah-crosshair::before { content: ""; position: absolute; top: -12px; left: 50%; transform: translateX(-50%); width: 2px; height: 12px; background: #ff003c; }

  /* ── IMAGES PAGE ── */
  .img-toolbar { display:flex; gap:15px; margin-bottom:30px; align-items: center; background: rgba(10, 12, 18, 0.5); padding: 15px; border-radius: 4px; border: 1px solid var(--border);}
  .btn { padding:10px 24px; border-radius:2px; border:none; cursor:pointer; font-size:14px; font-weight:700; font-family: 'Orbitron', sans-serif; text-transform: uppercase; transition: 0.2s; letter-spacing: 1px;}
  .btn-danger  { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
  .btn-danger:hover { background: var(--danger); color: #fff; box-shadow: 0 0 20px rgba(255,0,60,0.6); }
  .btn-primary { background: transparent; color: var(--accent); border: 1px solid var(--accent); }
  .btn-primary:hover { background: var(--accent); color: #000; box-shadow: 0 0 20px var(--accent-glow); }
  .img-count { font-size: 16px; color: var(--accent); font-weight: bold; margin-left: auto; font-family: 'Orbitron', sans-serif;}
  .img-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(320px,1fr)); gap:30px; }
  .img-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 4px; overflow:hidden; transition: 0.3s; position: relative;}
  .img-card::after { content: ''; position: absolute; inset: 0; border: 1px solid transparent; transition: 0.3s; pointer-events: none;}
  .img-card:hover { transform: translateY(-5px); box-shadow: 0 10px 30px rgba(0,0,0,0.8); }
  .img-card:hover::after { border-color: var(--accent); box-shadow: inset 0 0 15px rgba(0,229,255,0.2);}
  .img-card img { width:100%; height:240px; object-fit:cover; display:block; border-bottom: 1px solid var(--border); filter: contrast(1.1) saturate(1.1);}
  .img-info { padding: 18px; background: linear-gradient(to top, rgba(0,0,0,0.8), transparent);}
  .img-name { font-weight:700; color: #fff; margin-bottom: 8px; font-size: 14px; word-break: break-all; font-family: 'Consolas', monospace; color: var(--accent);}
  .img-time, .img-size { color: #8b949e; font-size: 12px; font-family: 'Consolas', monospace; margin-bottom: 4px;}

  /* ── AI INTEL PAGE ── */
  .ai-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(340px, 1fr)); gap: 25px; }

  /* Big status banner */
  .ai-status-banner {
    grid-column: 1 / -1;
    padding: 20px 30px;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: var(--bg-card);
    display: flex;
    align-items: center;
    gap: 24px;
    position: relative;
    overflow: hidden;
  }
  .ai-status-banner::before {
    content: "";
    position: absolute; top: 0; left: 0;
    width: 4px; height: 100%;
    background: var(--ai-color);
    box-shadow: 0 0 12px var(--ai-color);
  }
  .ai-status-dot {
    width: 18px; height: 18px; border-radius: 50%;
    background: var(--success);
    box-shadow: 0 0 12px var(--success);
    flex-shrink: 0;
    animation: pulse-dot 1.5s ease-in-out infinite;
  }
  @keyframes pulse-dot { 0%,100% { opacity:1; } 50% { opacity:0.3; } }
  .ai-status-dot.warning { background: var(--warning); box-shadow: 0 0 12px var(--warning); }
  .ai-status-dot.critical { background: var(--danger); box-shadow: 0 0 12px var(--danger); animation: pulse-dot 0.5s ease-in-out infinite; }
  .ai-status-dot.standby { background: #555; box-shadow: none; animation: none; }
  .ai-status-label {
    font-family: 'Orbitron', sans-serif;
    font-size: 22px;
    font-weight: 900;
    letter-spacing: 3px;
  }
  .ai-status-label.ok       { color: var(--success); text-shadow: 0 0 15px var(--success); }
  .ai-status-label.warning  { color: var(--warning); text-shadow: 0 0 15px var(--warning); }
  .ai-status-label.critical { color: var(--danger);  text-shadow: 0 0 15px var(--danger); }
  .ai-status-label.standby  { color: #555; }
  .ai-summary-text {
    font-size: 17px;
    color: var(--text-main);
    flex: 1;
    font-style: italic;
  }
  .ai-updated {
    font-family: 'Consolas', monospace;
    font-size: 12px;
    color: var(--text-muted);
    margin-left: auto;
    white-space: nowrap;
  }

  /* Alert / Prediction lists */
  .alert-list { list-style: none; }
  .alert-list li {
    padding: 10px 14px;
    margin-bottom: 8px;
    border-radius: 3px;
    font-family: 'Consolas', monospace;
    font-size: 14px;
    border-left: 3px solid var(--danger);
    background: rgba(255,0,60,0.08);
    color: #ffb3c1;
  }
  .alert-list li.pred {
    border-left-color: var(--warning);
    background: rgba(255,170,0,0.08);
    color: #ffe0a0;
  }
  .alert-list li.none {
    border-left-color: var(--success);
    background: rgba(0,255,136,0.05);
    color: var(--success);
  }

  /* Health meters */
  .health-meter { margin-bottom: 18px; }
  .health-label { font-size: 13px; color: #6e7681; margin-bottom: 6px; text-transform: uppercase; letter-spacing: 1px; }
  .health-bar-bg { height: 8px; background: #111; border-radius: 4px; overflow: hidden; border: 1px solid #222; }
  .health-bar-fill { height: 100%; border-radius: 4px; transition: width 1s ease, background 0.5s; }

  /* Sparkline canvas */
  .sparkline-wrap { margin-top: 10px; }
  .sparkline-wrap canvas { width: 100%; height: 60px; display: block; }

  /* Flight report box */
  .report-box {
    font-family: 'Consolas', monospace;
    font-size: 13px;
    color: #c9d1d9;
    background: rgba(0,0,0,0.5);
    border: 1px solid #1f2233;
    border-radius: 3px;
    padding: 16px;
    white-space: pre-wrap;
    line-height: 1.7;
    max-height: 340px;
    overflow-y: auto;
    margin-top: 8px;
  }
  .report-box::-webkit-scrollbar { width: 6px; }
  .report-box::-webkit-scrollbar-track { background: #0a0c12; }
  .report-box::-webkit-scrollbar-thumb { background: var(--ai-color); border-radius: 3px; }

  /* History timeline */
  .history-table { width: 100%; border-collapse: collapse; font-family: 'Consolas', monospace; font-size: 12px; }
  .history-table th { color: #4a5568; font-weight: 400; text-align: left; padding: 4px 8px; border-bottom: 1px solid #1f2233; }
  .history-table td { padding: 5px 8px; border-bottom: 1px solid rgba(31,34,51,0.5); }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 2px; font-size: 11px; font-weight: 700; }
  .badge.ok       { background: rgba(0,255,136,0.15); color: var(--success); }
  .badge.warning  { background: rgba(255,170,0,0.15); color: var(--warning); }
  .badge.critical { background: rgba(255,0,60,0.15);  color: var(--danger); }
  .badge.standby  { background: rgba(100,100,100,0.15); color: #555; }
</style>
</head>
<body>

<div id="loader">
  <div class="radar-ring"></div>
  <div class="radar-ring-inner"></div>
  <div class="glitch-wrapper"><div class="glitch">PLANETOOPS</div></div>
  <div class="terminal-box" id="term-box"></div>
  <div class="load-bar-container"><div class="load-bar-fill" id="load-bar"></div></div>
</div>

<div class="navbar">
  <h1>PLANETOOPS <span>// GCS_X1</span></h1>
  <div class="tabs">
    <div class="tab active"        onclick="switchTab('dashboard',this)">Telemetry Link</div>
    <div class="tab"               onclick="switchTab('images',this)">Payload Optics</div>
    <div class="tab ai-tab"        onclick="switchTab('ai',this)">⚡ AI Intel</div>
  </div>
</div>

<!-- ══════════════════════════════════════════
     TELEMETRY PAGE  (untouched from original)
     ══════════════════════════════════════════ -->
<div id="dashboard" class="page active">
  <div class="dashboard-grid">

    <div class="card">
      <h3>SYS.STATUS</h3>
      <div class="data-row"><span class="data-label">Arming State</span><span id="armed" class="data-val status-safe">DISARMED</span></div>
      <div class="data-row"><span class="data-label">Flight Mode</span><span id="mode" class="data-val">0</span></div>
      <div class="data-row"><span class="data-label">Throttle Output</span><span id="throttle" class="data-val" style="color: var(--accent)">0%</span></div>
    </div>

    <div class="card" style="grid-row: span 2; display: flex; flex-direction: column;">
      <h3>SPATIAL ORIENTATION</h3>
      <div class="attitude-container">
        <div class="ah-bezel">
          <div class="ah-ball" id="ah-ball">
            <div class="ah-sky"></div>
            <div class="ah-ground"></div>
          </div>
          <div class="ah-crosshair"></div>
        </div>
      </div>
      <div class="data-row"><span class="data-label">Roll</span><span id="roll" class="data-val">0.00°</span></div>
      <div class="data-row"><span class="data-label">Pitch</span><span id="pitch" class="data-val">0.00°</span></div>
      <div class="data-row"><span class="data-label">Yaw</span><span id="yaw" class="data-val">0.00°</span></div>
    </div>

    <div class="card">
      <h3>NAV.DATA</h3>
      <div class="data-row"><span class="data-label">Latitude</span><span id="lat" class="data-val">0</span></div>
      <div class="data-row"><span class="data-label">Longitude</span><span id="lon" class="data-val">0</span></div>
      <div class="data-row"><span class="data-label">Alt (MSL)</span><span id="alt" class="data-val" style="color: var(--success)">0.00 m</span></div>
      <div class="data-row"><span class="data-label">Heading</span><span id="heading" class="data-val">0°</span></div>
    </div>

    <div class="card">
      <h3>KINEMATICS</h3>
      <div class="data-row"><span class="data-label">Airspeed</span><span id="airspeed" class="data-val">0 m/s</span></div>
      <div class="data-row"><span class="data-label">Groundspeed</span><span id="groundspeed" class="data-val">0 m/s</span></div>
      <div class="data-row"><span class="data-label">Climb Rate (Z)</span><span id="climb" class="data-val">0 m/s</span></div>
    </div>

    <div class="card">
      <h3>POWER.SYS</h3>
      <div class="data-row"><span class="data-label">V_BATT</span><span id="voltage" class="data-val">0.00 V</span></div>
      <div class="data-row"><span class="data-label">I_DRAW</span><span id="current" class="data-val">0.00 A</span></div>
      <div class="data-row"><span class="data-label">Capacity</span><span id="batt_pct" class="data-val" style="color: var(--danger)">0%</span></div>
      <div class="data-row"><span class="data-label">Docking</span><span id="charging" class="data-val">Unknown</span></div>
    </div>

    <div class="card">
      <h3>SENSORS.RAW</h3>
      <div class="data-row"><span class="data-label">Vibration X</span><span id="vib_x" class="data-val">0.000</span></div>
      <div class="data-row"><span class="data-label">Vibration Y</span><span id="vib_y" class="data-val">0.000</span></div>
      <div class="data-row"><span class="data-label">Vibration Z</span><span id="vib_z" class="data-val">0.000</span></div>
      <div class="data-row"><span class="data-label">P_Static</span><span id="baro_press" class="data-val">0.00 hPa</span></div>
      <div class="data-row"><span class="data-label">Core Temp</span><span id="baro_temp" class="data-val">0.00 °C</span></div>
    </div>

  </div>
</div>

<!-- ══════════════════════════════════════════
     IMAGES PAGE  (untouched from original)
     ══════════════════════════════════════════ -->
<div id="images" class="page">
  <div class="img-toolbar">
    <button class="btn btn-danger"  onclick="clearView()">Purge Cache</button>
    <button class="btn btn-primary" onclick="loadImages()">Sync Optics</button>
    <span class="img-count" id="img_count">0 DATA FRAMES</span>
  </div>
  <div class="img-grid" id="img_grid">
    <div style="color:var(--text-muted); grid-column: 1/-1; font-family: 'Consolas';">> AWAITING DOWNLINK PACKETS...</div>
  </div>
</div>

<!-- ══════════════════════════════════════════
     AI INTEL PAGE  ← NEW
     ══════════════════════════════════════════ -->
<div id="ai" class="page">
  <div class="ai-grid">

    <!-- Status Banner -->
    <div class="ai-status-banner">
      <div class="ai-status-dot standby" id="ai-dot"></div>
      <div class="ai-status-label standby" id="ai-status-label">STANDBY</div>
      <div class="ai-summary-text" id="ai-summary">Awaiting telemetry data...</div>
      <div class="ai-updated" id="ai-updated">Last: —</div>
    </div>

    <!-- Alerts -->
    <div class="card ai-card">
      <h3>LIVE ALERTS</h3>
      <ul class="alert-list" id="ai-alerts">
        <li class="none">No alerts — system nominal</li>
      </ul>
    </div>

    <!-- Predictions -->
    <div class="card ai-card">
      <h3>PREDICTIVE WARNINGS</h3>
      <ul class="alert-list" id="ai-predictions">
        <li class="none">No predictions yet</li>
      </ul>
    </div>

    <!-- Health Scores -->
    <div class="card ai-card">
      <h3>SUBSYSTEM HEALTH</h3>
      <div class="health-meter">
        <div class="health-label">Battery</div>
        <div style="display:flex;align-items:center;gap:10px;">
          <div class="health-bar-bg" style="flex:1"><div class="health-bar-fill" id="bar-battery" style="width:0%;background:var(--success)"></div></div>
          <span id="txt-battery" style="font-family:Consolas;font-size:13px;color:#aaa;min-width:60px">—</span>
        </div>
      </div>
      <div class="health-meter">
        <div class="health-label">Vibration / Motors</div>
        <div style="display:flex;align-items:center;gap:10px;">
          <div class="health-bar-bg" style="flex:1"><div class="health-bar-fill" id="bar-vib" style="width:0%;background:var(--success)"></div></div>
          <span id="txt-vib" style="font-family:Consolas;font-size:13px;color:#aaa;min-width:60px">—</span>
        </div>
      </div>
      <div class="health-meter">
        <div class="health-label">Stability Score</div>
        <div style="display:flex;align-items:center;gap:10px;">
          <div class="health-bar-bg" style="flex:1"><div class="health-bar-fill" id="bar-stab" style="width:0%;background:var(--ai-color)"></div></div>
          <span id="txt-stab" style="font-family:Consolas;font-size:13px;color:#aaa;min-width:60px">—</span>
        </div>
      </div>
    </div>

    <!-- Sparkline chart -->
    <div class="card ai-card">
      <h3>TREND — BATTERY % (last 20 readings)</h3>
      <div class="sparkline-wrap">
        <canvas id="spark-canvas" height="60"></canvas>
      </div>
    </div>

    <!-- Live Params -->
    <div class="card ai-card">
      <h3>AI-DERIVED PARAMS</h3>
      <div class="data-row"><span class="data-label">Battery Health</span><span id="ai-bat-health" class="data-val" style="font-size:14px;color:var(--success)">—</span></div>
      <div class="data-row"><span class="data-label">Vibration Health</span><span id="ai-vib-health" class="data-val" style="font-size:14px;color:var(--success)">—</span></div>
      <div class="data-row"><span class="data-label">Stability</span><span id="ai-stab" class="data-val" style="font-size:14px;color:var(--ai-color)">—</span></div>
      <div class="data-row"><span class="data-label">Analysis Interval</span><span class="data-val" style="font-size:14px;color:#555">8 sec</span></div>
    </div>

    <!-- History table -->
    <div class="card ai-card">
      <h3>STATUS HISTORY (last 20)</h3>
      <table class="history-table">
        <thead><tr><th>TIME</th><th>STATUS</th><th>BATT%</th><th>VIB_Z</th><th>ALT</th></tr></thead>
        <tbody id="ai-history"></tbody>
      </table>
    </div>

    <!-- Flight Report — full width -->
    <div class="card ai-card" style="grid-column: 1 / -1;">
      <h3>FLIGHT REPORT <span style="color:var(--text-muted);font-size:11px;margin-left:10px;font-family:Consolas;" id="report-ts">updated: —</span></h3>
      <div class="report-box" id="ai-report">> Generating report after first telemetry batch is collected...
> Reports refresh every 60 seconds automatically.</div>
    </div>

  </div>
</div>

<script>
// ── BOOT SEQUENCE ─────────────────────────────
const bootLog = [
  "INIT KERNEL... OK",
  "MOUNTING VFS... OK",
  "LOADING PLANETOOPS GCS MODULES...",
  "ESTABLISHING SOCKET BINDINGS [PORT: 5000, 6000, 7000]...",
  "CHECKING NATIVE CSV DATABASES... OK",
  "PINGING DRONE_X1 UPLINK... STANDBY",
  "BYPASSING MAINFRAME ENCRYPTION... SUCCESS",
  "CALIBRATING ARTIFICIAL HORIZON... DONE",
  "LOADING AI INTEL MODULE... OK",
  "BOOT SEQUENCE COMPLETE. HANDING CONTROL TO UI."
];
window.addEventListener('load', () => {
  const termBox = document.getElementById('term-box');
  const loadBar = document.getElementById('load-bar');
  let step = 0;
  const bootInterval = setInterval(() => {
    if (step < bootLog.length) {
      const line = document.createElement('div');
      line.className = 'term-line';
      line.innerText = '> ' + bootLog[step];
      termBox.appendChild(line);
      termBox.scrollTop = termBox.scrollHeight;
      loadBar.style.width = ((step + 1) / bootLog.length * 100) + '%';
      step++;
    } else {
      clearInterval(bootInterval);
      setTimeout(() => {
        const loader = document.getElementById('loader');
        loader.style.opacity = '0';
        setTimeout(() => loader.remove(), 400);
      }, 500);
    }
  }, 350);
});

// ── TAB SWITCHING ─────────────────────────────
function switchTab(id, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  el.classList.add('active');
  if (id === 'images') loadImages();
}

// ── TELEMETRY HELPERS ─────────────────────────
function safeVal(val, fallback="0") { return val !== undefined && val !== null && val !== "" ? val : fallback; }
function toFixedSafe(val, decimals) {
  let num = parseFloat(val);
  return isNaN(num) ? "0" : num.toFixed(decimals);
}

// ── TELEMETRY POLLING ─────────────────────────
async function fetchTelemetry() {
  try {
    const res = await fetch('/api/telemetry', { cache: 'no-store' });
    if (!res.ok) return;
    const d = await res.json();
    if(!d || !d.timestamp) return;

    const armedEl = document.getElementById('armed');
    if(d.armed) {
      armedEl.innerText = "ARMED";
      armedEl.className = "data-val status-armed";
    } else {
      armedEl.innerText = "DISARMED";
      armedEl.className = "data-val status-safe";
    }
    document.getElementById('mode').innerText = safeVal(d.mode);
    document.getElementById('throttle').innerText = Math.round(safeVal(d.throttle, 0)) + "%";

    let r = parseFloat(safeVal(d.roll, 0));
    let p = parseFloat(safeVal(d.pitch, 0));
    let y = parseFloat(safeVal(d.yaw, 0));

    document.getElementById('roll').innerText  = r.toFixed(2) + "°";
    document.getElementById('pitch').innerText = p.toFixed(2) + "°";
    document.getElementById('yaw').innerText   = y.toFixed(2) + "°";

    let pitchOffset = p * 2.5;
    document.getElementById('ah-ball').style.transform = `translateY(${pitchOffset}px) rotate(${r}deg)`;

    document.getElementById('lat').innerText     = safeVal(d.gps_lat);
    document.getElementById('lon').innerText     = safeVal(d.gps_lon);
    document.getElementById('alt').innerText     = toFixedSafe(d.altitude, 2) + " m";
    document.getElementById('heading').innerText = Math.round(safeVal(d.heading, 0)) + "°";

    document.getElementById('airspeed').innerText   = Math.round(safeVal(d.airspeed, 0)) + " m/s";
    document.getElementById('groundspeed').innerText = Math.round(safeVal(d.groundspeed, 0)) + " m/s";
    document.getElementById('climb').innerText      = Math.round(safeVal(d.climb, 0)) + " m/s";

    document.getElementById('voltage').innerText = toFixedSafe(d.voltage, 2) + " V";
    document.getElementById('current').innerText = toFixedSafe(d.current, 2) + " A";

    const batt = Math.round(safeVal(d.battery_pct, 0));
    const battEl = document.getElementById('batt_pct');
    battEl.innerText = batt + "%";
    battEl.style.color = batt > 20 ? 'var(--success)' : 'var(--danger)';

    document.getElementById('charging').innerText   = d.charging || 'Unknown';
    document.getElementById('vib_x').innerText      = toFixedSafe(d.vib_x, 3);
    document.getElementById('vib_y').innerText      = toFixedSafe(d.vib_y, 3);
    document.getElementById('vib_z').innerText      = toFixedSafe(d.vib_z, 3);
    document.getElementById('baro_press').innerText = toFixedSafe(d.baro_press, 2) + " hPa";
    document.getElementById('baro_temp').innerText  = toFixedSafe(d.baro_temp, 2) + " °C";
  } catch(e) {}
}

// ── AI INTEL POLLING ──────────────────────────
function statusClass(s) {
  s = (s||'').toLowerCase();
  if(s==='critical') return 'critical';
  if(s==='warning')  return 'warning';
  if(s==='ok')       return 'ok';
  return 'standby';
}

function extractPercent(text) {
  if(!text) return 0;
  const m = text.match(/(\d+)/);
  return m ? Math.min(100, parseInt(m[1])) : 50;
}

function barColor(pct) {
  if(pct >= 70) return 'var(--success)';
  if(pct >= 40) return 'var(--warning)';
  return 'var(--danger)';
}

function drawSparkline(history) {
  const canvas = document.getElementById('spark-canvas');
  if(!canvas) return;
  canvas.width = canvas.offsetWidth || 400;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  if(!history || history.length < 2) return;

  const vals = history.map(h => parseFloat(h.batt) || 0);
  const max = 100, min = 0;
  const W = canvas.width, H = canvas.height;
  const pad = 6;

  ctx.beginPath();
  ctx.strokeStyle = '#bf00ff';
  ctx.lineWidth = 2;
  ctx.shadowBlur = 8;
  ctx.shadowColor = '#bf00ff';

  vals.forEach((v, i) => {
    const x = pad + (i / (vals.length - 1)) * (W - 2*pad);
    const y = H - pad - ((v - min) / (max - min)) * (H - 2*pad);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.stroke();

  // Gradient fill
  const grad = ctx.createLinearGradient(0, 0, 0, H);
  grad.addColorStop(0, 'rgba(191,0,255,0.3)');
  grad.addColorStop(1, 'rgba(191,0,255,0)');
  ctx.lineTo(W - pad, H);
  ctx.lineTo(pad, H);
  ctx.closePath();
  ctx.fillStyle = grad;
  ctx.fill();
}

async function fetchAI() {
  try {
    const res = await fetch('/api/ai', { cache: 'no-store' });
    if (!res.ok) return;
    const d = await res.json();

    // Status banner
    const sc = statusClass(d.status);
    const dot   = document.getElementById('ai-dot');
    const label = document.getElementById('ai-status-label');
    dot.className   = 'ai-status-dot ' + sc;
    label.className = 'ai-status-label ' + sc;
    label.innerText = (d.status || 'STANDBY').toUpperCase();
    document.getElementById('ai-summary').innerText  = d.summary  || '—';
    document.getElementById('ai-updated').innerText  = 'Last: ' + (d.last_updated || '—');

    // Alerts
    const alertEl = document.getElementById('ai-alerts');
    if(d.alerts && d.alerts.length > 0) {
      alertEl.innerHTML = d.alerts.map(a => `<li>${a}</li>`).join('');
    } else {
      alertEl.innerHTML = '<li class="none">✓ No alerts — system nominal</li>';
    }

    // Predictions
    const predEl = document.getElementById('ai-predictions');
    if(d.predictions && d.predictions.length > 0) {
      predEl.innerHTML = d.predictions.map(p => `<li class="pred">⚠ ${p}</li>`).join('');
    } else {
      predEl.innerHTML = '<li class="none">✓ No predictions — looking good</li>';
    }

    // Health bars
    const battPct = parseFloat(d.battery_pct_raw) || extractPercent(d.battery_health);
    const vibPct  = 100 - Math.min(100, (parseFloat(d.vib_raw) || 0) * 200);
    const stabPct = extractPercent(d.stability_score);

    const barBatt = document.getElementById('bar-battery');
    barBatt.style.width = battPct + '%';
    barBatt.style.background = barColor(battPct);
    document.getElementById('txt-battery').innerText = battPct + '%';

    const barVib = document.getElementById('bar-vib');
    barVib.style.width = Math.max(0, vibPct) + '%';
    barVib.style.background = barColor(vibPct);
    document.getElementById('txt-vib').innerText = Math.round(vibPct) + '%';

    const barStab = document.getElementById('bar-stab');
    barStab.style.width = stabPct + '%';
    barStab.style.background = 'var(--ai-color)';
    document.getElementById('txt-stab').innerText = stabPct + '%';

    // Text fields
    document.getElementById('ai-bat-health').innerText = d.battery_health   || '—';
    document.getElementById('ai-vib-health').innerText = d.vibration_health  || '—';
    document.getElementById('ai-stab').innerText       = d.stability_score   || '—';

    // History table
    const tbody = document.getElementById('ai-history');
    if(d.history && d.history.length > 0) {
      tbody.innerHTML = [...d.history].reverse().map(h => `
        <tr>
          <td>${h.t}</td>
          <td><span class="badge ${statusClass(h.status)}">${h.status}</span></td>
          <td>${parseFloat(h.batt||0).toFixed(1)}%</td>
          <td>${parseFloat(h.vib||0).toFixed(3)}</td>
          <td>${parseFloat(h.alt||0).toFixed(1)}m</td>
        </tr>`).join('');
    }

    // Sparkline
    drawSparkline(d.history || []);

    // Flight report
    if(d.flight_report && d.flight_report.length > 10) {
      document.getElementById('ai-report').innerText   = d.flight_report;
      document.getElementById('report-ts').innerText   = 'updated: ' + (d.report_updated || '—');
    }

  } catch(e) {}
}

// ── IMAGE FUNCTIONS (untouched) ───────────────
var hiddenBefore = 0;
async function loadImages() {
  try {
    var r = await fetch('/api/images?hidden_before=' + hiddenBefore, { cache: 'no-store' });
    if (!r.ok) return;
    var images = await r.json();
    var grid = document.getElementById('img_grid');
    document.getElementById('img_count').textContent = images.length + ' DATA FRAMES';
    if (images.length === 0) {
      grid.innerHTML = '<div style="color:var(--text-muted); grid-column: 1/-1; font-family: consolas;">> AWAITING DOWNLINK PACKETS...</div>';
      return;
    }
    grid.innerHTML = images.slice().reverse().map(function(img) {
      return '<div class="img-card">' +
        '<img src="/images/' + img.filename + '" loading="lazy" onerror="this.hidden=true">' +
        '<div class="img-info">' +
        '<div class="img-name">FILE: ' + img.filename + '</div>' +
        '<div class="img-time">RECV_T: ' + img.received_at + '</div>' +
        '<div class="img-size">SIZE_B: ' + (img.size/1024).toFixed(1) + ' KB</div>' +
        '</div></div>';
    }).join('');
  } catch(e) {}
}

async function clearView() {
  try {
    var r = await fetch('/api/clear_view', { method:'POST', cache:'no-store' });
    var d = await r.json();
    hiddenBefore = d.hidden_before;
    loadImages();
  } catch(e) {}
}

// ── POLLING INTERVALS ─────────────────────────
setInterval(fetchTelemetry, 100);
setInterval(fetchAI, 5000);
setInterval(() => { if(document.getElementById('images').classList.contains('active')) loadImages(); }, 4000);
fetchTelemetry();
fetchAI();
</script>
</body>
</html>'''

@web_app.route('/')
def index():
    return HTML

@web_app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)

@web_app.route('/api/telemetry')
def api_telemetry():
    with state_lock:
        d = dict(latest_telemetry)
        d['charging'] = charging_status
    return jsonify(d)

@web_app.route('/api/images')
def api_images():
    try:
        hidden = int(request.args.get('hidden_before', 0))
    except (ValueError, TypeError):
        hidden = 0
    with state_lock:
        imgs = list(received_images[hidden:])
    return jsonify(imgs)

@web_app.route('/api/clear_view', methods=['POST'])
def api_clear_view():
    global hidden_before
    with state_lock:
        hidden_before = len(received_images)
        hb = hidden_before
    return jsonify({'hidden_before': hb})

# ── NEW: AI endpoint ──────────────────────────
@web_app.route('/api/ai')
def api_ai():
    with ai_lock:
        # Also pass raw sensor values for accurate bar rendering
        with state_lock:
            t = dict(latest_telemetry)
        data = dict(ai_state)
        data['battery_pct_raw'] = t.get('battery_pct', 0)
        data['vib_raw']         = t.get('vib_z', 0)
    return jsonify(data)

def run_web_server():
    print(f'[{now()}] [*] Website running on http://0.0.0.0:{WEB_PORT}')
    web_app.run(host='0.0.0.0', port=WEB_PORT, threaded=True, use_reloader=False)

# ─────────────────────────────────────────────
# LOAD EXISTING IMAGES ON STARTUP
# ─────────────────────────────────────────────
def load_existing_images():
    files = sorted([f for f in os.listdir(IMAGE_DIR) if f.endswith('.jpg')])
    for f in files:
        fp    = os.path.join(IMAGE_DIR, f)
        size  = os.path.getsize(fp)
        mtime = datetime.fromtimestamp(os.path.getmtime(fp)).strftime('%Y-%m-%d %H:%M:%S')
        received_images.append({'filename': f, 'received_at': mtime, 'size': size})
    print(f'[{now()}] [*] Loaded {len(files)} existing images')

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    cleanup()
    load_existing_images()

    threading.Thread(target=log_worker,       daemon=True).start()
    threading.Thread(target=handle_discovery, daemon=True).start()
    threading.Thread(target=handle_telemetry, daemon=True).start()
    threading.Thread(target=handle_images,    daemon=True).start()
    threading.Thread(target=run_esp_server,   daemon=True).start()
    threading.Thread(target=ai_realtime_loop, daemon=True).start()   # ← AI thread

    run_web_server()
