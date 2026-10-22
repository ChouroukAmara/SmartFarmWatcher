# Python standard library
import json
from datetime import timedelta

# Django core imports
from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from django.core.paginator import Paginator
from django.db import transaction, models
from django.db.models import Q
from django.utils import timezone
from django.contrib.gis.geos import GEOSGeometry, Point

# Django REST Framework imports
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from utils.fire_risk import FireRiskPredictor

# Local app imports
from .models import Project, FarmBoundary, Camera, CropCoefficient
from .forms import ProjectForm
from iot_management.models import Node
from irrigation_management.models import IrrigationLog



@login_required
def create_project_wizard(request):
    """
    Single page project creation with 4 steps:
    1. Project Information
    2. Draw Farm Boundaries
    3. Place Cameras
    4. Review & Create
    """
    if request.method == 'POST':
        try:
            return _handle_project_creation(request)
        except Exception as e:
            messages.error(request, f'Error creating project: {str(e)}')
            return render(request, 'project_management/create_project_wizard.html', {
                'form': ProjectForm(),
                'step': 1,
                'crops_kc_json': '{}',    # ← NOUVEAU
            })
    
    # GET request - show the wizard
    form = ProjectForm()

    # ── NOUVEAU — prépare les données Kc pour le JS ──────────
    crops_kc = {}
    for crop in CropCoefficient.objects.all():
        crops_kc[crop.id] = {
            'name'    : crop.crop_name,
            'kc_ini'  : crop.kc_ini,
            'kc_mid'  : crop.kc_mid,
            'kc_end'  : crop.kc_end,
            'days_ini': crop.days_ini,
            'days_dev': crop.days_dev,
            'days_mid': crop.days_mid,
            'days_end': crop.days_end,
        }
    # ─────────────────────────────────────────────────────────
    context = {
        'form': form,
        'step': 1,
        'total_steps': 5,
        'crops_kc_json': json.dumps(crops_kc),
    }
    return render(request, 'project_management/create_project_wizard.html', context)


def _handle_project_creation(request):
    crop_id         = request.POST.get('crop_id', '').strip()
    planting_date   = request.POST.get('planting_date', '').strip()
    irrigation_type = request.POST.get('irrigation_type', 'drip').strip()
    soil_type       = request.POST.get('soil_type', 'loamy').strip()

    print(f"[DEBUG POST] crop_id={crop_id} planting_date={planting_date} irrigation={irrigation_type} soil={soil_type}")

    project_data = {
        'name':            request.POST.get('project_name'),
        'description':     request.POST.get('project_description', ''),
        'location_city':   request.POST.get('location_city', ''),
        'contact_person':  request.POST.get('contact_person', ''),
        'contact_phone':   request.POST.get('contact_phone', ''),
        'irrigation_type': irrigation_type,
        'soil_type':       soil_type,
    }
    
    # Parse farm boundaries data
    boundaries_json = request.POST.get('farm_boundaries_data', '[]')
    cameras_json = request.POST.get('cameras_data', '[]')
    nodes_json = request.POST.get('nodes_data', '[]')
    
    try:
        farm_boundaries_data = json.loads(boundaries_json)
        cameras_data = json.loads(cameras_json)
        nodes_data = json.loads(nodes_json)
    except json.JSONDecodeError:
        raise ValueError("Invalid farm boundaries or cameras data")
    
    
    # Validate project data
    form = ProjectForm(project_data)
    if not form.is_valid():
        raise ValueError(f"Project data validation failed: {form.errors}")
    
    # Create everything in a transaction
    with transaction.atomic():
        # Create the project
        project = Project(
            name=project_data['name'],
            description=project_data['description'],
            location_city=project_data['location_city'],
            contact_person=project_data['contact_person'],
            contact_phone=project_data['contact_phone'],
            created_by=request.user
        )
        # ── NOUVEAU ──────────────────────────────────────────
        

        # ✅ crop assigné AVANT save()
        if crop_id:
            try:
                project.crop = CropCoefficient.objects.get(pk=int(crop_id))
                print(f"[DEBUG] crop assigné: {project.crop}")
            except (CropCoefficient.DoesNotExist, ValueError) as e:
                print(f"[DEBUG] crop non trouvé: crop_id='{crop_id}' err={e}")

        # ✅ planting_date assigné AVANT save()
        if planting_date:
            from datetime import datetime
            try:
                project.planting_date = datetime.strptime(planting_date, '%Y-%m-%d').date()
                print(f"[DEBUG] planting_date: {project.planting_date}")
            except ValueError as e:
                print(f"[DEBUG] planting_date invalide: {e}")

        project.save()
        print(f"[DEBUG] Projet sauvé id={project.id} crop={project.crop} stage={project.current_stage} kc={project.current_kc}")
        
        # Create farm boundaries and map them
        boundary_mapping = {}
        created_boundaries = []
        
        for boundary_data in farm_boundaries_data:
            farm_boundary = FarmBoundary(
                project=project,
                description=boundary_data.get('description', '')
            )
            
            # Convert boundary geometry with proper handling
            boundary_geom = boundary_data.get('boundary')
            if boundary_geom:
                try:
                    if isinstance(boundary_geom, str):
                        geom_data = json.loads(boundary_geom)
                    else:
                        geom_data = boundary_geom
                    
                    # Handle different geometry structures
                    geometry_json = None
                    if 'geometry' in geom_data:
                        geometry_json = geom_data['geometry']
                    elif 'type' in geom_data and 'coordinates' in geom_data:
                        geometry_json = geom_data
                    
                    if geometry_json:
                        # Create the geometry object
                        geom = GEOSGeometry(json.dumps(geometry_json))
                        
                        # Convert Polygon to MultiPolygon if needed
                        if geom.geom_type == 'Polygon':
                            # Import MultiPolygon from GEOS
                            from django.contrib.gis.geos import MultiPolygon
                            # Convert single polygon to multipolygon
                            multi_geom = MultiPolygon(geom)
                            farm_boundary.boundary = multi_geom
                        elif geom.geom_type == 'MultiPolygon':
                            farm_boundary.boundary = geom
                        else:
                            continue
                    
                except Exception as e:
                    continue
            
            farm_boundary.full_clean()
            farm_boundary.save()
            
            created_boundaries.append(farm_boundary)
            
            # Map temporary ID to actual boundary
            temp_id = boundary_data.get('temp_id')
            if temp_id:
                boundary_mapping[str(temp_id)] = farm_boundary  # Ensure string key
        
        
        # Create cameras
        cameras_created = 0
        for camera_data in cameras_data:
            
            # Find the corresponding farm boundary
            temp_boundary_id = camera_data.get('farm_boundary_temp_id')
            
            # Try different ways to find the boundary
            farm_boundary = None
            
            if temp_boundary_id:
                # Try exact match first
                farm_boundary = boundary_mapping.get(str(temp_boundary_id))
                if not farm_boundary:
                    # Try as integer
                    farm_boundary = boundary_mapping.get(int(temp_boundary_id) if str(temp_boundary_id).isdigit() else temp_boundary_id)
            
            # If still no boundary found, use the first created boundary as fallback
            if not farm_boundary and created_boundaries:
                farm_boundary = created_boundaries[0]
            
            if not farm_boundary:
                continue
            
            
            camera = Camera(
                project=project,
                farm_boundary=farm_boundary,
                camera_type=camera_data.get('camera_type', 'ip'),
                description=camera_data.get('description', ''),
                ip_address=camera_data.get('ip_address'),
                port=camera_data.get('port'),
                cellular_identifier=camera_data.get('cellular_identifier')
            )
            
            # Set camera location
            location_data = camera_data.get('location')
            if location_data:
                lat = location_data.get('lat')
                lng = location_data.get('lng')
                if lat and lng:
                    try:
                        camera.location = Point(float(lng), float(lat), srid=4326)
                    except (ValueError, TypeError) as e:
                        print(f"Error setting camera location: {e}")
                
            try:
                camera.full_clean()
                camera.save()
                cameras_created += 1
            except Exception as e:
                continue



        nodes_created = 0

        for node_data in nodes_data:
            temp_boundary_id = node_data.get('farm_boundary_temp_id')
            farm_boundary = boundary_mapping.get(str(temp_boundary_id))

            if not farm_boundary:
                continue

            node = Node(
                project=project,
                farm_boundary=farm_boundary,
                name=node_data.get('name'),
                node_type=node_data.get('node_type'),
                dev_eui=node_data.get('dev_eui')
            )

            location = node_data.get('location')
            if location:
                node.location = Point(float(location['lng']), float(location['lat']), srid=4326)

            try:
                node.full_clean()   # ✅ مهم
                node.save()
                nodes_created += 1
            except Exception as e:
                print("NODE ERROR:", e)
            
     
            
    messages.success(request, f'Project "{project.name}" created successfully with {len(created_boundaries)} boundaries and {cameras_created} cameras, {nodes_created} nodes!')
    return redirect('project_management:project_detail', slug=project.slug)


@csrf_exempt
@require_http_methods(["POST"])
def validate_boundary_step(request):
    """AJAX endpoint to validate farm boundary data before proceeding to camera step"""
    try:
        boundaries_json = request.POST.get('farm_boundaries_data', '[]')
        boundaries_data = json.loads(boundaries_json)
                
        if not boundaries_data:
            return JsonResponse({
                'valid': False,
                'message': 'At least one farm boundary is required.'
            })
        
        # Validate each boundary
        for i, boundary in enumerate(boundaries_data):
            boundary_geom = boundary.get('boundary')
            if not boundary_geom:
                return JsonResponse({
                    'valid': False,
                    'message': f'Farm boundary {i+1} must have a drawn area.'
                })
            
            # Handle different geometry formats
            try:
                if isinstance(boundary_geom, str):
                    # If it's a string, try to parse as JSON
                    geom_data = json.loads(boundary_geom)
                else:
                    # If it's already a dict/object
                    geom_data = boundary_geom
                                
                # Check if we have a geometry object or need to extract it
                geometry_json = None
                if 'geometry' in geom_data:
                    # Full GeoJSON feature
                    geometry_json = geom_data['geometry']
                elif 'type' in geom_data and 'coordinates' in geom_data:
                    # Just the geometry part
                    geometry_json = geom_data
                else:
                    return JsonResponse({
                        'valid': False,
                        'message': f'Farm boundary {i+1} has invalid geometry structure.'
                    })
                
                # Create the geometry object
                geom = GEOSGeometry(json.dumps(geometry_json))
                
                # Convert to MultiPolygon if it's a Polygon (for consistency with model)
                if geom.geom_type == 'Polygon':
                    from django.contrib.gis.geos import MultiPolygon
                    geom = MultiPolygon(geom)
                
                # Validate the geometry
                if not geom.valid:
                    return JsonResponse({
                        'valid': False,
                        'message': f'Farm boundary {i+1} has invalid geometry.'
                    })
                
                if geom.area == 0:
                    return JsonResponse({
                        'valid': False,
                        'message': f'Farm boundary {i+1} must have a valid area.'
                    })
                                
            except Exception as e:
                return JsonResponse({
                    'valid': False,
                    'message': f'Farm boundary {i+1} has invalid geometry: {str(e)}'
                })
        
        # Check for overlaps (simplified version)
        overlaps = _check_boundary_overlaps(boundaries_data)
        if overlaps:
            return JsonResponse({
                'valid': False,
                'message': f'Farm boundaries overlap: {overlaps}'
            })
        
        return JsonResponse({'valid': True})
        
    except json.JSONDecodeError as e:
        return JsonResponse({
            'valid': False,
            'message': 'Invalid boundary data format.'
        })
    except Exception as e:
        return JsonResponse({
            'valid': False,
            'message': f'Validation error: {str(e)}'
        })


@csrf_exempt
@require_http_methods(["POST"])
def validate_camera_step(request):
    """AJAX endpoint to validate camera data before final review"""
    try:
        boundaries_json = request.POST.get('farm_boundaries_data', '[]')
        cameras_json = request.POST.get('cameras_data', '[]')
        nodes_json = request.POST.get('nodes_data', '[]')

        boundaries_data = json.loads(boundaries_json)
        cameras_data = json.loads(cameras_json)
        nodes_data = json.loads(nodes_json)

        if not cameras_data and not nodes_data:
            return JsonResponse({
                'valid': False,
                'message': 'At least one node or one camera is required.'
            })
        
        # Create boundary geometries for validation
        boundary_geoms = {}
        for boundary in boundaries_data:
            if boundary.get('temp_id') and boundary.get('boundary'):
                try:
                    boundary_geoms[boundary['temp_id']] = GEOSGeometry(boundary['boundary'])
                except Exception:
                    continue
        
        # Validate each camera
        for i, camera in enumerate(cameras_data):
            camera_type = camera.get('camera_type')
            
            # Validate required fields based on camera type
            if camera_type == 'ip':
                if not camera.get('ip_address'):
                    return JsonResponse({
                        'valid': False,
                        'message': f'Camera {i+1}: IP address is required for IP cameras.'
                    })
                if not camera.get('port'):
                    return JsonResponse({
                        'valid': False,
                        'message': f'Camera {i+1}: Port is required for IP cameras.'
                    })
            elif camera_type == 'cellular':
                if not camera.get('cellular_identifier'):
                    return JsonResponse({
                        'valid': False,
                        'message': f'Camera {i+1}: Cellular identifier is required.'
                    })
            
            # Validate camera location
            location = camera.get('location')
            boundary_temp_id = camera.get('farm_boundary_temp_id')
            
            if not location or not location.get('lat') or not location.get('lng'):
                return JsonResponse({
                    'valid': False,
                    'message': f'Camera {i+1}: Location is required.'
                })
            
            # Check if camera is within its assigned boundary
            if boundary_temp_id in boundary_geoms:
                try:
                    camera_point = Point(float(location['lng']), float(location['lat']), srid=4326)
                    boundary_geom = boundary_geoms[boundary_temp_id]
                    
                    if not boundary_geom.contains(camera_point):
                        return JsonResponse({
                            'valid': False,
                            'message': f'Camera {i+1}: Must be placed within its assigned farm boundary.'
                        })
                except Exception:
                    return JsonResponse({
                        'valid': False,
                        'message': f'Camera {i+1}: Invalid location coordinates.'
                    })
        
        return JsonResponse({'valid': True})
        
    except Exception as e:
        return JsonResponse({
            'valid': False,
            'message': f'Validation error: {str(e)}'
        })


def _check_boundary_overlaps(boundaries_data):
    """Check if any farm boundaries overlap with each other"""
    geometries = []
    
    # Parse all geometries
    for boundary in boundaries_data:
        boundary_geom = boundary.get('boundary')
        if boundary_geom:
            try:
                if isinstance(boundary_geom, str):
                    geom_data = json.loads(boundary_geom)
                else:
                    geom_data = boundary_geom
                
                # Handle different geometry structures
                geometry_json = None
                if 'geometry' in geom_data:
                    geometry_json = geom_data['geometry']
                elif 'type' in geom_data and 'coordinates' in geom_data:
                    geometry_json = geom_data
                else:
                    continue
                
                geom = GEOSGeometry(json.dumps(geometry_json))
                
                # Convert to MultiPolygon if needed for consistency
                if geom.geom_type == 'Polygon':
                    from django.contrib.gis.geos import MultiPolygon
                    geom = MultiPolygon(geom)
                
                geometries.append(geom)
            except Exception as e:
                continue
    
    # Check for overlaps
    overlapping_pairs = []
    for i in range(len(geometries)):
        for j in range(i + 1, len(geometries)):
            if geometries[i].intersects(geometries[j]):
                overlapping_pairs.append(f"Boundary {i+1} and Boundary {j+1}")
    
    return ", ".join(overlapping_pairs) if overlapping_pairs else None

@login_required
def project_list(request):
    """Display list of projects created by the logged-in user"""
    
    # Get search query if any
    search_query = request.GET.get('search', '')
    
    # Base queryset - projects created by the current user
    projects = Project.objects.filter(created_by=request.user)
    
    # Apply search filter if provided
    if search_query:
        projects = projects.filter(
            Q(name__icontains=search_query) |
            Q(location_city__icontains=search_query) |
            Q(description__icontains=search_query)
        )
    
    # Order by creation date (newest first)
    projects = projects.order_by('-created_at')
    
    # Add project statistics
    projects_with_stats = []
    for project in projects:
        project_data = {
            'project': project,
            'total_boundaries': project.get_total_farm_boundaries(),
            'total_cameras': project.get_total_cameras(),
            'total_area': project.get_total_farm_area_hectares(),
            #'total_nodes': project.node_set.count(),
            'total_nodes': project.get_total_nodes(),
        }
        projects_with_stats.append(project_data)
    
    # Pagination
    paginator = Paginator(projects_with_stats, 10)  # Show 10 projects per page
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)
    
    context = {
        'page_obj': page_obj,
        'search_query': search_query,
        'total_projects': projects.count(),
    }
    
    return render(request, 'project_management/project_list.html', context)


@login_required
def project_detail(request, slug):
    """Display detailed view of a specific project with map"""
    
    # Get the project, ensuring it belongs to the current user
    project = get_object_or_404(Project, slug=slug, created_by=request.user)
    
    # Get all farm boundaries with their geometries
    farm_boundaries = project.farm_boundaries.filter(is_active=True)
    
    # Get all cameras with their locations
    cameras = project.cameras.filter(is_active=True).select_related('farm_boundary')
    # Get all nodes
    nodes = project.nodes.filter(is_active=True).select_related('farm_boundary')

    
    # Prepare boundary data for the map
    boundaries_data = []
    for boundary in farm_boundaries:
        if boundary.boundary:
            try:
                # Convert geometry to GeoJSON for JavaScript
                boundary_data = {
                    'id': boundary.id,
                    'description': boundary.description,
                    'area_hectares': float(boundary.area_hectares) if boundary.area_hectares else 0,
                    'geometry': json.loads(boundary.boundary.geojson),
                    'created_at': boundary.created_at.isoformat()
                }
                boundaries_data.append(boundary_data)
            except Exception as e:
                continue

    fire_predictor = FireRiskPredictor()
    
    # Prepare camera data for the map
    cameras_data = []
    for camera in cameras:
        if camera.location:
            fire_risk = fire_predictor.calculate_fire_risk(
                    camera.location.y,  # latitude
                    camera.location.x   # longitude
            )
            try:
                camera_data = {
                    'id': camera.id,
                    'farm_boundary_id': camera.farm_boundary.id,
                    'camera_type': camera.camera_type,
                    'description': camera.description,
                    'latitude': camera.location.y,
                    'longitude': camera.location.x,
                    'connection_info': camera.get_connection_string(),
                    'is_within_boundary': camera.is_within_farm_boundary(),
                    'created_at': camera.created_at.isoformat(),
                    'fire_risk': fire_risk
                }
                cameras_data.append(camera_data)
            except Exception as e:
                continue
    
    #prepare nodes data for the map
    nodes_data = []
    for node in nodes:
        if node.location:
            try:
                node_data = {
                    'id': node.id,
                    'name': node.name,
                    'node_type': node.node_type,
                    'latitude': node.location.y,
                    'longitude': node.location.x,
                    'farm_boundary_id': node.farm_boundary.id,
                    'dev_eui': node.dev_eui,
                    #'is_within_boundary': True,
                    'is_within_boundary': (
                        node.location is not None and
                        node.farm_boundary.boundary is not None and
                        node.farm_boundary.boundary.contains(node.location)
                    ),
                }
                nodes_data.append(node_data)
            except Exception:
                continue

    # Calculate map center based on boundaries or cameras
    map_center = [36.8065, 10.1815]  # Default to Tunis
    if boundaries_data:
        # Use centroid of all boundaries
        try:
            combined_boundary = project.get_all_farm_boundaries_combined()
            if combined_boundary:
                centroid = combined_boundary.centroid
                map_center = [centroid.y, centroid.x]
        except Exception:
            pass
    elif cameras_data:
        # Use average of camera locations
        try:
            avg_lat = sum(cam['latitude'] for cam in cameras_data) / len(cameras_data)
            avg_lng = sum(cam['longitude'] for cam in cameras_data) / len(cameras_data)
            map_center = [avg_lat, avg_lng]
        except Exception:
            pass
    
    context = {
        'project': project,
        'farm_boundaries': farm_boundaries,
        'cameras': cameras,
        'nodes': nodes,
        'boundaries_data': json.dumps(boundaries_data),
        'cameras_data': json.dumps(cameras_data),
        'map_center': json.dumps(map_center),
        'nodes_data': json.dumps(nodes_data),
        'project_stats': {
            'total_boundaries': project.get_total_farm_boundaries(),
            'total_cameras': project.get_total_cameras(),
            'total_area': project.get_total_farm_area_hectares(),
            #'total_nodes': nodes.count(),
            'total_nodes': project.get_total_nodes(),
        },
         # ← AJOUTE CECI
        'total_crop_days': (
            project.crop.days_ini + project.crop.days_dev +
            project.crop.days_mid + project.crop.days_end
        ) if project.crop else 0,
    }
    
    return render(request, 'project_management/project_detail.html', context)


@login_required
def project_delete(request, slug):
    project = get_object_or_404(Project, slug=slug, created_by=request.user)

    if request.method == 'POST':
        project.delete()

        # ✅ إذا AJAX
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            return JsonResponse({'success': True})

        # ✅ fallback عادي
        messages.success(request, f'Project "{project.name}" deleted successfully.')
        return redirect('project_management:project_list')

    return JsonResponse({'success': False})


@login_required
def project_status_toggle(request, slug):
    """Toggle project active status via AJAX"""
    if request.method == 'POST':
        project = get_object_or_404(Project, slug=slug, created_by=request.user)
        project.is_active = not project.is_active
        project.save()
        
        return JsonResponse({
            'success': True,
            'is_active': project.is_active,
            'message': f'Project {"activated" if project.is_active else "deactivated"} successfully.'
        })
    
    return JsonResponse({'success': False, 'message': 'Invalid request method.'})


@login_required
def project_regenerate_code(request, slug):
    """Regenerate project access code via AJAX"""
    if request.method == 'POST':
        project = get_object_or_404(Project, slug=slug, created_by=request.user)
        new_code = project.regenerate_access_code()
        
        return JsonResponse({
            'success': True,
            'new_code': new_code,
            'message': 'Access code regenerated successfully.'
        })
    
    return JsonResponse({'success': False, 'message': 'Invalid request method.'})



# ✅ NOUVELLE VUE — Ajouter un device (caméra ou node) à un projet existant
@login_required
def add_device_to_project(request, slug):
    project = get_object_or_404(Project, slug=slug, created_by=request.user)

    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Invalid method.'})

    device_type = request.POST.get('device_type')  # 'camera' ou 'node'

    try:
        boundary = project.farm_boundaries.filter(is_active=True).first()
        if not boundary:
            return JsonResponse({'success': False, 'message': 'No active farm boundary found for this project.'})

        lat = float(request.POST.get('latitude'))
        lng = float(request.POST.get('longitude'))
        location = Point(lng, lat, srid=4326)

        if device_type == 'camera':
            camera_type = request.POST.get('camera_type', 'ip')
            ip_address = request.POST.get('ip_address') or None
            port_raw = request.POST.get('port')
            port = int(port_raw) if port_raw else None
            cellular_identifier = request.POST.get('cellular_identifier') or None
            description = request.POST.get('description', '')

            # Validation selon le type
            if camera_type == 'ip':
                if not ip_address or not port:
                    return JsonResponse({'success': False, 'message': 'IP address and port are required for IP cameras.'})
            elif camera_type == 'cellular':
                if not cellular_identifier:
                    return JsonResponse({'success': False, 'message': 'Cellular identifier is required for cellular cameras.'})

            camera = Camera(
                project=project,
                farm_boundary=boundary,
                camera_type=camera_type,
                description=description,
                ip_address=ip_address,
                port=port,
                cellular_identifier=cellular_identifier,
                location=location
            )
            camera.full_clean()
            camera.save()
            return JsonResponse({'success': True, 'message': f'Camera added to "{project.name}" successfully.'})

        elif device_type == 'node':
            name = request.POST.get('name', '').strip()
            node_type = request.POST.get('node_type', 'env')
            dev_eui = request.POST.get('dev_eui', '').strip()

            if not name:
                return JsonResponse({'success': False, 'message': 'Node name is required.'})
            if not dev_eui:
                return JsonResponse({'success': False, 'message': 'DevEUI is required.'})

            # Vérification doublon DevEUI
            if Node.objects.filter(dev_eui=dev_eui).exists():
                return JsonResponse({'success': False, 'message': 'This DevEUI is already used by another node.'})

            node = Node(
                project=project,
                farm_boundary=boundary,
                name=name,
                node_type=node_type,
                dev_eui=dev_eui,
                location=location
            )
            node.full_clean()
            node.save()
            return JsonResponse({'success': True, 'message': f'Node "{name}" added to "{project.name}" successfully.'})

        else:
            return JsonResponse({'success': False, 'message': 'Invalid device type. Must be "camera" or "node".'})

    except (ValueError, TypeError) as e:
        return JsonResponse({'success': False, 'message': f'Invalid coordinates or data: {str(e)}'})
    except Exception as e:
        return JsonResponse({'success': False, 'message': f'Error saving device: {str(e)}'})


# ✅ NOUVELLE VUE — Retourner les boundaries GeoJSON d'un projet (pour la mini-carte du modal)
#@login_required
#def get_project_boundaries(request, slug):
    #project = get_object_or_404(Project, slug=slug, created_by=request.user)
    #boundaries = project.farm_boundaries.filter(is_active=True)

    #data = []
    #for b in boundaries:
        #if b.boundary:
            #try:
                #data.append({
                    #'id': b.id,
                    #'geometry': json.loads(b.boundary.geojson),
                    #'area_hectares': float(b.area_hectares) if b.area_hectares else 0
                #})
            #except Exception:
                #continue

    #return JsonResponse({'boundaries': data})

@login_required
def get_project_boundaries(request, slug):
    project = get_object_or_404(Project, slug=slug, created_by=request.user)
    boundaries = project.farm_boundaries.filter(is_active=True)

    boundaries_data = []
    for b in boundaries:
        if b.boundary:
            try:
                boundaries_data.append({
                    'id': b.id,
                    'geometry': json.loads(b.boundary.geojson),
                    'area_hectares': float(b.area_hectares) if b.area_hectares else 0
                })
            except Exception:
                continue

    # ✅ Cameras existantes
    cameras_data = []
    for camera in project.cameras.filter(is_active=True):
        if camera.location:
            try:
                cameras_data.append({
                    'id': camera.id,
                    'latitude': camera.location.y,
                    'longitude': camera.location.x,
                    'camera_type': camera.camera_type,
                    'description': camera.description or '',
                    'connection': camera.get_connection_string() or '',
                })
            except Exception:
                continue

    # ✅ Nodes existants
    nodes_data = []
    for node in project.nodes.filter(is_active=True):
        if node.location:
            try:
                nodes_data.append({
                    'id': node.id,
                    'latitude': node.location.y,
                    'longitude': node.location.x,
                    'name': node.name,
                    'node_type': node.node_type,
                    'dev_eui': node.dev_eui,
                })
            except Exception:
                continue

    return JsonResponse({
        'boundaries': boundaries_data,
        'cameras': cameras_data,
        'nodes': nodes_data,
    })







@login_required
def client_project_list(request):
    """Redirige le client directement vers son unique projet"""
    
    project = Project.objects.filter(
        user_roles__user=request.user,
        is_active=True
    ).first()

    if project:
        return redirect('project_management:project_detail_client', slug=project.slug)
    
    # Aucun projet — rediriger vers dashboard
    return redirect('dashboard:dashboard')



@login_required
def project_detail_client(request, slug):
    """Vue détail projet pour le client — sans access code, avec données nodes temps réel"""
    
    project = get_object_or_404(
        Project,
        slug=slug,
        user_roles__user=request.user,
        is_active=True
    )

    farm_boundaries = project.farm_boundaries.filter(is_active=True)
    cameras = project.cameras.filter(is_active=True).select_related('farm_boundary')
    nodes   = project.nodes.filter(is_active=True).select_related('farm_boundary')

    boundaries_data = []
    for boundary in farm_boundaries:
        if boundary.boundary:
            try:
                boundaries_data.append({
                    'id':            boundary.id,
                    'description':   boundary.description,
                    'area_hectares': float(boundary.area_hectares) if boundary.area_hectares else 0,
                    'geometry':      json.loads(boundary.boundary.geojson),
                    'created_at':    boundary.created_at.isoformat()
                })
            except Exception:
                continue

    cameras_data = []
    for camera in cameras:
        if camera.location:
            try:
                cameras_data.append({
                    'id':                 camera.id,
                    'farm_boundary_id':   camera.farm_boundary.id,
                    'camera_type':        camera.camera_type,
                    'description':        camera.description,
                    'latitude':           camera.location.y,
                    'longitude':          camera.location.x,
                    'is_within_boundary': camera.is_within_farm_boundary(),
                    'created_at':         camera.created_at.isoformat(),
                })
            except Exception:
                continue

    nodes_data = []
    for node in nodes:
        if node.location:
            try:
                nodes_data.append({
                    'id':                 node.id,
                    'name':               node.name,
                    'node_type':          node.node_type,
                    'latitude':           node.location.y,
                    'longitude':          node.location.x,
                    'farm_boundary_id':   node.farm_boundary.id,
                    'dev_eui':            node.dev_eui,
                    'is_within_boundary': (
                        node.location is not None and
                        node.farm_boundary.boundary is not None and
                        node.farm_boundary.boundary.contains(node.location)
                    ),
                })
            except Exception:
                continue

    map_center = [36.8065, 10.1815]
    if boundaries_data:
        try:
            combined = project.get_all_farm_boundaries_combined()
            if combined:
                map_center = [combined.centroid.y, combined.centroid.x]
        except Exception:
            pass
        # ── Dernière recommandation d'irrigation ──────────────────
    latest_irrigation = IrrigationLog.objects.filter(
        project=project
    ).order_by('-computed_at').first()
    # ─────────────────────────────────────────────────────────
    context = {
        'project':         project,
        'farm_boundaries': farm_boundaries,
        'cameras':         cameras,
        'nodes':           nodes,
        'boundaries_data': json.dumps(boundaries_data),
        'cameras_data':    json.dumps(cameras_data),
        'map_center':      json.dumps(map_center),
        'nodes_data':      json.dumps(nodes_data),
        'project_stats': {
            'total_boundaries': project.get_total_farm_boundaries(),
            'total_cameras':    project.get_total_cameras(),
            'total_area':       project.get_total_farm_area_hectares(),
            'total_nodes':      project.get_total_nodes(),
        },
        'total_crop_days': (
            project.crop.days_ini + project.crop.days_dev +
            project.crop.days_mid + project.crop.days_end
        ) if project.crop else 0,
        'latest_irrigation': latest_irrigation,
    }
    return render(request, 'project_management/project_detail_client.html', context)















@api_view(['POST'])
@permission_classes([AllowAny])
def camera_heartbeat(request):
    timeout_threshold = timezone.now() - timedelta(seconds=300)
    expired_cameras = Camera.objects.filter(heartbeat_check=True).filter(
        models.Q(last_heartbeat__lt=timeout_threshold) |
        models.Q(last_heartbeat__isnull=True)
    )
    expired_count = expired_cameras.update(heartbeat_check=False)

    connection_string = request.data.get('connection_string')
    heartbeat_check = request.data.get('heartbeat_check')

    if not connection_string:
        return Response({'success': False, 'message': 'connection_string is required'}, status=status.HTTP_400_BAD_REQUEST)

    if heartbeat_check is None:
        return Response({'success': False, 'message': 'heartbeat_check is required'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        camera = None
        if ':' in connection_string:
            ip_address, port = connection_string.split(':', 1)
            camera = Camera.objects.get(camera_type='ip', ip_address=ip_address, port=int(port))
        else:
            camera = Camera.objects.get(camera_type='cellular', cellular_identifier=connection_string)

        if heartbeat_check:
            camera.heartbeat_check = True
            camera.last_heartbeat = timezone.now()
            camera.save(update_fields=['heartbeat_check', 'last_heartbeat'])

        return Response({
            'success': True,
            'message': 'Camera heartbeat received',
            'camera_id': camera.id,
            'connection_string': connection_string,
            'heartbeat_check': camera.heartbeat_check,
            'last_heartbeat': camera.last_heartbeat,
            'expired_cameras_count': expired_count
        }, status=status.HTTP_200_OK)

    except Camera.DoesNotExist:
        return Response({'success': False, 'message': f'Camera with connection string "{connection_string}" not found'}, status=status.HTTP_404_NOT_FOUND)
    except ValueError as e:
        return Response({'success': False, 'message': f'Invalid connection string format: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        return Response({'success': False, 'message': 'Internal server error'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
