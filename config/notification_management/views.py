# Django REST Framework imports
from rest_framework import generics, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

# Local app imports
from .models import Notification, FCMToken
from .serializers import NotificationSerializer

from django.contrib.auth.decorators import login_required
from django.shortcuts import render


class NotificationListView(generics.ListAPIView):
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return Notification.objects.filter(user=self.request.user).select_related('detection', 'detection__camera', 'detection__detection_type')

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_notification_read(request, notification_id):
    try:
        notification = Notification.objects.get(id=notification_id, user=request.user)
        notification.is_read = True
        notification.save()
        return Response({'status': 'success'})
    except Notification.DoesNotExist:
        return Response({'error': 'Notification not found'}, status=status.HTTP_404_NOT_FOUND)

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def mark_all_read(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return Response({'status': 'success'})

@api_view(['POST'])
@permission_classes([IsAuthenticated])
def store_fcm_token(request):
    token = request.data.get('fcm_token')
    if token:
        FCMToken.objects.update_or_create(
            user=request.user,
            defaults={'token': token, 'is_active': True}
        )
        return Response({'status': 'success'})
    return Response({'error': 'Token required'}, status=status.HTTP_400_BAD_REQUEST)




@login_required
def notification_list(request):
    """
    Simple HTML page listing all notifications for the logged-in user
    (supervisor or client). Separate from the DRF API above, which is
    used for JSON/mobile access.
    """
    notifications = Notification.objects.filter(user=request.user).order_by('-created_at')
    return render(request, 'notification_management/notification_list.html', {
        'notifications': notifications,
    })





from django.shortcuts import redirect
from django.views.decorators.http import require_POST


@login_required
@require_POST
def delete_notification(request, notification_id):
    """Deletes a single notification belonging to the logged-in user."""
    try:
        notification = Notification.objects.get(id=notification_id, user=request.user)
        notification.delete()
    except Notification.DoesNotExist:
        pass
    return redirect('notification_management:notification_list')


@login_required
@require_POST
def clear_all_notifications(request):
    """Deletes ALL notifications for the logged-in user."""
    Notification.objects.filter(user=request.user).delete()
    return redirect('notification_management:notification_list')