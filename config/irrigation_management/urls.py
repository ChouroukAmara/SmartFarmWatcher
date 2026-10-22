from django.urls import path
from . import views

app_name = 'irrigation_management'

urlpatterns = [
    path('history/', views.irrigation_history, name='irrigation_history'),
    path('api/latest/', views.latest_irrigation, name='latest_irrigation_api'),
]