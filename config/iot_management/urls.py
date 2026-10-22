from django.urls import path
from . import views

app_name = 'iot_management'

urlpatterns = [
    path('sensor-history/', views.sensor_history, name='sensor_history'),
    path('api/sensor-history/', views.sensor_history_api, name='sensor_history_api'),
]