"""Test-only fixture modules for configuration_service tests.

Marked as an explicit package so ``_walk_class_for_pvs`` can resolve
``tests.fixtures.role_classes.<Name>`` the same way it would resolve
a real ``ophyd`` device class at runtime.
"""
