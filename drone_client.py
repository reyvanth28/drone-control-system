import socket
import struct
import time
import subprocess
import os
import signal
import sys
import threading
import json
import math
from datetime import datetime
from pymavlink import mavutil

# CONFIG
DISCOVERY_PORT = 5001
IMAGE_PORT = 5000
TELEMETRY_PORT = 6000
IMAGE_INTERVAL = 10
TELEMETRY_INTERVAL = 0.2  # 🔥 Fast Telemetry: 5 times a second
BUFFER_DIR = '/tmp/pi_buffer'
MAVLINK_PORT = '/dev/serial0'
MAVLINK_BAUD = 921600
MAX_BUFFERED_IMAGES = 200 # 🔥 Max offline images to hold

os.makedirs(BUFFER_DIR, exist_ok=True)

def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')

def nowfile():
    return datetime.now().strftime('%d_%H-%M-%S')

# CLEANUP
def kill_camera():
    subprocess.run("pkill -9 -f rpicam", shell=True)
    subprocess.run("pkill -9 -f libcamera", shell=True)

def cleanup():
    kill_camera()

def force_exit(sig, frame):
    cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT, force_exit)

# DISCOVERY
def find_server(label, send, recv):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(2)

    while True:
        try:
            s.sendto(send, ('<broadcast>', DISCOVERY_PORT))
            msg, addr = s.recvfrom(1024)
            if msg == recv:
                return addr[0]
        except:
            pass

# IMAGE CAPTURE THREAD (Never blocks, caches offline)
counter = 1

def capture():
    global counter
    kill_camera()
    name = f"{counter:04d}_{nowfile()}.jpg"
    final_path = os.path.join(BUFFER_DIR, name)
    tmp_path = os.path.join(BUFFER_DIR, 'temp.jpg')

    # Save to temp file first, then atomic rename so sender doesn't read half a file
    subprocess.run(['rpicam-jpeg', '-o', tmp_path, '-t', '1000'])
    if os.path.exists(tmp_path):
        os.rename(tmp_path, final_path)
    
    counter += 1

def capture_loop():
    while True:
        try:
            capture()
            # Trim buffer to MAX_BUFFERED_IMAGES
            files = sorted([f for f in os.listdir(BUFFER_DIR) if f.endswith('.jpg')])
            while len(files) > MAX_BUFFERED_IMAGES:
                oldest = files.pop(0)
                try:
                    os.remove(os.path.join(BUFFER_DIR, oldest))
                except:
                    pass
        except Exception as e:
            pass
        time.sleep(IMAGE_INTERVAL)

# IMAGE SEND THREAD (Drains buffer to Base Server)
def send_img(sock, path):
    with open(path,'rb') as f:
        data = f.read()

    name = os.path.basename(path).encode()
    sock.sendall(struct.pack('>I', len(name)))
    sock.sendall(name)
    sock.sendall(struct.pack('>I', len(data)))
    sock.sendall(data)

    return sock.recv(3) == b'ACK'

def image_send_loop():
    while True:
        ip = find_server("IMG", b'PI_IMAGE_SENDER', b'BASE_SERVER_HERE')
        s = socket.socket()
        try:
            s.connect((ip, IMAGE_PORT))
            while True:
                files = sorted([f for f in os.listdir(BUFFER_DIR) if f.endswith('.jpg') and f != 'temp.jpg'])
                if files:
                    path = os.path.join(BUFFER_DIR, files[0])
                    if send_img(s, path):
                        try:
                            os.remove(path)
                        except:
                            pass
                else:
                    time.sleep(1) # Buffer empty, rest before checking again
        except Exception:
            pass # Pipe broke, loop back to find_server
        finally:
            s.close()

# MAVLINK
latest = {}
mav_lock = threading.Lock()

def mav_loop():
    master = mavutil.mavlink_connection(MAVLINK_PORT, baud=MAVLINK_BAUD)
    master.wait_heartbeat()

    master.mav.request_data_stream_send(
        master.target_system,
        master.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL,
        10, 
        1
    )

    while True:
        msg = master.recv_match(blocking=True)
        if msg:
            with mav_lock:
                latest[msg.get_type()] = msg

# TELEMETRY BUILD
def build():
    with mav_lock:
        snap = dict(latest)

    d = {'timestamp': now()}

    hb = snap.get('HEARTBEAT')
    if hb:
        d['armed'] = bool(hb.base_mode & mavutil.mavlink.MAV_MODE_FLAG_SAFETY_ARMED)
        d['mode'] = str(hb.custom_mode) 

    vfr = snap.get('VFR_HUD')
    if vfr:
        d['throttle'] = vfr.throttle
        d['heading'] = vfr.heading
        d['airspeed'] = round(vfr.airspeed, 2)
        d['groundspeed'] = round(vfr.groundspeed, 2)
        d['climb'] = round(vfr.climb, 2)

    ahrs2 = snap.get('AHRS2')
    if ahrs2:
        d['roll'] = round(math.degrees(ahrs2.roll),2)
        d['pitch'] = round(math.degrees(ahrs2.pitch),2)
        d['yaw'] = round(math.degrees(ahrs2.yaw),2)
        d['altitude'] = round(ahrs2.altitude,2)

    gps = snap.get('GPS_RAW_INT')
    if gps:
        d['gps_lat'] = gps.lat/1e7
        d['gps_lon'] = gps.lon/1e7

    bat = snap.get('SYS_STATUS')
    if bat:
        v = bat.voltage_battery/1000 if bat.voltage_battery != -1 else 0
        d['voltage'] = v
        d['current'] = bat.current_battery/100 if bat.current_battery != -1 else 0
        d['battery_pct'] = max(0,min(100,(v-9.6)/(12.6-9.6)*100)) if v else 0

    vib = snap.get('VIBRATION')
    if vib:
        d['vib_x'] = vib.vibration_x
        d['vib_y'] = vib.vibration_y
        d['vib_z'] = vib.vibration_z

    baro = snap.get('SCALED_PRESSURE')
    if baro:
        d['baro_press'] = baro.press_abs
        d['baro_temp'] = baro.temperature/100

    return d

# TELEMETRY SEND
def tele_loop():
    while True:
        ip = find_server("TEL", b'TELEMETRY_CLIENT', b'BASE_SERVER_HERE')
        s = socket.socket()
        try:
            s.connect((ip, TELEMETRY_PORT))
            while True:
                pkt = json.dumps(build()).encode()
                s.sendall(struct.pack('>I', len(pkt)))
                s.sendall(pkt)
                time.sleep(TELEMETRY_INTERVAL)
        except Exception:
            pass
        finally:
            s.close()

# MAIN
if __name__ == "__main__":
    cleanup()

    threading.Thread(target=mav_loop, daemon=True).start()
    threading.Thread(target=capture_loop, daemon=True).start() # Runs alone
    threading.Thread(target=image_send_loop, daemon=True).start() # Drains alone
    threading.Thread(target=tele_loop, daemon=True).start()

    while True:
        time.sleep(60)
