"""Exercise real reconnect policy/scheduling with a controlled D-Bus boundary."""
import asyncio
import copy
import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src/resources/lib/bluetooth_reconnect.py'
spec = importlib.util.spec_from_file_location('bluetooth_reconnect', SOURCE)
MODULE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MODULE)


def device(uuids=(), **props):
    return {MODULE.DEVICE: dict(Paired=True, Bonded=True, Trusted=True, Connected=False,
                                AddressType='public', Blocked=False,
                                UUIDs=list(uuids), Adapter='/adapter', **props)}


class PolicyTests(unittest.TestCase):
    def test_native_hog_and_vendor_services(self):
        item = device([MODULE.HOG, '12345678-1234-1234-1234-123456789abc'])
        self.assertIsNone(MODULE.reconnect_reason(item))
        item[MODULE.DEVICE]['Trusted'] = False
        self.assertEqual(MODULE.reconnect_reason(item), 'paired device')

    def test_audio_and_mixed_profiles_remain_host_candidates(self):
        for uuid in MODULE.AUDIO:
            self.assertEqual(MODULE.reconnect_reason(device([MODULE.HOG, uuid])), 'audio')
        for uuid in (MODULE.HID, '00001115' + MODULE.UUID_SUFFIX):
            self.assertIsNotNone(MODULE.reconnect_reason(device([MODULE.HOG, uuid])))
        self.assertIsNotNone(MODULE.reconnect_reason(device([MODULE.HOG], Class=0x2540)))

    def test_private_or_unbonded_hog_keeps_host_fallback(self):
        item = device([MODULE.HOG])
        item[MODULE.DEVICE]['AddressType'] = 'random'
        for address, native in [('C1:00:00:00:00:01', True),
                                ('41:00:00:00:00:01', False),
                                ('01:00:00:00:00:01', False), ('invalid', False)]:
            item[MODULE.DEVICE]['Address'] = address
            self.assertEqual(MODULE.reconnect_reason(item) is None, native)
        item[MODULE.DEVICE]['AddressType'] = 'public'
        item[MODULE.DEVICE]['Bonded'] = False
        self.assertIsNotNone(MODULE.reconnect_reason(item))

    def test_classic_hid_modes_and_unknown_fallback(self):
        for mode in ('host', 'any', 'device', 'none', None):
            item = device([MODULE.HID, '00001200' + MODULE.UUID_SUFFIX])
            item[MODULE.INPUT] = {'ReconnectMode': mode}
            self.assertEqual(MODULE.reconnect_reason(item) is not None,
                             mode not in ('device', 'none'))
        item[MODULE.INPUT]['ReconnectMode'] = 'device'
        item[MODULE.DEVICE]['UUIDs'].append('00001115' + MODULE.UUID_SUFFIX)
        self.assertIsNotNone(MODULE.reconnect_reason(item))

    def test_unknown_capabilities_preserve_paired_policy(self):
        self.assertEqual(MODULE.reconnect_reason(device()), 'paired device')
        item = device([MODULE.HOG.upper()])
        self.assertIsNone(MODULE.reconnect_reason(item))

    def test_connected_unpaired_and_blocked_are_skipped(self):
        for field, value in [('Connected', True), ('Paired', False), ('Blocked', True)]:
            item = device(MODULE.AUDIO)
            item[MODULE.DEVICE][field] = value
            self.assertIsNone(MODULE.reconnect_reason(item))


class FakeBus:
    def __init__(self, devices):
        self.objects = {'/adapter': {MODULE.ADAPTER: {'Powered': True}}, **devices}
        self.calls = []
        self.started = asyncio.Queue()
        self.gates = {}
        self.errors = {}
        self.active = 0
        self.maximum = 0
        self.reads = 0
        self.before_read = lambda: None

    async def __call__(self, bus, path, interface, method):
        if method == 'GetManagedObjects':
            self.reads += 1
            self.before_read()
            return copy.deepcopy(self.objects)
        self.calls.append((path, method))
        assert method == 'Connect'
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.started.put_nowait(path)
        try:
            if path in self.gates:
                await self.gates[path].wait()
            if path in self.errors:
                raise self.errors[path]
            self.objects[path][MODULE.DEVICE]['Connected'] = True
        finally:
            self.active -= 1


class SchedulerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages = []
        self.stopped = False

    def run_pass(self, bus, **kwargs):
        return MODULE.reconnect(bus, self.messages.append, lambda: self.stopped, **kwargs)

    async def test_offline_device_does_not_hold_online_device(self):
        bus = FakeBus({'/offline': device(), '/online': device(), '/third': device()})
        bus.gates['/offline'] = asyncio.Event()
        task = asyncio.create_task(self.run_pass(bus))
        self.assertEqual(await asyncio.wait_for(bus.started.get(), 1), '/offline')
        self.assertEqual(await asyncio.wait_for(bus.started.get(), 1), '/online')
        self.assertEqual(await asyncio.wait_for(bus.started.get(), 1), '/third')
        self.assertFalse(task.done())
        self.assertTrue(bus.objects['/online'][MODULE.DEVICE]['Connected'])
        self.assertLessEqual(bus.maximum, 2)
        bus.gates['/offline'].set()
        await task

    async def test_audio_first_and_hog_omitted(self):
        bus = FakeBus({'/unknown': device(), '/hog': device([MODULE.HOG]),
                       '/audio': device(MODULE.AUDIO)})
        await self.run_pass(bus)
        self.assertEqual(bus.calls, [('/audio', 'Connect'), ('/unknown', 'Connect')])

    async def test_bounded_concurrency(self):
        bus = FakeBus({f'/{i}': device() for i in range(5)})
        bus.gates = {path: asyncio.Event() for path in bus.objects if path != '/adapter'}
        task = asyncio.create_task(self.run_pass(bus))
        await asyncio.wait_for(bus.started.get(), 1)
        await asyncio.wait_for(bus.started.get(), 1)
        self.assertEqual(len(bus.calls), 2)
        for gate in bus.gates.values():
            gate.set()
        await task
        self.assertEqual(len(bus.calls), 5)
        self.assertEqual(bus.maximum, 2)

    async def test_fresh_state_handles_connected_blocked_removed_and_power_off(self):
        for change in ('connected', 'blocked', 'removed', 'power'):
            bus = FakeBus({'/one': device()})
            def mutate():
                if bus.reads != 2:
                    return
                if change == 'removed':
                    del bus.objects['/one']
                elif change == 'power':
                    bus.objects['/adapter'][MODULE.ADAPTER]['Powered'] = False
                else:
                    bus.objects['/one'][MODULE.DEVICE][change.title()] = True
            bus.before_read = mutate
            await self.run_pass(bus)
            self.assertEqual(bus.calls, [], change)

    async def test_multiple_adapters_use_each_devices_power_state(self):
        bus = FakeBus({'/one': device(), '/two': device()})
        bus.objects['/adapter'][MODULE.ADAPTER]['Powered'] = False
        bus.objects['/other'] = {MODULE.ADAPTER: {'Powered': True}}
        bus.objects['/two'][MODULE.DEVICE]['Adapter'] = '/other'
        await self.run_pass(bus)
        self.assertEqual(bus.calls, [('/two', 'Connect')])

    async def test_cancellation_releases_pending_without_disconnect_or_new_calls(self):
        bus = FakeBus({f'/{i}': device() for i in range(4)})
        bus.gates = {path: asyncio.Event() for path in bus.objects if path != '/adapter'}
        task = asyncio.create_task(self.run_pass(bus))
        await asyncio.wait_for(bus.started.get(), 1)
        await asyncio.wait_for(bus.started.get(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(bus.active, 0)
        self.assertEqual(len(bus.calls), 2)

    async def test_disabled_policy_stops_queued_work(self):
        bus = FakeBus({'/one': device(), '/two': device()})
        bus.gates['/one'] = asyncio.Event()
        task = asyncio.create_task(self.run_pass(bus, concurrency=1))
        await asyncio.wait_for(bus.started.get(), 1)
        self.stopped = True
        bus.gates['/one'].set()
        await task
        self.assertEqual(bus.calls, [('/one', 'Connect')])

    async def test_timeout_and_inprogress_are_logged_without_retry(self):
        bus = FakeBus({'/timeout': device(), '/race': device(), '/ok': device()})
        bus.gates['/timeout'] = asyncio.Event()
        bus.errors['/race'] = RuntimeError('org.bluez.Error.InProgress')
        await self.run_pass(bus, timeout=0.02)
        self.assertEqual(len(bus.calls), 3)
        self.assertEqual(bus.active, 0)
        self.assertTrue(any('timed out /timeout' in message for message in self.messages))
        self.assertTrue(any('InProgress' in message for message in self.messages))
        self.assertTrue(bus.objects['/ok'][MODULE.DEVICE]['Connected'])

    async def test_bus_failure_is_not_silenced(self):
        async def failed(*args):
            raise RuntimeError('org.bluez unavailable')
        with self.assertRaisesRegex(RuntimeError, 'unavailable'):
            await self.run_pass(failed)


if __name__ == '__main__':
    unittest.main(verbosity=2)
