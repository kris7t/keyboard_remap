#!/usr/bin/env python

import asyncio
import evdev
import logging
import pyudev
import signal

REMAPPED_PREFIX = '[remapped]'


class AlreadyRemappedError(Exception):
    pass


class VirtualModifier():
    def __init__(self, keyboard, code):
        self._keyboard = keyboard
        self.code = code
        self._pressed_keys = []
        self._previous_state = False
        self._callbacks = []

    @property
    def pressed(self):
        return bool(self._pressed_keys)

    @property
    def repeating_key(self):
        if self._pressed_keys:
            self._pressed_keys[0]
        else:
            None

    def add_callback(self, callback):
        self._callbacks.append(callback)

    def press(self, key):
        was_pressed = self.pressed
        if key not in self._pressed_keys:
            self._pressed_keys.append(key)
        if not was_pressed:
            self.trigger()

    def release(self, key):
        self._pressed_keys.remove(key)

    def trigger(self):
        for callback in self._callbacks:
            callback()

    def flush_state_change(self, sync_event):
        if self.pressed and not self._previous_state:
            self._keyboard.write_event(
                evdev.InputEvent(sync_event.sec, sync_event.usec,
                                 evdev.ecodes.EV_KEY, self.code, 1))
        elif not self.pressed and self._previous_state:
            self._keyboard.write_event(
                evdev.InputEvent(sync_event.sec, sync_event.usec,
                                 evdev.ecodes.EV_KEY, self.code, 0))
        self._previous_state = self.pressed


class Key:
    def __init__(self, keyboard):
        self._keyboard = keyboard

    def process_event(self, event):
        return True

    def flush_event(self, event):
        self._keyboard.write_event(event)


class TriggerKey(Key):
    def __init__(self, keyboard, *callbacks):
        super().__init__(keyboard)
        self._callbacks = callbacks

    def _trigger(self):
        for callback in self._callbacks:
            callback()

    def process_event(self, event):
        if event.value == 1 or event.value == 2:
            self._trigger()
        return super().process_event(event)


class ModifierKey(Key):
    def __init__(self, keyboard, virtual_modifier):
        super().__init__(keyboard)
        self._virtual_modifier = virtual_modifier

    def process_event(self, event):
        if event.value == 0:
            self._virtual_modifier.release(self)
            return False
        elif event.value == 1:
            self._virtual_modifier.press(self)
            return False
        else:
            return True

    def flush_event(self, event):
        if event.value == 2 and self._virtual_modifier.repeating_key is self:
            event.code = self._virtual_modifier.code
            super().flush_event(event)


class TriggeredModifierKey(ModifierKey):
    def __init__(self, keyboard, single_code, virtual_modifier):
        super().__init__(keyboard, virtual_modifier)
        virtual_modifier.add_callback(self._trigger)
        self._single_code = single_code
        self._pressed = False
        self._triggered = False

    def _trigger(self):
        if self._pressed and not self._triggered:
            self._virtual_modifier.press(self)
            self._triggered = True

    def process_event(self, event):
        if event.value == 0:
            self._pressed = False
            if self._triggered:
                self._triggered = False
                self._virtual_modifier.release(self)
                return False
            else:
                return True
        if event.value == 1:
            self._pressed = True
            self._triggered = self._virtual_modifier.pressed
            return False
        if event.value == 2:
            return self._triggered

    def flush_event(self, event):
        if event.value == 0:
            if self._triggered:
                self._triggered = False
            else:
                event.code = self._single_code
                event.value = 1
                self._keyboard.write_event(event)
                event.value = 0
                self._keyboard.write_event(event)
        else:
            super().flush_event(event)


class OnReleaseKey(TriggerKey):
    def __init__(self, keyboard, extra_code, *callbacks):
        super().__init__(keyboard)
        self._extra_code = extra_code
        self._pressed = False
        self._triggered = False

    def trigger(self):
        if self._pressed and not self._triggered:
            self._triggered = True

    def process_event(self, event):
        if event.value == 0:
            self._pressed = False
        if event.value == 1:
            self._pressed = True
        if event.value == 2:
            if not self._triggered:
                self._triggered = True
        return True

    def flush_event(self, event):
        if event.value == 0:
            if self._triggered:
                self._triggered = False
            else:
                main_code = event.code
                event.code = self._extra_code
                event.value = 1
                self._keyboard.write_event(event)
                event.value = 0
                self._keyboard.write_event(event)
                event.code = main_code
        super().flush_event(event)

class Keyboard:
    def __init__(self, path):
        self._logger = logging.getLogger(type(self).__qualname__)
        self._device = evdev.InputDevice(path)
        name = self._device.name
        if name.startswith(REMAPPED_PREFIX):
            self._device.close()
            raise AlreadyRemappedError()
        try:
            self._uinput = evdev.UInput.from_device(
                self._device, name=f'{REMAPPED_PREFIX} {name}')
        except:
            self._device.close()
            raise
        self._logger = self._logger.getChild(name)
        self._logger.info(f'Initialized at {path}')
        right_alt = VirtualModifier(self, evdev.ecodes.KEY_CAPSLOCK)
        self._virtual_modifiers = [right_alt]
        basic_key = Key(self)
        left_meta = OnReleaseKey(self, evdev.ecodes.KEY_D, right_alt.trigger)
        right_meta = OnReleaseKey(self, evdev.ecodes.KEY_F, right_alt.trigger)
        self._special_keys = {
            evdev.ecodes.KEY_LEFTSHIFT: basic_key,
            evdev.ecodes.KEY_RIGHTSHIFT: basic_key,
            evdev.ecodes.KEY_LEFTCTRL: basic_key,
            evdev.ecodes.KEY_RIGHTCTRL: basic_key,
            evdev.ecodes.KEY_CAPSLOCK: TriggeredModifierKey(self, evdev.ecodes.KEY_ESC, right_alt),
            evdev.ecodes.KEY_LEFTALT: basic_key,
            evdev.ecodes.KEY_RIGHTALT: TriggeredModifierKey(self, evdev.ecodes.KEY_COMPOSE, right_alt),
            evdev.ecodes.KEY_LEFTMETA: left_meta,
            evdev.ecodes.KEY_RIGHTMETA: right_meta,
            evdev.ecodes.KEY_MENU: basic_key,
        }
        self._default_key = TriggerKey(self, right_alt.trigger, left_meta.trigger, right_meta.trigger)
        self._backlog = []

    async def process_events(self):
        try:
            with self._device.grab_context():
                async for event in self._device.async_read_loop():
                    self._process_event(event)
        except asyncio.CancelledError:
            pass
        finally:
            self._uinput.close()
            self._device.close()

    def _process_event(self, event):
        if event.type == evdev.ecodes.EV_SYN:
            for virtual_modifier in self._virtual_modifiers:
                virtual_modifier.flush_state_change(event)
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
        self._logger.debug(f'out: {repr(event)}')
        self._uinput.write_event(event)


class KeyboardRemapper:
    def __init__(self):
        self._logger = logging.getLogger(type(self).__qualname__)
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
        asyncio.gather(*self._devices.values())

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
            task = asyncio.create_task(keyboard.process_events(),
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


async def main(timeout):
    interrupted = asyncio.Event()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGINT, interrupted.set)
    tasks = [interrupted.wait()]
    if timeout is not None:
        tasks.append(asyncio.sleep(timeout))
    keyboard_remapper = KeyboardRemapper()
    try:
        await keyboard_remapper.start()
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        loop.remove_signal_handler(signal.SIGINT)
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
