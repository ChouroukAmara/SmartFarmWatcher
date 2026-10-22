from celery import shared_task
from django.utils import timezone
from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from irrigation_management.predictor import predict_recommended_irrigation as ml_predict, map_stage


def push_notification_ws(notification):
    """
    Sends a notification to the recipient's WebSocket group in real time,
    in addition to it being saved in the database. Matches the format
    expected by NotificationConsumer.notification_message().
    """
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    try:
        async_to_sync(channel_layer.group_send)(
            f"notifications_{notification.user_id}",
            {
                "type": "notification_message",
                "notification": {
                    "id": notification.id,
                    "title": notification.title,
                    "message": notification.message,
                    "notification_type": notification.notification_type,
                    "is_read": notification.is_read,
                    "created_at": notification.created_at.isoformat(),
                },
            },
        )
    except Exception as e:
        print(f"[WS] Failed to push notification: {e}")


def get_project_recipients(project):
    """
    Returns the list of users who should receive notifications for this
    project: the supervisor (project.created_by) + all active clients
    (via UserProjectRole), without duplicates.
    """
    from project_management.models import UserProjectRole

    recipients = []
    if project.created_by:
        recipients.append(project.created_by)

    client_roles = UserProjectRole.objects.filter(
        project=project, role='client', is_active=True
    ).select_related('user')
    recipients.extend([role.user for role in client_roles])

    # Remove duplicates while keeping order
    return list({user.id: user for user in recipients}.values())


def check_irrigation_alerts(log, project):
    """
    Creates the 3 essential irrigation notifications (time to irrigate,
    critical soil, critical salinity) and sends them to the project
    supervisor AND all clients who have access to this project.
    """
    from notification_management.models import Notification

    recipients = get_project_recipients(project)
    if not recipients:
        return 0

    alerts = []  # (title, message)

    # ── 1. Time to irrigate ──
    if log.b_totaux_ml is not None and log.b_totaux_ml > 0:
        crop_name = project.crop.crop_name if project.crop else "unspecified crop"
        alerts.append((
            "💧 Time to irrigate",
            f"{log.b_totaux_ml} mm of water recommended today for "
            f"{project.name} ({crop_name})."
        ))

    # ── 2. Critical soil moisture ──
    if log.soil_moisture is not None and log.soil_moisture < 10:
        alerts.append((
            "🚨 Critical soil — urgent irrigation needed",
            f"Soil moisture at {log.soil_moisture}% for {project.name}, "
            f"below the critical threshold (10%)."
        ))

    # ── 3. Critical salinity ──
    if log.salinity_risk == 'high':
        alerts.append((
            "🚨 Critical salinity",
            f"ECw={log.ecw_dSm} dS/m far exceeds the tolerance threshold "
            f"({log.ece_dSm} dS/m) for {project.name}."
        ))

    total_created = 0
    for title, message in alerts:
        for recipient in recipients:
            notification = Notification.objects.create(
                user=recipient,
                notification_type='irrigation',
                title=title,
                message=message,
            )
            push_notification_ws(notification)
            total_created += 1

    return total_created


@shared_task
def check_offline_sensors():
    """
    4th essential notification. Checks all active nodes and notifies the
    supervisor + clients if no data has been received for 24h+.
    Scheduled to run every hour (see celery.py beat_schedule).
    """
    from datetime import timedelta
    from iot_management.models import Node, NodeData
    from notification_management.models import Notification

    threshold = timezone.now() - timedelta(hours=24)
    notified = 0

    for node in Node.objects.filter(is_active=True).select_related('project__created_by'):
        last_data = NodeData.objects.filter(node=node).order_by('-id').first()
        is_silent = (not last_data) or (last_data.created_at < threshold)

        if not is_silent:
            continue

        project = node.project
        recipients = get_project_recipients(project)

        for recipient in recipients:
            # Avoid spamming: only one "offline sensor" notification per 24h per recipient
            already_notified = Notification.objects.filter(
                user=recipient,
                notification_type='irrigation',
                title__icontains=node.name,
                created_at__gte=timezone.now() - timedelta(hours=24),
            ).exists()

            if already_notified:
                continue

            notification = Notification.objects.create(
                user=recipient,
                notification_type='irrigation',
                title=f"⚠️ Sensor {node.name} offline",
                message=(
                    f"No data received for over 24h from sensor "
                    f"{node.name} ({node.dev_eui}) on {project.name}."
                ),
            )
            push_notification_ws(notification)
            notified += 1

    print(f"[SensorCheck] {notified} offline-sensor alert(s) created")
    return {"status": "ok", "notified": notified}


@shared_task
def check_critical_salinity(project_id, ecw):
    """
    Immediate check triggered on EVERY water sensor reading (not once a day
    like compute_daily_irrigation_recommendations). Only creates a
    notification if salinity is at/above the crop's tolerance threshold,
    and avoids spamming by only sending once per hour per recipient.
    """
    from datetime import timedelta
    from django.utils import timezone
    from project_management.models import Project
    from notification_management.models import Notification

    try:
        project = Project.objects.select_related('crop').get(id=project_id)
    except Project.DoesNotExist:
        return {"status": "skipped", "reason": "project not found"}

    if not project.crop:
        return {"status": "skipped", "reason": "no crop configured"}

    ece = project.crop.ece_threshold
    if not ece or ece <= 0:
        return {"status": "skipped", "reason": "invalid ece_threshold"}

    ratio = ecw / ece
    if ratio < 1.0:
        return {"status": "ok", "critical": False, "ratio": round(ratio, 3)}

    recipients = get_project_recipients(project)
    if not recipients:
        return {"status": "skipped", "reason": "no recipients"}

    one_hour_ago = timezone.now() - timedelta(hours=1)
    created = 0

    for recipient in recipients:
        already_sent = Notification.objects.filter(
            user=recipient,
            notification_type='irrigation',
            title__icontains='Critical salinity',
            created_at__gte=one_hour_ago,
        ).exists()

        if already_sent:
            continue

        notification = Notification.objects.create(
            user=recipient,
            notification_type='irrigation',
            title="🚨 Critical salinity",
            message=(
                f"ECw={ecw} dS/m exceeds the tolerance threshold "
                f"({ece} dS/m) for {project.name}. Immediate attention recommended."
            ),
        )
        push_notification_ws(notification)
        created += 1

    print(f"[SalinityCheck] ECw={ecw} ratio={ratio:.2f} -> {created} notification(s) created for {project.name}")
    return {"status": "ok", "critical": True, "ratio": round(ratio, 3), "notified": created}


@shared_task
def compute_daily_irrigation_recommendations():
    """
    Scheduled ONCE PER DAY (Celery Beat, see celery.py).

    Computes today's irrigation recommendation for every active project,
    using the latest sensor reading available. Sensor data itself keeps
    being logged at every message by mqtt_listener.py, independently of
    this daily task.
    """
    from project_management.models import Project
    from iot_management.models import Node, NodeData

    projects = Project.objects.filter(
        is_active=True,
        crop__isnull=False,
        planting_date__isnull=False,
    )

    launched = 0
    skipped = 0

    for project in projects:
        env_node = Node.objects.filter(
            project=project, node_type="env", is_active=True
        ).first()

        if not env_node:
            skipped += 1
            continue

        last_data = NodeData.objects.filter(node=env_node).order_by('-id').first()

        if not last_data or last_data.temperature is None or last_data.humidity is None:
            print(f"[DailyIrrigation] ⚠️ No recent data for {project.name} ({env_node.dev_eui})")
            skipped += 1
            continue

        compute_fao56_task.delay(
            dev_eui=env_node.dev_eui,
            temperature=float(last_data.temperature),
            humidity=float(last_data.humidity),
            pressure=float(last_data.pressure) if last_data.pressure is not None else 1013.0,
        )
        launched += 1

    print(f"[DailyIrrigation] ✅ {launched} calculation(s) launched, {skipped} project(s) skipped")
    return {"status": "ok", "launched": launched, "skipped": skipped}


@shared_task(bind=True, max_retries=3)
def compute_fao56_task(self, dev_eui, temperature, humidity, pressure=1013.0):
    try:
        from iot_management.models import Node, NodeData
        from irrigation_management.models import IrrigationLog
        from utils.fao56 import FAO56Calculator

        env_node = Node.objects.select_related(
            "project__crop"
        ).get(dev_eui=dev_eui, node_type="env", is_active=True)

        project = env_node.project

        if not project.crop or not project.planting_date or not env_node.location:
            return {"status": "skipped", "reason": "missing crop/planting_date/location"}

        # ── Safety guard: prevents a duplicate computation the same day ──
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        already_done_today = IrrigationLog.objects.filter(
            project=project, computed_at__gte=today_start
        ).exists()
        if already_done_today:
            return {"status": "skipped", "reason": "already computed today"}

        lat   = env_node.location.y
        lon   = env_node.location.x
        kc    = project.current_kc
        stage = project.current_stage

        soil_moisture_pct = None
        last_env = NodeData.objects.filter(node=env_node).order_by('-id').first()
        if last_env and last_env.soil_moisture is not None:
            soil_moisture_pct = float(last_env.soil_moisture)

        ecw = 0.0
        water_node = Node.objects.filter(
            project=project, node_type="water", is_active=True
        ).first()

        if water_node:
            last_water = NodeData.objects.filter(node=water_node).order_by('-id').first()
            if last_water and last_water.salinity is not None:
                ecw = float(last_water.salinity)
                print(f"[FAO56] 💧 ECw={ecw} dS/m from water node")
        else:
            print("[FAO56] ⚠️ No water node found")

        ece             = getattr(project.crop, 'ece_threshold', 2.5)
        soil_type       = getattr(project, 'soil_type', 'loamy')
        irrigation_type = getattr(project, 'irrigation_type', 'drip')

        calc    = FAO56Calculator(latitude=lat, longitude=lon)
        et0_res = calc.compute_et0(T_mean=temperature, RH_mean=humidity, P_hpa=pressure)
        et0     = et0_res["et0_mm"]
        etc     = calc.compute_etc(et0, kc)

        advice = calc.compute_irrigation_advice(
            etc_mm=etc,
            et0_mm=et0,
            kc=kc,
            stage=stage,
            ecw=ecw,
            ece=ece,
            soil_moisture_pct=soil_moisture_pct,
            soil_type=soil_type,
            irrigation_type=irrigation_type,
        )

        try:
            crop_type_ml  = project.crop.crop_name
            crop_stage_ml = map_stage(stage)

            ml_result = ml_predict(
                et0_mm            = et0,
                kc                = kc,
                soil_moisture_pct = soil_moisture_pct if soil_moisture_pct is not None else 50.0,
                soil_type         = soil_type,
                ecw               = ecw,
                ece               = ece,
                crop_type         = crop_type_ml,
                crop_stage        = crop_stage_ml,
                rainfall_mm       = advice["rainfall_mm"],
            )
            recommended_irrigation_ml = ml_result["Recommended_Irrigation_mm"]
            print(
                f"[ML] Recommended={recommended_irrigation_ml} mm | "
                f"FAO={advice['b_totaux_mm']} mm | "
                f"Ks(ref)={ml_result['Ks_reference_only']} | "
                f"Alert={ml_result['Alert']}"
            )
        except Exception as e:
            print(f"[ML] Prediction error: {e}")
            recommended_irrigation_ml = None

        log = IrrigationLog.objects.create(
            project=project,
            node=env_node,
            temperature=temperature,
            humidity=humidity,
            pressure=pressure,
            kc=kc,
            et0_mm=et0,
            etc_mm=etc,
            rainfall_mm=advice["rainfall_mm"],
            net_need_mm=advice["net_need_mm"],
            gross_need_mm=advice["gross_need_mm"],
            advice_level=advice["advice_level"],
            message=advice["message"],
            ecw_dSm=advice["ecw_dSm"],
            ece_dSm=advice["ece_dSm"],
            FL=advice["FL"],
            BL_mm=advice["BL_mm"],
            salinity_risk=advice["salinity_risk"],
            pu_mm=advice["pu_mm"],
            eg=advice["EG"],
            b_totaux_mm=advice["b_totaux_mm"],
            b_totaux_ml=recommended_irrigation_ml,
            soil_moisture=soil_moisture_pct,
        )

        # ── Essential notifications (supervisor + clients) ──
        nb_alerts = check_irrigation_alerts(log, project)
        print(f"[Notifications] {nb_alerts} alert(s) created for {project.name}")

        print(
            f"[FAO56] ✅ Log #{log.id} | "
            f"ETc={etc}mm | ECw={ecw} | "
            f"soil={soil_moisture_pct}% | "
            f"{advice['message']}"
        )
        return {"status": "ok", "log_id": log.id, **advice}

    except Node.DoesNotExist:
        return {"status": "skipped", "reason": f"env node {dev_eui} not found"}
    except Exception as exc:
        print(f"[FAO56] ❌ Error: {exc}")
        raise self.retry(exc=exc, countdown=30)