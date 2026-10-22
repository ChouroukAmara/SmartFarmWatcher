from django.urls import re_path
from .consumers import IoTConsumer

websocket_urlpatterns = [
    re_path(r'ws/iot/$', IoTConsumer.as_asgi()),
]