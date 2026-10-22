import json
from unittest.mock import patch

import pytest

from pipeline.core import device_identity as identity


@pytest.mark.parametrize('system,output', [
    ('Darwin', '"IOPlatformUUID" = "12345678-1234-5678-9abc-123456789abc"'),
    ('Windows', '12345678-1234-5678-9abc-123456789abc'),
    ('Linux', '12345678-1234-5678-9abc-123456789abc'),
])
def test_native_identity_is_stable_hashed_and_changes_with_hardware(system, output):
    identity.machine_id.cache_clear()
    try:
        with patch.object(identity.platform, 'system', return_value=system), \
                patch.object(identity, '_command', return_value=output), \
                patch.object(identity.Path, 'read_text', return_value=output):
            first = identity.machine_id()
            assert len(first) == 64 and '12345678' not in first
            assert identity.machine_id() == first
            identity.machine_id.cache_clear()
            with patch.object(identity, '_command', return_value=output.replace('12345678', '87654321')), \
                    patch.object(identity.Path, 'read_text', return_value=output.replace('12345678', '87654321')):
                assert identity.machine_id() != first
    finally:
        identity.machine_id.cache_clear()


@pytest.mark.parametrize('output', ['', 'not-a-uuid', '00000000-0000-0000-0000-000000000000', 'ffffffff-ffff-ffff-ffff-ffffffffffff'])
def test_unavailable_hardware_does_not_fall_back_to_random_identity(output):
    identity.machine_id.cache_clear()
    try:
        with patch.object(identity.platform, 'system', return_value='Windows'), patch.object(identity, '_command', return_value=output):
            with pytest.raises(ValueError, match='DEVICE_ID_UNAVAILABLE'):
                identity.machine_id()
    finally:
        identity.machine_id.cache_clear()


@pytest.mark.parametrize('system,model,chassis,battery,expected', [
    ('Darwin', 'MacBook Pro', '', None, 'laptop'),
    ('Darwin', 'Mac mini', '', object(), 'pc'),
    ('Windows', '', '10', None, 'laptop'),
    ('Windows', '', '3', object(), 'pc'),
    ('Linux', '', '9', None, 'laptop'),
    ('Linux', '', '6', object(), 'pc'),
    ('Windows', '', '2', None, 'pc'),
    ('Linux', '', '2', object(), 'laptop'),
])
def test_desktop_type_uses_native_model_or_chassis_before_battery(system, model, chassis, battery, expected):
    output = json.dumps({'SPHardwareDataType': [{'machine_name': model}]}) if system == 'Darwin' else chassis
    identity.desktop_device_type.cache_clear()
    try:
        with patch.object(identity.platform, 'system', return_value=system), \
                patch.object(identity, '_command', return_value=output), \
                patch.object(identity.Path, 'read_text', return_value=chassis), \
                patch('psutil.sensors_battery', return_value=battery):
            assert identity.desktop_device_type() == expected
    finally:
        identity.desktop_device_type.cache_clear()


@pytest.mark.parametrize('battery,expected', [(None, 'pc'), (object(), 'laptop')])
def test_desktop_type_falls_back_when_native_probe_fails(battery, expected):
    identity.desktop_device_type.cache_clear()
    try:
        with patch.object(identity.platform, 'system', return_value='Windows'), \
                patch.object(identity, '_command', side_effect=OSError('unavailable')), \
                patch('psutil.sensors_battery', return_value=battery):
            assert identity.desktop_device_type() == expected
    finally:
        identity.desktop_device_type.cache_clear()
