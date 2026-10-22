from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.shortcuts import render
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Node, NodeData


def _get_user_nodes(user):
    """
    Active nodes visible to this user.
    - Clients: projects they have access to (via UserProjectRole).
    - Supervisors: projects they created.
    """
    from project_management.models import UserProjectRole

    if user.user_type == 'client':
        project_ids = UserProjectRole.objects.filter(
            user=user, role='client', is_active=True
        ).values_list('project_id', flat=True)
    else:
        project_ids = user.created_projects.values_list('id', flat=True)

    return Node.objects.filter(project_id__in=project_ids, is_active=True)


@login_required
def sensor_history(request):
    """Web page: sensor reading history."""
    nodes = _get_user_nodes(request.user)

    selected_node_id = request.GET.get('node')
    readings = NodeData.objects.filter(node__in=nodes).select_related('node').order_by('-created_at')

    if selected_node_id:
        readings = readings.filter(node_id=selected_node_id)

    paginator = Paginator(readings, 50)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'iot_management/sensor_history.html', {
        'page_obj': page_obj,
        'nodes': nodes,
        'selected_node_id': int(selected_node_id) if selected_node_id else None,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def sensor_history_api(request):
    """
    JSON API for the mobile app.
    Query params: ?node=<id>&page=<n>&page_size=<n>
    """
    try:
        nodes = _get_user_nodes(request.user)
        readings = (
            NodeData.objects.filter(node__in=nodes)
            .select_related('node')
            .order_by('-created_at')
        )

        node_id = request.GET.get('node')
        if node_id:
            try:
                readings = readings.filter(node_id=int(node_id))
            except ValueError:
                return Response({'success': False, 'error': 'Invalid node id'}, status=400)

        try:
            page_size = min(max(int(request.GET.get('page_size', 30)), 1), 100)
        except ValueError:
            page_size = 30

        paginator = Paginator(readings, page_size)
        try:
            page_obj = paginator.page(request.GET.get('page', 1))
            page_items = list(page_obj)
            page_number = page_obj.number
            has_next = page_obj.has_next()
        except (EmptyPage, PageNotAnInteger):
            page_items, page_number, has_next = [], 0, False

        results = [
            {
                'id': r.id,
                'node_id': r.node_id,
                'node_name': r.node.name,
                'node_type': r.node.node_type,  # 'env' or 'water'
                'temperature': r.temperature,
                'humidity': r.humidity,
                'pressure': r.pressure,
                'soil_moisture': r.soil_moisture,
                'salinity': r.salinity,
                'created_at': r.created_at,
            }
            for r in page_items
        ]

        return Response({
            'success': True,
            'count': paginator.count,
            'page': page_number,
            'num_pages': paginator.num_pages,
            'has_next': has_next,
            'nodes': [
                {'id': n.id, 'name': n.name, 'node_type': n.node_type}
                for n in nodes
            ],
            'results': results,
        })
    except Exception as e:
        return Response({'success': False, 'error': str(e)}, status=500)