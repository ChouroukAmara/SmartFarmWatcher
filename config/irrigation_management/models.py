from django.db import models
from project_management.models import Project
from iot_management.models import Node


class IrrigationLog(models.Model):
    ADVICE_CHOICES = [
        ('none',   'Aucune'),
        ('low',    'Légère'),
        ('medium', 'Modérée'),
        ('high',   'Urgente'),
    ]

    project       = models.ForeignKey(
        Project, on_delete=models.CASCADE,
        related_name='irrigation_logs'
    )
    node          = models.ForeignKey(
        Node, on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='irrigation_logs'
    )
    computed_at   = models.DateTimeField(auto_now_add=True, db_index=True)

    # Données capteur BME688
    temperature   = models.FloatField()
    humidity      = models.FloatField()
    pressure      = models.FloatField(default=1013.0)

    # Résultats FAO-56
    kc            = models.FloatField()
    et0_mm        = models.FloatField()
    etc_mm        = models.FloatField()
    rainfall_mm   = models.FloatField(default=0.0)
    net_need_mm   = models.FloatField()
    gross_need_mm = models.FloatField()
    advice_level  = models.CharField(max_length=10, choices=ADVICE_CHOICES)
    message       = models.TextField()

    # ✅ AJOUT — salinité et sol
    ecw_dSm       = models.FloatField(default=0.0, help_text="Salinité eau capteur (dS/m)")
    ece_dSm       = models.FloatField(default=2.5, help_text="Seuil FAO culture (dS/m)")
    FL            = models.FloatField(default=0.0, help_text="Fraction lessivage")
    BL_mm         = models.FloatField(default=0.0, help_text="Besoin lessivage (mm)")
    salinity_risk = models.CharField(max_length=10, default='none',
                                     help_text="none/low/medium/high")
    pu_mm         = models.FloatField(default=0.0, help_text="Précipitation utile (mm)")
    eg            = models.FloatField(default=0.81, help_text="Efficience globale")
    b_totaux_mm   = models.FloatField(default=0.0, help_text="Besoins totaux (mm)")
    soil_moisture = models.FloatField(null=True, blank=True, help_text="Humidité sol capteur (%)")
    b_totaux_ml = models.FloatField(null=True, blank=True, verbose_name="Btotaux ML (mm)")

    class Meta:
        ordering = ['-computed_at']
        indexes  = [
            models.Index(fields=['project', '-computed_at']),
            models.Index(fields=['node',    '-computed_at']),
        ]

    def __str__(self):
        return (
            f"{self.project.name} | "
            f"{self.computed_at:%Y-%m-%d %H:%M} | "
            f"ETc={self.etc_mm}mm | {self.advice_level}"
        )