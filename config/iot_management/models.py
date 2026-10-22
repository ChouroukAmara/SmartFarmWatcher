from django.contrib.gis.db import models
from project_management.models import Project, FarmBoundary

class Node(models.Model):

    NODE_TYPES = [
        ('env', 'Environmental Node'),  # temperature, humidity, gaz...
        ('water', 'Water Node'),       # salinity
    ]

    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='nodes')
    farm_boundary = models.ForeignKey(FarmBoundary, on_delete=models.CASCADE, related_name='nodes')

    name = models.CharField(max_length=100)
    node_type = models.CharField(max_length=20, choices=NODE_TYPES)

    # tres important
    dev_eui = models.CharField(max_length=50, unique=True)

    location = models.PointField(srid=4326, null=True, blank=True)

    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.name} ({self.node_type})"


# Create your models here.

class NodeData(models.Model):
    node = models.ForeignKey(Node, on_delete=models.CASCADE, related_name="data")

    temperature = models.FloatField(null=True, blank=True)
    humidity = models.FloatField(null=True, blank=True)
    pressure = models.FloatField(null=True, blank=True)
    gas = models.FloatField(null=True, blank=True)
    soil_moisture = models.FloatField(null=True, blank=True)
    salinity = models.FloatField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.node.name} - {self.created_at}"

