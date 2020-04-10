#!/usr/bin/env python

import asyncio
import enum
import logging
import signal
from collections import defaultdict

import evdev
import pyudev

REMAPPED_PREFIX = '[remapped]'


class AlreadyRemappedError(Exception):
    pass


class VirtualModifierState(enum.Enum):
    RELEASED = enum.auto()
    PRESSED_SILENT = enum.auto()
    PRESSED_NOISY = enum.auto()


class VirtualModifier():
    def __init__(self, keyboard, code):
        self._keyboard = keyboard
        self.code = code
        self._pressed_keys = []
        self._state = VirtualModifierState.RELEASED
        self._previous_state = self._state
        self._callbacks = []

    @property
    def active(self):
        return self._state != VirtualModifierState.RELEASED

    @property
    def noisy(self):
        return self._state == VirtualModifierState.PRESSED_NOISY

    @property
    def repeating_key(self):
        if self._state == VirtualModifierState.PRESSED_NOISY:
            self._pressed_keys[0]
        else:
            None

    def add_callback(self, callback):
        self._callbacks.append(callback)

    def press(self, key):
        if key not in self._pressed_keys:
            self._pressed_keys.append(key)
        if len(self._pressed_keys) >= 2:
            self.trigger_noisy()

    def release(self, key):
        self._pressed_keys.remove(key)
        if not self._pressed_keys:
            self._state = VirtualModifierState.RELEASED

    def trigger_silent(self):
        if self._pressed_keys and self._state == VirtualModifierState.RELEASED:
            self._state = VirtualModifierState.PRESSED_SILENT
            self._trigger()

    def trigger_noisy(self):
        if not self.code:
            self.trigger_silent()
            return
        if self._pressed_keys and self._state == VirtualModifierState.RELEASED:
            self._state = VirtualModifierState.PRESSED_NOISY
            self._trigger()
        elif self._state == VirtualModifierState.PRESSED_SILENT:
            self._state = VirtualModifierState.PRESSED_NOISY

    def _trigger(self):
        for callback in self._callbacks:
            callback()

    def flush_state_change(self, sync_event):
        noisy = self._state == VirtualModifierState.PRESSED_NOISY
        previous_noisy = self._previous_state == VirtualModifierState.PRESSED_NOISY
        if noisy and not previous_noisy:
            self._keyboard.write_event(
                evdev.InputEvent(sync_event.sec, sync_event.usec,
                                 evdev.ecodes.EV_KEY, self.code, 1))
        elif not noisy and previous_noisy:
            self._keyboard.write_event(
                evdev.InputEvent(sync_event.sec, sync_event.usec,
                                 evdev.ecodes.EV_KEY, self.code, 0))
        self._previous_state = self._state


class Key:
    def __init__(self, keyboard):
        self._keyboard = keyboard

    def process_event(self, event):
        return True

    def flush_state_change(self, sync_event):
        pass

    def flush_event(self, event):
        self._keyboard.write_event(event)


class TriggerKey:
    def __init__(self, key, *callbacks):
        self._key = key
        self._callbacks = callbacks

    def _trigger(self):
        for callback in self._callbacks:
            callback()

    def process_event(self, event):
        if event.value == 1:
            self._trigger()
        return self._key.process_event(event)

    def flush_state_change(self, sync_event):
        self._key.flush_state_change(sync_event)

    def flush_event(self, event):
        self._key.flush_event(event)

    def __getattr__(self, name):
        return self._key.__getattribute__(name)


class VirtualModifierKey(Key):
    def __init__(self, keyboard, virtual_modifier, **kwargs):
        super().__init__(keyboard, **kwargs)
        self._virtual_modifier = virtual_modifier

    def process_event(self, event):
        super().process_event(event)
        if event.value == 0:
            self._virtual_modifier.release(self)
            return False
        elif event.value == 1:
            self._virtual_modifier.press(self)
            return False
        else:
            return self._virtual_modifier.noisy

    def flush_event(self, event):
        if event.value == 2 and self._virtual_modifier.repeating_key is self:
            event.code = self._virtual_modifier.code
            self._keyboard.write_event(event)


class ModifierKey(VirtualModifierKey):
    def __init__(self, keyboard, virtual_modifier, silent=False):
        super().__init__(keyboard, virtual_modifier)
        self._silent = silent

    def process_event(self, event):
        ret = super().process_event(event)
        if event.value == 1:
            if self._silent:
                self._virtual_modifier.trigger_silent()
            else:
                self._virtual_modifier.trigger_noisy()
        return ret


class OnReleaseState(enum.Enum):
    RELEASED = enum.auto()
    PRESSED_SINGLE = enum.auto()
    PRESSED_SILENT = enum.auto()


class OnReleaseKey(Key):
    def __init__(self, keyboard, extra_code, silence_modifier=None, callbacks=None, **kwargs):
        super().__init__(keyboard, **kwargs)
        self._extra_code = extra_code
        self._silence_modifier = silence_modifier
        if silence_modifier:
            silence_modifier.add_callback(self.silence_release)
        self._callbacks = callbacks
        self._state = OnReleaseState.RELEASED
        self._previous_state = self._state

    def silence_release(self):
        if self._state == OnReleaseState.PRESSED_SINGLE:
            self._state = OnReleaseState.PRESSED_SILENT

    def process_event(self, event):
        if event.value == 0:
            self._state = OnReleaseState.RELEASED
            if self._previous_state == OnReleaseState.PRESSED_SINGLE:
                if self._callbacks:
                    for callback in self._callbacks:
                        callback()
        if event.value == 1:
            if self._silence_modifier and self._silence_modifier.active:
                self._state = OnReleaseState.PRESSED_SILENT
            else:
                self._state = OnReleaseState.PRESSED_SINGLE
        return True

    def flush_state_change(self, sync_event):
        if self._previous_state == OnReleaseState.PRESSED_SINGLE and \
           self._state == OnReleaseState.RELEASED:
            event = evdev.InputEvent(sync_event.sec, sync_event.usec,
                                     evdev.ecodes.EV_KEY, self._extra_code, 1)
            self._keyboard.write_event(event)
            event.value = 0
            self._keyboard.write_event(event)
        self._previous_state = self._state


class OnQuickReleaseKey(OnReleaseKey):
    def __init__(self, keyboard, extra_code, delay=None, **kwargs):
        super().__init__(keyboard, extra_code, **kwargs)

    def process_event(self, event):
        ret = super().process_event(event)
        if event.value == 2:
            self.silence_release()
        return ret


class SingleOrModifierKey(VirtualModifierKey, OnReleaseKey):
    def __init__(self, keyboard, single_code, virtual_modifier, **kwargs):
        super().__init__(keyboard, extra_code=single_code,
                         virtual_modifier=virtual_modifier,
                         silence_modifier=virtual_modifier, **kwargs)


class RemapKey(Key):
    def __init__(self, keyboard, code):
        super().__init__(keyboard)
        self._code = code

    def flush_event(self, event):
        event.code = self._code
        super().flush_event(event)


class ModKey():
    def __init__(self, normal_key, virtual_modifier, modified_key):
        self._normal_key = normal_key
        self._virtual_modifier = virtual_modifier
        self._modified_key = modified_key
        self._active_key = None
        self._releasing = False
        self._wants_release_event = False

    def process_event(self, event):
        if self._releasing:
            return False
        if event.value == 0:
            self._releasing = True
            self._wants_release_event = self._active_key.process_event(event)
            return True
        elif event.value == 1:
            if not self._active_key:
                self._virtual_modifier.trigger_silent()
                if self._virtual_modifier.active:
                    self._active_key = self._modified_key
                else:
                    self._active_key = self._normal_key
        if self._active_key:
            return self._active_key.process_event(event)
        else:
            return False

    def flush_state_change(self, sync_event):
        if self._active_key:
            self._active_key.flush_state_change(sync_event)

    def flush_event(self, event):
        if self._releasing and event.value == 0:
            if self._active_key and self._wants_release_event:
                self._active_key.flush_event(event)
                self._wants_release_event = False
            self._releasing = False
            self._active_key = None
        elif self._active_key:
            self._active_key.flush_event(event)

    def __getattr__(self, name):
        return self._normal_key.__getattribute__(name)


class SecondTouchKey(OnQuickReleaseKey):
    def __init__(self, keyboard, first_code, second_code, force_modifier=None, **kwargs):
        super().__init__(keyboard, extra_code=first_code,
                         silence_modifier=force_modifier, **kwargs)
        self._second_code = second_code
        self._force_modifier = force_modifier
        self._repeating = False

    def process_event(self, event):
        if event.value == 1 and self._force_modifier:
            self._force_modifier.trigger_silent()
            if self._force_modifier.active:
                self._repeating = True
        super().process_event(event)
        return event.value == 2 and self._repeating

    def flush_state_change(self, sync_event):
        if self._state == OnReleaseState.PRESSED_SILENT and \
           self._previous_state != OnReleaseState.PRESSED_SILENT:
            event = evdev.InputEvent(sync_event.sec, sync_event.usec,
                                     evdev.ecodes.EV_KEY, self._second_code, 1)
            self._keyboard.write_event(event)
            if not self._repeating:
                event.value = 0
                self._keyboard.write_event(event)
        if self._state != OnReleaseState.PRESSED_SILENT and \
           self._previous_state == OnReleaseState.PRESSED_SILENT and \
           self._repeating:
            event = evdev.InputEvent(sync_event.sec, sync_event.usec,
                                     evdev.ecodes.EV_KEY, self._second_code, 0)
            self._keyboard.write_event(event)
            self._repeating = False
        super().flush_state_change(sync_event)


class Keyboard:
    def __init__(self, path):
        self._logger = logging.getLogger(type(self).__qualname__)
        self._device = evdev.InputDevice(path)
        name = self._device.name
        if name.startswith(REMAPPED_PREFIX):
            self._device.close()
            raise AlreadyRemappedError()
        capabilities = defaultdict(set)
        for ev_type, ev_codes in self._device.capabilities().items():
            if ev_type != evdev.ecodes.EV_SYN and ev_type != evdev.ecodes.EV_FF:
                capabilities[ev_type].update(ev_codes)
        capabilities[evdev.ecodes.EV_KEY].update({
            evdev.ecodes.KEY_ESC,
            evdev.ecodes.KEY_COMPOSE,
            evdev.ecodes.KEY_F13,
            evdev.ecodes.KEY_F14,
            evdev.ecodes.KEY_F15,
            evdev.ecodes.KEY_F16,
            evdev.ecodes.KEY_F17,
            evdev.ecodes.KEY_F18,
            evdev.ecodes.KEY_F18,
            evdev.ecodes.KEY_F20,
            evdev.ecodes.KEY_F21,
            evdev.ecodes.KEY_F22,
            evdev.ecodes.KEY_F23,
            evdev.ecodes.KEY_F24,
            evdev.ecodes.KEY_FILE,
            evdev.ecodes.KEY_HOMEPAGE,
            evdev.ecodes.KEY_CALC,
            evdev.ecodes.KEY_CONFIG,
            evdev.ecodes.KEY_PREVIOUSSONG,
            evdev.ecodes.KEY_NEXTSONG,
            evdev.ecodes.KEY_PLAYPAUSE,
            evdev.ecodes.KEY_STOP,
            evdev.ecodes.KEY_MUTE,
            evdev.ecodes.KEY_VOLUMEDOWN,
            evdev.ecodes.KEY_VOLUMEUP,
            evdev.ecodes.KEY_PROG1,
            evdev.ecodes.KEY_PROG2,
        })
        try:
            self._uinput = evdev.UInput(
                events=capabilities, name=f'{REMAPPED_PREFIX} {name}')
        except:
            self._device.close()
            raise
        self._logger = self._logger.getChild(name)
        self._logger.info('Initialized at %s', path)
        right_alt = VirtualModifier(self, evdev.ecodes.KEY_RIGHTALT)
        fn = VirtualModifier(self, None)
        self._virtual_modifiers = [right_alt, fn]
        basic_key = Key(self)
        left_meta = TriggerKey(
            OnQuickReleaseKey(self, evdev.ecodes.KEY_D, silence_modifier=right_alt),
            right_alt.trigger_silent)
        right_meta = TriggerKey(
            OnQuickReleaseKey(self, evdev.ecodes.KEY_F, silence_modifier=right_alt),
            right_alt.trigger_silent)
        self._special_keys = {
            evdev.ecodes.KEY_LEFTSHIFT: basic_key,
            evdev.ecodes.KEY_RIGHTSHIFT: basic_key,
            evdev.ecodes.KEY_LEFTCTRL: basic_key,
            evdev.ecodes.KEY_RIGHTCTRL: basic_key,
            evdev.ecodes.KEY_CAPSLOCK: SingleOrModifierKey(
                self, evdev.ecodes.KEY_ESC, right_alt,
                callbacks=[left_meta.silence_release, right_meta.silence_release]),
            evdev.ecodes.KEY_LEFTALT: basic_key,
            evdev.ecodes.KEY_RIGHTALT: SingleOrModifierKey(
                self, evdev.ecodes.KEY_COMPOSE, right_alt,
                callbacks=[left_meta.silence_release, right_meta.silence_release]),
            evdev.ecodes.KEY_LEFTMETA: left_meta,
            evdev.ecodes.KEY_RIGHTMETA: right_meta,
            evdev.ecodes.KEY_MENU: ModifierKey(self, fn),
            evdev.ecodes.KEY_COMPOSE: ModifierKey(self, fn),
            evdev.ecodes.KEY_F1: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F13, evdev.ecodes.KEY_F1, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_FILE)),
            evdev.ecodes.KEY_F2: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F14, evdev.ecodes.KEY_F2, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_HOMEPAGE)),
            evdev.ecodes.KEY_F3: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F15, evdev.ecodes.KEY_F3, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_CALC)),
            evdev.ecodes.KEY_F4: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F16, evdev.ecodes.KEY_F4, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_CONFIG)),
            evdev.ecodes.KEY_F5: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F17, evdev.ecodes.KEY_F5, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_PREVIOUSSONG)),
            evdev.ecodes.KEY_F6: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F18, evdev.ecodes.KEY_F6, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_NEXTSONG)),
            evdev.ecodes.KEY_F7: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F19, evdev.ecodes.KEY_F7, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_PLAYPAUSE)),
            evdev.ecodes.KEY_F8: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F20, evdev.ecodes.KEY_F8, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_STOP)),
            evdev.ecodes.KEY_F9: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F21, evdev.ecodes.KEY_F9, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_MUTE)),
            evdev.ecodes.KEY_F10: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F22, evdev.ecodes.KEY_F10, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_VOLUMEDOWN)),
            evdev.ecodes.KEY_F11: ModKey(
                SecondTouchKey(self, evdev.ecodes.KEY_F23, evdev.ecodes.KEY_F11, right_alt),
                fn, RemapKey(self, evdev.ecodes.KEY_VOLUMEUP)),
            evdev.ecodes.KEY_F12: SecondTouchKey(
                self, evdev.ecodes.KEY_F24, evdev.ecodes.KEY_F12, right_alt),
            evdev.ecodes.KEY_SYSRQ: ModKey(
                RemapKey(self, evdev.ecodes.KEY_PROG1), right_alt, basic_key),
            evdev.ecodes.KEY_SCROLLLOCK: ModKey(
                RemapKey(self, evdev.ecodes.KEY_PROG1), right_alt, basic_key),
            evdev.ecodes.KEY_PAUSE: ModKey(
                RemapKey(self, evdev.ecodes.KEY_PROG2), right_alt, basic_key),
            evdev.ecodes.KEY_BREAK: ModKey(
                RemapKey(self, evdev.ecodes.KEY_PROG2), right_alt, basic_key),
        }
        self._default_key = TriggerKey(
            basic_key, left_meta.silence_release, right_meta.silence_release,
            right_alt.trigger_noisy)
        self._backlog = []
        self._await_later = []

    async def process_events(self, interrupted):
        try:
            with self._device.grab_context():
                async for event in self._device.async_read_loop():
                    self._process_event(event)
        except asyncio.CancelledError:
            pass
        except OSError:
            return
        except:
            interrupted.set()
            raise
        finally:
            try:
                self._uinput.close()
            except asyncio.CancelledError | OSError:
                pass
            try:
                self._device.close()
            except asyncio.CancelledError | OSError:
                pass
            self._logger.info('Closed')

    def _process_event(self, event):
        if event.type == evdev.ecodes.EV_SYN:
            for virtual_modifier in self._virtual_modifiers:
                virtual_modifier.flush_state_change(event)
            for key in self._special_keys.values():
                key.flush_state_change(event)
            for buffered_event in self._backlog:
                buffered_event.sec = event.sec
                buffered_event.usec = event.usec
                self.get_key(buffered_event.code).flush_event(buffered_event)
            self.write_event(event)
            self._backlog.clear()
        elif event.type == evdev.ecodes.EV_KEY:
            if self.get_key(event.code).process_event(event):
                self._backlog.append(event)
        else:
            self.write_event(event)

    def get_key(self, code):
        return self._special_keys.get(code, self._default_key)

    def write_event(self, event):
        self._logger.debug('out: %r', event)
        self._uinput.write_event(event)


class KeyboardRemapper:
    def __init__(self, interrupted):
        self._logger = logging.getLogger(type(self).__qualname__)
        self._interrupted = interrupted
        self._devices = {}
        self._context = pyudev.Context()
        self._monitor = pyudev.Monitor.from_netlink(self._context)
        self._monitor.filter_by('input')

    async def start(self):
        self._monitor.start()
        keyboards = self._context.list_devices(
            subsystem='input').match_property('ID_INPUT_KEYBOARD', True)
        for device in keyboards:
            self._add_device(device)
        loop = asyncio.get_running_loop()
        loop.add_reader(self._monitor.fileno(), self._poll_udev_event)

    async def stop(self):
        for task in self._devices.values():
            task.cancel()
        await asyncio.gather(*self._devices.values())

    def _poll_udev_event(self):
        device = self._monitor.poll()
        action = device.action
        if action == 'add' or action == 'online':
            self._add_device(device)
        elif action == 'remove' or action == 'offline':
            self._remove_device(device)
        elif action != 'change':
            self._logger.warn(
                f'Unknown action {action} from {device.device_path}')

    def _add_device(self, device):
        device_path = device.device_path
        if device_path in self._devices:
            return
        device_node = device.device_node
        if device_node is None or not evdev.util.is_device(device_node):
            return
        if self._is_keyboard(device):
            try:
                keyboard = Keyboard(device_node)
            except AlreadyRemappedError:
                return
            except OSError:
                self._logger.exception(
                    f'Cannot initialize {device_path} at {device_node}')
                return
            task = asyncio.create_task(keyboard.process_events(self._interrupted),
                                       name=f'remap {device_path}')
            self._devices[device_path] = task

    def _is_keyboard(self, device):
        try:
           if not device.properties.asbool('ID_INPUT_KEYBOARD'):
               return False
        except KeyError:
           return False
        except UnicodeDecodeError | ValueError:
           self._logger.exception(
               f'{device.device_path} has malformed ID_INPUT_KEYBOARD property')
           return False
        try:
            if device.properties.asbool('ID_INPUT_MOUSE'):
                return False
        except KeyError:
            pass
        except UnicodeDecodeError | ValueError:
           self._logger.exception(
               f'{device.device_path} has malformed ID_INPUT_MOUSE property')
           return False
        return True

    def _remove_device(self, device):
        task = self._devices.pop(device.device_path, None)
        if task is not None:
            task.cancel()


async def _timeout(timeout, interrupted):
    try:
        await asyncio.sleep(timeout)
        interrupted.set()
    except asyncio.CancelledError:
        pass


async def main(timeout):
    interrupted = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, interrupted.set)
    if timeout:
        timeout_task = asyncio.create_task(_timeout(timeout, interrupted))
    else:
        timeout_task = None
    keyboard_remapper = KeyboardRemapper(interrupted)
    try:
        await keyboard_remapper.start()
        await interrupted.wait()
    finally:
        loop.remove_signal_handler(signal.SIGINT)
        if timeout_task:
            timeout_task.cancel()
            await timeout_task
        await keyboard_remapper.stop()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Remap keyboard input.')
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='verbose output')
    parser.add_argument('-d', '--debug', action='store_true',
                        help='enable debugging')
    parser.add_argument('-t', '--timeout', metavar='SECONDS', type=int,
                        help='exit after timeout instead of running indefinitely')
    args = parser.parse_args()

    timeout = args.timeout
    if args.debug:
        if timeout is None:
            timeout = 1
        log_level = logging.DEBUG
    elif args.verbose:
        log_level = logging.INFO
    else:
        log_level = logging.WARN
    logging.basicConfig(level=log_level)

    asyncio.run(main(timeout))
