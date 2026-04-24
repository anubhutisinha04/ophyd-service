"""
Configuration Service (SVC-004)

Centralized device/PV registry management.
Implements ProvidesDeviceRegistry protocol.
"""

from configuration_service.main import app, create_app
from configuration_service.models import (
    DeviceMetadata,
    PVMetadata,
    DeviceLabel,
    DeviceRegistry,
)
from configuration_service.loader import MockProfileLoader
from configuration_service.config import Settings

__all__ = [
    "app",
    "create_app",
    "DeviceMetadata",
    "PVMetadata",
    "DeviceLabel",
    "DeviceRegistry",
    "MockProfileLoader",
    "Settings",
]

__version__ = "1.0.0"
