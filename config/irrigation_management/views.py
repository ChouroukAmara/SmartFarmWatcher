from django.shortcuts import render
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from .models import IrrigationLog
from project_management.models import UserProjectRole

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from project_management.models import Project

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def latest_irrigation(request):
    try:
        user_projects = UserProjectRole.objects.filter(
            user=request.user
        ).values_list('project_id', flat=True)
        
        log = IrrigationLog.objects.filter(
            project_id__in=user_projects
        ).first()
        
        if not log:
            return Response({'detail': 'No data'}, status=404)
        
        return Response({
            'b_totaux_ml': log.b_totaux_ml,
            'temperature': log.temperature,
            'humidity': log.humidity,
            'pressure': log.pressure,
            'soil_moisture': log.soil_moisture,
            'advice_level': log.advice_level,
            'message': log.message,
            'computed_at': log.computed_at,
            'ecw_dSm': log.ecw_dSm,    # ← AJOUTE
            'pu_mm': log.pu_mm,          # ← AJOUTE
        })
    except Exception as e:
        return Response({'error': str(e)}, status=500)

@login_required
def irrigation_history(request):
    """
    Affiche l'historique des estimations d'irrigation pour les projets du superviseur.
    Filtrable par projet.
    """
    user_projects = Project.objects.filter(created_by=request.user, is_active=True)

    logs = IrrigationLog.objects.filter(
        project__in=user_projects
    ).select_related('node', 'project', 'project__crop').order_by('-computed_at')

    selected_project_id = request.GET.get('project')
    if selected_project_id:
        logs = logs.filter(project_id=selected_project_id)

    paginator = Paginator(logs, 25)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'irrigation_management/irrigation_history.html', {
        'page_obj': page_obj,
        'projects': user_projects,
        'selected_project_id': int(selected_project_id) if selected_project_id else None,
    })