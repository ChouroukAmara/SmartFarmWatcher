import os
from celery import Celery
from celery.schedules import crontab

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

app = Celery('config')
app.config_from_object('django.conf:settings', namespace='CELERY')
app.autodiscover_tasks()

# ── Daily irrigation scheduling ────────────────────────────────────
# compute_daily_irrigation_recommendations runs once a day at 6:00 AM,
# independently of how often sensors send data (see irrigation_management.tasks).
# check_offline_sensors runs every hour to detect silent sensors.
app.conf.beat_schedule = {
    'calcul-irrigation-quotidien': {
        'task': 'irrigation_management.tasks.compute_daily_irrigation_recommendations',
        'schedule': crontab(hour=11, minute=30),
    },
    'verifier-capteurs-silencieux': {
        'task': 'irrigation_management.tasks.check_offline_sensors',
        'schedule': crontab(minute=0),
    },
}