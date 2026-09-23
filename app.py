import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from config import ESP32_URL, HOST, PORT, REQUEST_TIMEOUT

DASHBOARD = """<!doctype html>
<html><head><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Raspberry Pi Rover</title><style>
body{font-family:system-ui,sans-serif;max-width:680px;margin:auto;padding:20px;background:#17212b;color:#f5f7fa}
h1{color:#55d6be}.status{padding:12px;background:#243442;border-radius:8px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;max-width:360px;margin:20px auto}
button{min-height:58px;border:0;border-radius:8px;background:#2e8bdb;color:white;font-size:16px;font-weight:600}
.stop{background:#d94b4b;grid-column:2}label{display:block;margin:10px 0 4px}
input{width:100%;box-sizing:border-box;padding:10px;border-radius:6px;border:1px solid #647484;background:#243442;color:white;font-size:16px}
small{color:#b9c5cf}#gps{line-height:1.7}
</style></head><body><h1>Raspberry Pi Rover</h1>
<small>Pi gateway: ESP32_URL_PLACEHOLDER</small><div class=status id=gps>Loading GPS...</div>
<label>Speed (0-255)</label><input id=speed type=number min=1 max=255 value=150>
<label>Distance (cm) or turn (degrees)</label><input id=value type=number min=1 max=10000 value=20>
<div class=grid><span></span><button onclick="move('FORWARD')">Forward</button><span></span>
<button onclick="move('LEFT')">Left</button><button class=stop onclick=stop()>STOP</button><button onclick="move('RIGHT')">Right</button>
<span></span><button onclick="move('BACKWARD')">Backward</button><span></span></div>
<script>
async function move(direction){let speed=Number(speedEl.value),value=Number(valueEl.value);await fetch('/api/command?'+new URLSearchParams({direction,speed,value}),{method:'POST'});}
async function stop(){await fetch('/api/stop',{method:'POST'});}
const speedEl=document.querySelector('#speed'),valueEl=document.querySelector('#value');
async function refresh(){try{let d=await (await fetch('/api/status')).json();let g=d.gps;document.querySelector('#gps').innerHTML=g.fix?`GPS fix<br>Lat: ${g.latitude.toFixed(6)}<br>Lon: ${g.longitude.toFixed(6)}<br>Alt: ${g.altitude.toFixed(1)} m | Satellites: ${g.satellites}`:'Waiting for GPS fix';}catch(e){document.querySelector('#gps').textContent='ESP32 connection lost';}}
setInterval(refresh,1000);refresh();
</script></body></html>""".replace("ESP32_URL_PLACEHOLDER", ESP32_URL)


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
        self.send_payload(404, b'{"error":"not found"}')

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
