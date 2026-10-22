import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async


class IoTConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        await self.channel_layer.group_add("iot_data", self.channel_name)
        await self.accept()
        print("✅ WebSocket connected")

    async def disconnect(self, close_code):
        await self.channel_layer.group_discard("iot_data", self.channel_name)
        print("❌ WebSocket disconnected")

    async def send_data(self, event):
        await self.send(text_data=json.dumps(event["data"]))

    async def receive(self, text_data):
        try:
            data = json.loads(text_data)
        except json.JSONDecodeError:
            return

        await self._save_node_data(data)

        dev_eui = data.get("dev_eui")
        T  = data.get("temperature")
        RH = data.get("humidity")
        P  = data.get("pressure", 1013.0)

        # ✅ Lance FAO-56 seulement si node env (T et RH présents)
        if dev_eui and T is not None and RH is not None:
            from irrigation_management.tasks import compute_fao56_task
            compute_fao56_task.delay(
                dev_eui=dev_eui,
                temperature=float(T),
                humidity=float(RH),
                pressure=float(P),
            )

        await self.channel_layer.group_send(
            "iot_data",
            {"type": "send_data", "data": data}
        )

    @database_sync_to_async
    def _save_node_data(self, data):
        try:
            from iot_management.models import Node, NodeData
            dev_eui = data.get("dev_eui")
            if not dev_eui:
                return
            node = Node.objects.get(dev_eui=dev_eui, is_active=True)
            NodeData.objects.create(
                node=node,
                temperature=data.get("temperature"),
                humidity=data.get("humidity"),
                pressure=data.get("pressure"),
                gas=data.get("gas"),
                soil_moisture=data.get("soil_moisture"),
                salinity=data.get("salinity"),
            )
            print(
                f"[NodeData] ✅ Saved [{node.node_type}] — "
                f"T={data.get('temperature')} "
                f"RH={data.get('humidity')} "
                f"soil={data.get('soil_moisture')}% "
                f"sal={data.get('salinity')} dS/m"
            )
        except Node.DoesNotExist:
            print(f"[NodeData] Node {data.get('dev_eui')} not found or inactive")
        except Exception as e:
            print(f"[NodeData] Save error: {e}")