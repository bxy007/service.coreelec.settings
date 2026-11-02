# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Team CoreELEC

"""One startup reconnect pass; BlueZ owns subsequent link recovery."""

import asyncio
import time

DEVICE = 'org.bluez.Device1'
ADAPTER = 'org.bluez.Adapter1'
INPUT = 'org.bluez.Input1'
OBJECT_MANAGER = 'org.freedesktop.DBus.ObjectManager'
UUID_SUFFIX = '-0000-1000-8000-00805f9b34fb'
HOG = '00001812' + UUID_SUFFIX
HID = '00001124' + UUID_SUFFIX
AUDIO = {'0000' + service + UUID_SUFFIX for service in
         ('1108', '110a', '110b', '1112', '111e', '111f')}


def has_identity_address(props):
    if props.get('AddressType') == 'public':
        return True
    if props.get('AddressType') == 'random':
        try:
            return int(props['Address'].split(':')[0], 16) & 0xc0 == 0xc0
        except (KeyError, ValueError):
            pass
    return False


def reconnect_reason(interfaces):
    """Return a host-connect reason, or None when no startup call is needed.

    Preserve paired-device policy for unknown or mixed profiles. Only omit
    HID calls when the cached capabilities identify a native reconnect path.
    """
    props = interfaces.get(DEVICE, {})
    if not props.get('Paired') or props.get('Connected') or props.get('Blocked'):
        return None
    uuids = {uuid.lower() for uuid in props.get('UUIDs', [])}
    if uuids & AUDIO:
        return 'audio'
    # Standard BR/EDR service classes, except the informational PnP record.
    classic = {uuid for uuid in uuids if uuid.endswith(UUID_SUFFIX)
               and '00001100' <= uuid[:8] < '00001400'
               and uuid != '00001200' + UUID_SUFFIX}
    if (HOG in uuids and not classic and not props.get('Class')
            and props.get('Trusted') and props.get('Bonded')
            and has_identity_address(props)):
        # input-hog auto_connect is registered when BlueZ loads bonded HOG.
        return None
    if HID in uuids and classic == {HID} and HOG not in uuids:
        mode = interfaces.get(INPUT, {}).get('ReconnectMode')
        if mode in ('none', 'device'):
            return None
        return 'host HID'  # Includes unknown mode: retain the old fallback.
    return 'paired device'


async def reconnect(call, report, stopped, *, concurrency=2, timeout=30):
    """Bound connection concurrency and recheck state immediately before use.

    call is the settings service's async D-Bus transport. Cancellation releases
    local waits only; Disconnect would also tear down a newly established or
    manually initiated link, so it is deliberately not used for cleanup.
    """
    async def objects():
        return await asyncio.wait_for(
            call('org.bluez', '/', OBJECT_MANAGER, 'GetManagedObjects'), 10)

    if stopped():
        return
    snapshot = await objects()
    candidates = [(path, reconnect_reason(interfaces))
                  for path, interfaces in snapshot.items() if DEVICE in interfaces]
    candidates = [(path, reason) for path, reason in candidates if reason]
    candidates.sort(key=lambda candidate: candidate[1] != 'audio')
    report(f'Startup reconnect: {len(candidates)} host candidates, '
           f'{sum(DEVICE in item for item in snapshot.values())} known devices')
    limit = asyncio.Semaphore(concurrency)

    async def connect(path):
        async with limit:
            if stopped():
                return
            started = time.monotonic()
            try:
                current = await objects()
                interfaces = current.get(path, {})
                reason = reconnect_reason(interfaces)
                adapter = interfaces.get(DEVICE, {}).get('Adapter')
                if not reason or not current.get(adapter, {}).get(ADAPTER, {}).get('Powered'):
                    return
                if stopped():
                    return
                report(f'Startup reconnect: requesting {path} ({reason})')
                await asyncio.wait_for(call('org.bluez', path, DEVICE, 'Connect'), timeout)
                report(f'Startup reconnect: completed {path} in {time.monotonic() - started:.3f}s')
            except asyncio.TimeoutError:
                report(f'Startup reconnect: timed out {path} after {time.monotonic() - started:.3f}s')
            except Exception as error:
                # InProgress/AlreadyConnected can race native or manual calls.
                # Keep the outcome visible and never launch a duplicate retry.
                report(f'Startup reconnect: {path}: {error}')

    tasks = [asyncio.create_task(connect(path)) for path, _ in candidates]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
