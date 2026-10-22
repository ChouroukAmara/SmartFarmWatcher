import json
import ssl
import time
import paho.mqtt.client as mqtt

TTN_HOST = "eu1.cloud.thethings.network"
TTN_PORT = 8883
TTN_USER = "stm32-lorawan-test@ttn"
TTN_PASS = "NNSXS.X653WVSDYKXIASWGCF5C3OUOVILFBT6J5OIS2IA.R4V4HI5LBTQ7TSM6OQZ2PFQSOIKOIS6VZOCGI3R24ZJD7LUXHLBA"
TOPIC = "v3/stm32-lorawan-test@ttn/devices/irrigation-smart/up"

payload = {
    "end_device_ids": {
        "device_id": "irrigation-smart",
        "dev_eui": "70B3D57ED0075EA4"
    },
    "uplink_message": {
        "decoded_payload": {
            "temperature": 27.4,
            "humidity": 58.2,
            "pressure": 1012.5,
            "gas": 120,
            "soil_moisture": 34.6,
            "salinity": 2.3
        }
    }
}

connected = False

def on_connect(client, userdata, flags, rc):
    global connected
    print("Connect result code:", rc)
    connected = (rc == 0)

def on_publish(client, userdata, mid):
    print("📤 Publish confirmé (mid =", mid, ")")

client = mqtt.Client()
client.username_pw_set(TTN_USER, TTN_PASS)
client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
client.on_connect = on_connect
client.on_publish = on_publish

client.connect(TTN_HOST, TTN_PORT, 60)
client.loop_start()          # ← démarre le thread réseau

time.sleep(2)                 # laisse le temps à CONNACK d'arriver
if not connected:
    print("❌ Pas connecté, abandon")
else:
    info = client.publish(TOPIC, json.dumps(payload), qos=1)
    info.wait_for_publish()   # ← attend la confirmation réelle d'envoi
    print("✅ Message publié et confirmé")

time.sleep(1)
client.loop_stop()
client.disconnect()