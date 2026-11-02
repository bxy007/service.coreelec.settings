"""Update startup policy tests with the production module and fake external I/O.

Run: python3 -m unittest discover -s tests -v
"""
import importlib.util
import itertools
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch


SOURCE = Path(__file__).resolve().parents[1] / 'src/resources/lib/modules/updates.py'
xbmc = types.ModuleType('xbmc')
xbmc.Player = lambda: types.SimpleNamespace(isPlaying=lambda: False)
xbmcgui = types.ModuleType('xbmcgui')
xbmcgui.Dialog = lambda: types.SimpleNamespace(select=lambda *args: -1)
with patch.dict(sys.modules, {'xbmc': xbmc, 'xbmcgui': xbmcgui,
                             'oeWindows': types.ModuleType('oeWindows')}):
    spec = importlib.util.spec_from_file_location('settings_updates', SOURCE)
    MODULE = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(MODULE)


CATALOG = {'CoreELEC-21.3': {
    'url': 'https://updates.invalid/',
    'prettyname_regex': r'21\.3',
    'project': {'Amlogic-ng.arm': {'releases': {
        '1': {'file': {'name': 'CoreELEC-Amlogic-ng.arm-21.3.tar'}}}}}}}


class FakeOE:
    LOGDEBUG, LOGINFO, LOGERROR = 0, 1, 3
    RPI_CPU_VER = '0'
    ARCHITECTURE = 'Amlogic-ng.arm'
    SYSTEMID = 'test'
    DISTRIBUTION = 'CoreELEC'
    VERSION = '21.3'
    VERSION_ID = '21.3'
    LAST_STABLE = None
    BUILDER_VERSION = None
    BUILDER_NAME = None

    def __init__(self, saved):
        self.saved = dict(saved)
        self.requests = []
        self.logs = []
        self.notifications = []
        self.dictModules = {}
        self.winOeMain = types.SimpleNamespace(build_menu=Mock())

    def dbg_log(self, *args):
        self.logs.append(args)

    def read_setting(self, group, key):
        assert group == 'updates'
        return self.saved.get(key)

    def write_setting(self, group, key, value):
        assert group == 'updates'
        self.saved[key] = value

    def load_url(self, url):
        self.requests.append(url)
        return json.dumps(CATALOG if url.endswith('releases.php') else {'data': {}})

    def notify(self, *args):
        self.notifications.append(args)

    def _(self, number):
        return str(number)

    @staticmethod
    def url_quote(value):
        return value


class CapturedThread:
    def __init__(self, oe):
        self.oe = oe
        self.wait_evt = types.SimpleNamespace(set=Mock())
        self.start = Mock()
        self.stop = Mock()


class UpdatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.worker_class = MODULE.updateThread
        self.patcher = patch.object(MODULE, 'updateThread', CapturedThread)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def create(self, saved=None):
        oe = FakeOE(saved or {})
        update = MODULE.updates(oe)
        update.LOCAL_UPDATE_DIR = self.directory.name
        update.UPDATE_DOWNLOAD_URL = 'https://%s/%s/%s'
        update.UPDATE_REQUEST_URL = 'https://updates.invalid/updates.php'
        update.get_hardware_flags = lambda: 'test'
        oe.dictModules['updates'] = update
        return update, oe

    def cycle(self, update, oe):
        worker = self.worker_class(oe)
        waits = []

        def wait(seconds):
            waits.append(seconds)
            worker.stopped = True

        worker.wait_evt = types.SimpleNamespace(wait=wait, clear=lambda: None)
        worker.run()
        return waits

    @staticmethod
    def item(name, value):
        values = {'category': 'update', 'entry': name, 'value': value}
        return types.SimpleNamespace(getProperty=values.__getitem__)

    def test_default_startup_and_background_do_not_request_updates(self):
        update, oe = self.create()
        update.start_service()
        settings = update.struct['update']['settings']
        self.assertEqual(settings['AutoUpdate']['value'], 'manual')
        self.assertEqual(settings['AutoUpdate']['values'][0], 'manual')
        for key in ('SubmitStats', 'UpdateNotify', 'ShowCustomChannels'):
            self.assertEqual(settings[key]['value'], '0')
        self.assertIsNone(update.update_json)
        self.assertEqual(self.cycle(update, oe), [21600])
        self.assertEqual(oe.requests, [])
        self.assertFalse(any(log[-1] == oe.LOGERROR for log in oe.logs))

    def test_all_saved_startup_combinations(self):
        for auto, notify, stats, custom in itertools.product(
                ('manual', 'auto'), ('0', '1'), ('0', '1'), ('0', '1')):
            with self.subTest(auto=auto, notify=notify, stats=stats, custom=custom):
                saved = {'AutoUpdate': auto, 'UpdateNotify': notify,
                         'SubmitStats': stats, 'ShowCustomChannels': custom}
                update, oe = self.create(saved)
                update.start_service()
                enabled = auto == 'auto' or notify == '1'
                self.assertEqual(len(oe.requests), int(enabled))
                self.assertTrue(all(url.endswith('releases.php') for url in oe.requests))
                oe.requests.clear()
                self.cycle(update, oe)
                self.assertEqual(len(oe.requests), int(enabled))
                if enabled:
                    self.assertIn('updates.php?', oe.requests[0])
                    self.assertEqual('&nostats' in oe.requests[0], stats == '0')
                for key, value in saved.items():
                    self.assertEqual(update.struct['update']['settings'][key]['value'], value)

    def test_manual_page_fetches_channels_on_demand_and_reuses_result(self):
        update, oe = self.create({'Channel': 'CoreELEC-21.3'})
        update.start_service()
        self.assertEqual(oe.requests, [])
        update.load_menu(None)
        self.assertEqual(len(oe.requests), 1)
        self.assertEqual(update.struct['update']['settings']['Channel']['values'], ['CoreELEC-21.3'])
        self.assertEqual(update.struct['update']['settings']['Build']['values'], ['21.3'])
        update.load_menu(None)
        self.assertEqual(len(oe.requests), 1)
        self.assertEqual(oe.winOeMain.build_menu.call_count, 2)
        self.cycle(update, oe)
        self.assertEqual(len(oe.requests), 1)

    def test_explicit_manual_update_refreshes_even_with_both_switches_off(self):
        update, oe = self.create({'Channel': 'CoreELEC-21.3'})
        update.start_service()
        update.do_manual_update()
        self.assertEqual(len(oe.requests), 1)
        self.assertEqual(update.struct['update']['settings']['Build']['values'], ['21.3'])
        self.assertFalse(hasattr(update, 'update_in_progress'))

    def test_notification_toggle_wakes_worker_and_disabling_stops_requests(self):
        update, oe = self.create()
        update.start_service()
        self.assertEqual(update.struct['update']['settings']['UpdateNotify']['action'], 'set_auto_update')
        update.set_auto_update(self.item('UpdateNotify', '1'))
        update.update_thread.wait_evt.set.assert_called_once()
        self.cycle(update, oe)
        self.assertEqual(len(oe.requests), 1)
        update.set_auto_update(self.item('UpdateNotify', '0'))
        self.cycle(update, oe)
        self.assertEqual(len(oe.requests), 1)
        self.assertEqual(oe.saved['UpdateNotify'], '0')

    def test_auto_toggle_wakes_worker_and_preserves_notification_off(self):
        update, oe = self.create()
        update.start_service()
        update.set_auto_update(self.item('AutoUpdate', 'auto'))
        self.cycle(update, oe)
        self.assertEqual(len(oe.requests), 1)
        self.assertEqual(update.struct['update']['settings']['UpdateNotify']['value'], '0')
        update.set_auto_update(self.item('AutoUpdate', 'manual'))
        self.cycle(update, oe)
        self.assertEqual(len(oe.requests), 1)

    def test_explicit_forced_check_keeps_no_stats_flag(self):
        update, oe = self.create()
        update.start_service()
        update.check_updates_v2(force=True)
        self.assertEqual(len(oe.requests), 1)
        self.assertIn('&nostats', oe.requests[0])

    def test_pending_update_respects_notification_switch(self):
        for notify in ('0', '1'):
            with self.subTest(notify=notify):
                update, oe = self.create({'UpdateNotify': notify})
                update.start_service()
                oe.requests.clear()
                update.update_in_progress = True
                self.assertEqual(self.cycle(update, oe), [3600])
                self.assertEqual(len(oe.notifications), int(notify))
                self.assertEqual(oe.requests, [])

    def test_failed_manual_catalog_is_retried_when_page_is_reopened(self):
        update, oe = self.create()
        update.start_service()
        with patch.object(update, 'build_json', side_effect=[None, CATALOG]) as fetch:
            update.load_menu(None)
            self.assertIsNone(update.update_json)
            update.load_menu(None)
            self.assertEqual(update.update_json, CATALOG)
            self.assertEqual(fetch.call_count, 2)

    def test_explicit_custom_channel_refresh_is_not_an_automatic_check(self):
        update, oe = self.create()
        update.start_service()
        update.set_custom_channel(self.item('ShowCustomChannels', '1'))
        self.assertEqual(len(oe.requests), 1)
        self.assertFalse(update.automatic_checks_enabled())


if __name__ == '__main__':
    unittest.main(verbosity=2)
