"""Cached battery snapshot and signal handling, with no device I/O allowed."""
import importlib.util
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

LIB = Path(__file__).resolve().parents[1] / 'src/resources/lib'
identity = lambda *a, **k: lambda f: f
stubs = {name: types.ModuleType(name) for name in (
    'dbussy', 'ravel', 'dbus_utils', 'xbmc', 'xbmcgui', 'dbus_obex',
    'bluetooth_reconnect', 'hostname', 'log', 'modules', 'oe', 'oeWindows')}
stubs['dbussy'].DBusError = Exception
stubs['ravel'].INTERFACE = types.SimpleNamespace(SERVER=1)
for name in ('interface', 'method', 'signal'):
    setattr(stubs['ravel'], name, identity)
stubs['dbus_utils'].Agent = object
stubs['dbus_utils'].call_method = Mock(side_effect=AssertionError('unexpected D-Bus call'))
stubs['dbus_obex'].Listener = object
stubs['dbus_obex'].Agent = object
stubs['modules'].Module = object
stubs['log'].log_function = identity


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


with patch.dict(sys.modules, stubs):
    BUS = load('dbus_bluez', LIB / 'dbus_bluez.py')
    with patch.dict(sys.modules, {'dbus_bluez': BUS}):
        UI = load('settings_bluetooth', LIB / 'modules/bluetooth.py')


class BatteryTests(unittest.TestCase):
    def test_snapshot_uses_one_existing_object_query(self):
        objects = {
            '/a': {BUS.INTERFACE_DEVICE: {'Connected': True},
                   BUS.INTERFACE_BATTERY: {'Percentage': 0}},
            '/b': {BUS.INTERFACE_DEVICE: {'Connected': False},
                   BUS.INTERFACE_BATTERY: {'Percentage': 80}},
            '/c': {BUS.INTERFACE_DEVICE: {'Connected': True}},
        }
        with patch.object(BUS, 'get_managed_objects', return_value=objects) as query:
            devices = BUS.find_devices()
        query.assert_called_once_with()
        self.assertEqual([d['BatteryLevel'] for d in devices.values()], ['0%', '', ''])
        self.assertNotIn('BatteryLevel', objects['/a'][BUS.INTERFACE_DEVICE])

    def test_unknown_and_invalid_are_not_zero(self):
        for value in (None, -1, 101, True, '50', 50.0):
            self.assertEqual(BUS.battery_level(value), '')
        self.assertEqual(BUS.battery_level(100), '100%')

    def test_signals_clear_stale_values_without_device_reads(self):
        props = {'Connected': '1', 'BatteryLevel': '96%'}
        item = types.SimpleNamespace(getProperty=lambda k: props.get(k, ''),
                                     setProperty=lambda k, v: props.__setitem__(k, v))
        parent = types.SimpleNamespace(visible=True, listItems={'/a': item},
                                      discovery_thread=True, discover_devices=Mock())
        listener = object.__new__(UI.Bluez_Listener)
        listener.parent = parent
        with patch.object(UI, 'bluetooth_menu_active', return_value=True), \
             patch.object(UI.oe, 'gui_lock', threading.RLock(), create=True), \
             patch.object(UI.oe, 'xbmcm', types.SimpleNamespace(abortRequested=lambda: True), create=True):
            listener.on_properties_changed(BUS.INTERFACE_BATTERY, {'Percentage': 0}, [], '/a')
            self.assertEqual(props['BatteryLevel'], '0%')
            listener.on_properties_changed(BUS.INTERFACE_BATTERY, {}, ['Percentage'], '/a')
            self.assertEqual(props['BatteryLevel'], '')
            listener.on_properties_changed(BUS.INTERFACE_BATTERY, {'Percentage': 74}, [], '/a')
            listener.on_interfaces_removed('/a', [BUS.INTERFACE_BATTERY])
            self.assertEqual(props['BatteryLevel'], '')
            props['BatteryLevel'] = '74%'
            listener.on_properties_changed(BUS.INTERFACE_DEVICE, {'Connected': False}, [], '/a')
            self.assertEqual(props['BatteryLevel'], '')
            listener.on_properties_changed(BUS.INTERFACE_BATTERY, {'Percentage': 74}, [], '/a')
            self.assertEqual(props['BatteryLevel'], '')
        parent.discover_devices.assert_not_called()
        stubs['dbus_utils'].call_method.assert_not_called()


if __name__ == '__main__':
    unittest.main()
