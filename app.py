import json
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from config import CAMERA_DEVICE, CAMERA_FPS, CAMERA_HEIGHT, CAMERA_STREAM_URL, CAMERA_WIDTH, ESP32_URL, HOST, PORT, REQUEST_TIMEOUT

DASHBOARD = """<!doctype html>
<html><head><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Raspberry Pi Rover</title><style>
body{font-family:system-ui,sans-serif;max-width:680px;margin:auto;padding:20px;background:#17212b;color:#f5f7fa}
h1{color:#55d6be}.status{padding:12px;background:#243442;border-radius:8px;margin:12px 0}
.camera{width:100%;aspect-ratio:16/9;object-fit:cover;background:#0d141b;border-radius:8px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;max-width:360px;margin:20px auto}
.mode{display:flex;gap:8px;margin:16px 0}.mode button{min-height:42px;flex:1}
.joystick{width:min(72vw,280px);aspect-ratio:1;margin:20px auto;background:#243442;border:2px solid #647484;border-radius:50%;position:relative;touch-action:none}
.joystick:after{content:'';position:absolute;inset:18%;border:1px dashed #647484;border-radius:50%}
.stick{position:absolute;width:76px;aspect-ratio:1;left:50%;top:50%;transform:translate(-50%,-50%);border:0;border-radius:50%;background:#2e8bdb;z-index:1}
.stop{display:block;margin:12px auto;min-height:48px;padding:0 30px;border:0;border-radius:8px;background:#d94b4b;color:white;font-size:16px;font-weight:600}
label{display:block;margin:10px 0 4px}
input{width:100%;box-sizing:border-box;padding:10px;border-radius:6px;border:1px solid #647484;background:#243442;color:white;font-size:16px}
small{color:#b9c5cf}#gps{line-height:1.7}
</style></head><body><h1>Raspberry Pi Rover</h1>
<small>Pi gateway: ESP32_URL_PLACEHOLDER</small><div class=status id=gps>Loading GPS...</div>
CAMERA_PANEL
<label>Speed (0-255)</label><input id=speed type=number min=1 max=255 value=150>
<div class=mode><button onclick="setMode('dpad')">D-pad</button><button onclick="setMode('joystick')">Joystick</button></div>
<div id=dpad class=grid><span></span><button onclick="move('FORWARD',150,5)">Forward</button><span></span>
<button onclick="move('LEFT',150,10)">Left</button><button class=stop onclick=stop()>STOP</button><button onclick="move('RIGHT',150,10)">Right</button>
<span></span><button onclick="move('BACKWARD',150,5)">Backward</button><span></span></div>
<div class=joystick id=joystick aria-label="Rover navigation joystick"><button class=stick id=stick aria-label="Joystick handle"></button></div>
<button class=stop onclick=stop()>STOP</button>
<script>
async function stop(){await fetch('/api/stop',{method:'POST'});}
const speedEl=document.querySelector('#speed'),joystick=document.querySelector('#joystick'),stick=document.querySelector('#stick');
let active=false,joystickX=0,joystickY=0,joystickTimer;
async function move(direction,speed,value){await fetch('/api/command?'+new URLSearchParams({direction,speed,value}),{method:'POST'});}
function setMode(mode){stop();document.querySelector('#dpad').style.display=mode==='dpad'?'grid':'none';joystick.style.display=mode==='joystick'?'block':'none';}
function commandFromJoystick(){const distance=Math.hypot(joystickX,joystickY);if(distance<0.12){stop();return;}const speed=Math.max(30,Math.min(255,Math.round(distance*255)));let direction,value;if(Math.abs(joystickX)>Math.abs(joystickY)){direction=joystickX<0?'LEFT':'RIGHT';value=10;}else{direction=joystickY<0?'FORWARD':'BACKWARD';value=5;}move(direction,Math.min(Number(speedEl.value),speed),value);}
function updateJoystick(event){const bounds=joystick.getBoundingClientRect(),radius=bounds.width/2,limit=radius-40;let x=event.clientX-(bounds.left+radius),y=event.clientY-(bounds.top+radius),length=Math.hypot(x,y);if(length>limit){x=x*limit/length;y=y*limit/length;}joystickX=x/limit;joystickY=y/limit;stick.style.left=`${50+joystickX*40}%`;stick.style.top=`${50+joystickY*40}%`;commandFromJoystick();}
function releaseJoystick(){active=false;clearInterval(joystickTimer);joystickX=0;joystickY=0;stick.style.left='50%';stick.style.top='50%';stop();}
joystick.addEventListener('pointerdown',event=>{active=true;joystick.setPointerCapture(event.pointerId);updateJoystick(event);clearInterval(joystickTimer);joystickTimer=setInterval(()=>{if(active)commandFromJoystick();},120);});
joystick.addEventListener('pointermove',event=>{if(active)updateJoystick(event);});
joystick.addEventListener('pointerup',releaseJoystick);joystick.addEventListener('pointercancel',releaseJoystick);window.addEventListener('blur',releaseJoystick);
setMode('joystick');
async function refresh(){try{let d=await (await fetch('/api/status')).json();let g=d.gps;document.querySelector('#gps').innerHTML=g.fix?`GPS fix<br>Lat: ${g.latitude.toFixed(6)}<br>Lon: ${g.longitude.toFixed(6)}<br>Alt: ${g.altitude.toFixed(1)} m | Satellites: ${g.satellites}`:'Waiting for GPS fix';}catch(e){document.querySelector('#gps').textContent='ESP32 connection lost';}}
setInterval(refresh,1000);refresh();
</script></body></html>""".replace("ESP32_URL_PLACEHOLDER", ESP32_URL).replace("CAMERA_PANEL", "<img class=camera src='" + CAMERA_STREAM_URL + "' alt='Camera stream'>" if CAMERA_STREAM_URL else "<img class=camera src='/camera' alt='USB webcam stream'>")


def esp32_request(path, method="GET"):
    request = Request(ESP32_URL + path, method=method)
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.status, response.read(), response.headers.get_content_type()


class RoverHandler(BaseHTTPRequestHandler):
    def send_payload(self, status, payload, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == "/":
            payload = DASHBOARD.encode()
            self.send_payload(200, payload, "text/html; charset=utf-8")
            return
        if self.path == "/api/status":
            self.proxy("/api/status", "GET")
            return
        if self.path == "/camera":
            self.stream_camera()
            return
        self.send_payload(404, b'{"error":"not found"}')

    def stream_camera(self):
        command = [
            "ffmpeg", "-loglevel", "error", "-f", "v4l2",
            "-input_format", "yuyv422", "-video_size", f"{CAMERA_WIDTH}x{CAMERA_HEIGHT}",
            "-framerate", CAMERA_FPS, "-i", CAMERA_DEVICE,
            "-f", "mpjpeg", "-q:v", "6", "pipe:1",
        ]
        process = None
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=ffmpeg")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            while True:
                chunk = process.stdout.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            if process is not None:
                process.terminate()
                process.wait(timeout=2)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/stop":
            self.proxy("/api/stop", "POST")
            return
        if parsed.path == "/api/command":
            values = parse_qs(parsed.query)
            direction = values.get("direction", [""])[0].upper()
            try:
                speed = int(values.get("speed", [""])[0])
                value = float(values.get("value", [""])[0])
            except ValueError:
                self.send_payload(400, b'{"error":"invalid speed or value"}')
                return
            if direction not in {"FORWARD", "BACKWARD", "LEFT", "RIGHT"} or not 1 <= speed <= 255 or value <= 0:
                self.send_payload(400, b'{"error":"invalid navigation command"}')
                return
            self.proxy("/api/command?" + urlencode({"direction": direction, "speed": speed, "value": value}), "POST")
            return
        self.send_payload(404, b'{"error":"not found"}')

    def proxy(self, path, method):
        try:
            status, payload, content_type = esp32_request(path, method)
            self.send_payload(status, payload, content_type)
        except (HTTPError, URLError, TimeoutError, OSError) as error:
            payload = json.dumps({"error": "ESP32 unavailable", "detail": str(error)}).encode()
            self.send_payload(502, payload)

    def log_message(self, format, *args):
        print("%s - %s" % (self.address_string(), format % args))


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), RoverHandler)
    print(f"Rover dashboard listening on http://{HOST}:{PORT}")
    print(f"Forwarding commands to {ESP32_URL}")
    server.serve_forever()
