import sys, json
sys.path.insert(0, "/var/lib/webosbrew/gemini-controller")
from smartthings import SmartThings

devs = SmartThings.get_devices()
print("TOTAL DEVICES:", len(devs))
for d in devs:
    name = d.get('label') or d.get('name')
    room = d.get('roomId', '')
    caps = [c.get('id') for c in d.get('components', [{}])[0].get('capabilities', [])]
    print(f"- {name} (ID: {d.get('deviceId')}) -> {caps[:6]}")
