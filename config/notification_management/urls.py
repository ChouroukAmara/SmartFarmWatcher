from django.urls import path
from .views import (
    NotificationListView,
    mark_notification_read,
    mark_all_read,
    store_fcm_token,
    notification_list,
    delete_notification,
    clear_all_notifications,
)

app_name = 'notification_management'

urlpatterns = [
    path('notifications/', NotificationListView.as_view(), name='notifications'),
    path('notifications/<int:notification_id>/read/', mark_notification_read, name='mark_notification_read'),
    path('notifications/mark-all-read/', mark_all_read, name='mark_all_read'),
    path('fcm-token/', store_fcm_token, name='store_fcm_token'),

    # ── NOUVELLE route HTML (page web, pas API) ──
    path('notifications/page/', notification_list, name='notification_list'),
    path('notifications/<int:notification_id>/delete/', delete_notification, name='delete_notification'),
    path('notifications/clear-all/', clear_all_notifications, name='clear_all_notifications'),
]