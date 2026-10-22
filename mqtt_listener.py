import os
import json
import ssl
import django
import paho.mqtt.client as mqtt

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from iot_management.models import Node, NodeData
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

TTN_HOST = "eu1.cloud.thethings.network"
TTN_PORT = 8883
TTN_USER = "stm32-lorawan-test@ttn"
# ⚠️ TODO: déplacer TTN_PASS dans une variable d'environnement avant la soutenance
# TTN_PASS = os.environ.get("TTN_PASS")
TTN_PASS = "NNSXS.X653WVSDYKXIASWGCF5C3OUOVILFBT6J5OIS2IA.R4V4HI5LBTQ7TSM6OQZ2PFQSOIKOIS6VZOCGI3R24ZJD7LUXHLBA"
TOPIC    = "v3/stm32-lorawan-test@ttn/devices/+/up"

channel_layer = get_channel_layer()


def on_connect(client, userdata, flags, rc):
    if rc == 0:
        print("✅ Connected to TTN MQTT Broker")
        client.subscribe(TOPIC)
        print("📡 Listening on:", TOPIC)
    else:
        print("❌ Connection failed code:", rc)


def on_disconnect(client, userdata, rc):
    print("🔌 Disconnected from TTN. Code:", rc)


def on_message(client, userdata, msg):
    try:
        print("\n📩 Message received:", msg.topic)

        payload  = json.loads(msg.payload.decode("utf-8"))
        end_device = payload.get("end_device_ids", {})
        uplink     = payload.get("uplink_message", {})
        decoded    = uplink.get("decoded_payload", {})
        dev_eui    = end_device.get("dev_eui")

        print("📡 DEV_EUI:", dev_eui)
        print("📡 DECODED:", decoded)

        if not dev_eui:
            print("⚠️ No dev_eui found")
            return

        node = Node.objects.filter(dev_eui=dev_eui).first()
        if not node:
            print("⚠️ Node not found in Django:", dev_eui)
            return

        # ── Save raw sensor data — TOUJOURS, à chaque message reçu ──
        # (historique complet, utilisé pour les graphiques et la détection
        #  de capteur hors-ligne, indépendamment du calcul d'irrigation)
        node_data = NodeData.objects.create(
            node=node,
            temperature=decoded.get("temperature"),
            humidity=decoded.get("humidity"),
            pressure=decoded.get("pressure"),
            gas=decoded.get("gas"),
            soil_moisture=decoded.get("soil_moisture"),
            salinity=decoded.get("salinity"),
        )
        print(f"✅ Data saved in PostgreSQL [{node.node_type}]")

        # ── SUPPRIMÉ : compute_fao56_task n'est plus déclenché ici ──
        # Le calcul FAO-56 + recommandation ML se fait maintenant UNE
        # SEULE FOIS PAR JOUR, à 6h00, via Celery Beat
        # (irrigation_management.tasks.compute_daily_irrigation_recommendations).
        # Ce handler ne fait plus qu'enregistrer la donnée brute du capteur.

        # ── NOUVEAU : verification IMMEDIATE de la salinite critique ──
        # Contrairement au calcul quotidien ci-dessus, cette alerte doit
        # arriver tout de suite, pas attendre 6h00 le lendemain.
        if node.node_type == "water":
            salinity = decoded.get("salinity")
            if salinity is not None:
                from irrigation_management.tasks import check_critical_salinity
                check_critical_salinity.delay(node.project_id, float(salinity))
                print(f"🧪 Salinity check launched for project {node.project_id}: {salinity} dS/m")

        # Send WebSocket
        async_to_sync(channel_layer.group_send)(
            "iot_data",
            {
                "type": "send_data",
                "data": {
                    "node": node.name,
                    "dev_eui": dev_eui,
                    **decoded,
                    "time": str(node_data.created_at),
                },
            },
        )
        print("🚀 Data sent to WebSocket")

    except Exception as e:
        print("❌ Error:", str(e))


client = mqtt.Client()
client.username_pw_set(TTN_USER, TTN_PASS)
client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
client.on_connect    = on_connect
client.on_disconnect = on_disconnect
client.on_message    = on_message

# ── Reconnexion automatique en cas d'echec (DNS/reseau momentane) ──
# Au lieu de planter des la premiere erreur (ce qui laissait le conteneur
# eteint sans redemarrage automatique), on retente indefiniment avec
# un delai croissant, pour survivre aux coupures reseau temporaires
# (VPN qui s'active, changement de connexion, etc.)
import time

RETRY_DELAY_SECONDS = 10
MAX_RETRY_DELAY_SECONDS = 120

while True:
    try:
        print("🔄 Connecting to TTN MQTT Broker...")
        client.connect(TTN_HOST, TTN_PORT, 60)
        RETRY_DELAY_SECONDS = 10  # reset apres connexion reussie
        client.loop_forever()
    except Exception as e:
        print(f"❌ Connection error: {e}")
        print(f"⏳ Retrying in {RETRY_DELAY_SECONDS}s...")
        time.sleep(RETRY_DELAY_SECONDS)
        RETRY_DELAY_SECONDS = min(RETRY_DELAY_SECONDS * 2, MAX_RETRY_DELAY_SECONDS)