# coding=utf-8
from __future__ import absolute_import

__author__ = "Shawn Bruce <kantlivelong@gmail.com>"
__license__ = "GNU Affero General Public License http://www.gnu.org/licenses/agpl.html"
__copyright__ = "Copyright (C) 2017 Shawn Bruce - Released under terms of the AGPLv3 License"

import octoprint.plugin
from octoprint.events import Events
from octoprint.util import RepeatedTimer
import time
import subprocess
import threading
from flask import make_response, jsonify
from flask_babel import gettext
from octoprint.util import fqfn
from octoprint.settings import valid_boolean_trues
import flask
from . import cli

try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except (ModuleNotFoundError, ImportError, RuntimeError):
    HAS_GPIO = False

try:
    from octoprint.access.permissions import Permissions
except Exception:
    from octoprint.server import user_permission

try:
    from octoprint.util import ResettableTimer
except Exception:
    from .util import ResettableTimer


class PSUControl(octoprint.plugin.StartupPlugin,
                 octoprint.plugin.TemplatePlugin,
                 octoprint.plugin.AssetPlugin,
                 octoprint.plugin.SettingsPlugin,
                 octoprint.plugin.SimpleApiPlugin,
                 octoprint.plugin.EventHandlerPlugin,
                 octoprint.plugin.WizardPlugin):

    def __init__(self):
        self._sub_plugins = dict()

        self.config = dict()

        self._autoOnTriggerGCodeCommandsArray = []
        self._idleIgnoreCommandsArray = []
        self._check_psu_state_thread = None
        self._check_psu_state_event = threading.Event()
        self._idleTimer = None
        self._idleCountdown = None
        self._idleStartTime = 0
        self._idleTimeLeft = None
        self._idleTimerOverride = False
        self._waitForHeaters = False
        self._skipIdleTimer = False
        self._configuredGPIOPins = {}
        self._noSensing_isPSUOn = False
        self.isPSUOn = False

        if GPIO.RPI_REVISION == 1:
            self._pin_to_gpio = [-1, -1, -1, 0, -1, 1, -1, 4, 14, -1, 15, \
                    17, 18, 21, -1, 22, 23, -1, 24, 10, -1, 9, 25, 11, 8, -1, \
                    7, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1 ]
        elif GPIO.RPI_REVISION == 2:
            self._pin_to_gpio = [-1, -1, -1, 2, -1, 3, -1, 4, 14, -1, 15, \
                    17, 18, 27, -1, 22, 23, -1, 24, 10, -1, 9, 25, 11, 8, -1, \
                    7, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1 ]
        else:
            self._pin_to_gpio = [-1, -1, -1, 2, -1, 3, -1, 4, 14, -1, 15, \
                    17, 18, 27, -1, 22, 23, -1, 24, 10, -1, 9, 25, 11, 8, -1, \
                    7, -1, -1, 5, -1, 6, 12, 13, -1, 19, 16, 26, 20, -1, 21 ]


    def get_settings_defaults(self):
        return dict(
            GPIOMode = 'BOARD',
            switchingMethod = 'GCODE',
            onoffGPIOPin = 0,
            onoffGPIOActiveMode = 'high',
            onGCodeCommand = 'M80',
            offGCodeCommand = 'M81',
            onSysCommand = '',
            offSysCommand = '',
            switchingPlugin = '',
            enablePseudoOnOff = False,
            pseudoOnGCodeCommand = 'M80',
            pseudoOffGCodeCommand = 'M81',
            postOnDelay = 0.0,
            connectOnPowerOn = False,
            disconnectOnPowerOff = False,
            sensingMethod = 'INTERNAL',
            senseGPIOPin = 0,
            sensePollingInterval = 5,
            senseGPIOActiveMode = "low",
            senseSystemCommand = '',
            sensingPlugin = '',
            autoOn = False,
            autoOnTriggerGCodeCommands = "G0,G1,G2,G3,G10,G11,G28,G29,G32,M104,M106,M109,M140,M190",
            enablePowerOffWarningDialog = True,
            enableNavBar = True,
            enableSideBar = True,
            enableIdleCountdownTimerNavBar = True,
            enableIdleCountdownTimerSideBar = True,
            powerOffWhenIdle = False,
            idleTimeout = 30,
            idleIgnoreCommands = 'M105',
            idleTimeoutWaitTemp = 50,
            turnOnWhenApiUploadPrint = False,
            turnOffWhenError = False,
            enableExternalButtonPSUOn = False,
            externalButtonPSUOn = 0,
            externalButtonPSUOnActiveMode = "low",
            enableExternalLedPSUOn = False,
            externalLedPSUOn = 0,
            externalLedPSUOnActiveMode = "high",
            enableExternalButtonOverride = False,
            externalButtonOverride = 0,
            externalButtonOverrideActiveMode = "low",
            enableExternalLedOverride = False,
            externalLedOverride = 0,
            externalLedOverrideActiveMode = "high",
        )


    def on_settings_initialized(self):
        scripts = self._settings.listScripts("gcode")

        if not "psucontrol_post_on" in scripts:
            self._settings.saveScript("gcode", "psucontrol_post_on", u'')

        if not "psucontrol_pre_off" in scripts:
            self._settings.saveScript("gcode", "psucontrol_pre_off", u'')

        self.reload_settings()


    def reload_settings(self):
        for k, v in self.get_settings_defaults().items():
            if type(v) == str:
                v = self._settings.get([k])
            elif type(v) == int:
                v = self._settings.get_int([k])
            elif type(v) == float:
                v = self._settings.get_float([k])
            elif type(v) == bool:
                v = self._settings.get_boolean([k])

            self.config[k] = v
            self._logger.debug("{}: {}".format(k, v))

        if self.config['switchingMethod'] == 'GPIO' and not HAS_GPIO:
            self._logger.error("Unable to use GPIO for switchingMethod.")
            self.config['switchingMethod'] = ''

        if self.config['sensingMethod'] == 'GPIO' and not HAS_GPIO:
            self._logger.error("Unable to use GPIO for sensingMethod.")
            self.config['sensingMethod'] = ''

        if (self.config['enableExternalButtonPSUOn'] or self.config['enableExternalButtonOverride']) and not HAS_GPIO:
            self._logger.error("Unable to use GPIO for external button.")
            self.config['enableExternalButtonPSUOn'] = False
            self.config['enableExternalButtonOverride'] = False

        if (self.config['enableExternalLedPSUOn'] or self.config['enableExternalLedOverride']) and not HAS_GPIO:
            self._logger.error("Unable to use GPIO for external led.")
            self.config['enableExternalLedPSUOn'] = False
            self.config['enableExternalLedOverride'] = False

        if self.config['enablePseudoOnOff'] and self.config['switchingMethod'] == 'GCODE':
            self._logger.warning("Pseudo On/Off cannot be used in conjunction with GCODE switching. Disabling.")
            self.config['enablePseudoOnOff'] = False

        self._autoOnTriggerGCodeCommandsArray = self.config['autoOnTriggerGCodeCommands'].split(',')
        self._idleIgnoreCommandsArray = self.config['idleIgnoreCommands'].split(',')


    def on_after_startup(self):
        if self.config['switchingMethod'] == 'GPIO' or self.config['sensingMethod'] == 'GPIO':
            self.configure_gpio()

        self._check_psu_state_thread = threading.Thread(target=self._check_psu_state)
        self._check_psu_state_thread.daemon = True
        self._check_psu_state_thread.start()

        self._start_idle_timer()


    def _gpio_get_pin(self, pin):
        try:
            if (GPIO.getmode() == GPIO.BOARD and self.config['GPIOMode'] == 'BOARD') or \
                    (GPIO.getmode() == GPIO.BCM and self.config['GPIOMode'] == 'BCM'):
                return pin
            elif GPIO.getmode() == GPIO.BOARD and self.config['GPIOMode'] == 'BCM':
                return self._pin_to_gpio.index(pin)
            elif GPIO.getmode() == GPIO.BCM and self.config['GPIOMode'] == 'BOARD':
                return self._pin_to_gpio[pin]
            else:
                return 0
        except (ValueError, IndexError):
            return 0


    def _event_gpio_btn(self, channel):
        btn_name = None
        for name, options in self._configuredGPIOPins.items():
            if channel == options[0]:
                btn_name = name
                break

        if btn_name == None:
            return

        if btn_name == "ext_btn_psuon":
            if not self.isPSUOn:
                self.turn_psu_on()
        elif btn_name == "ext_btn_ovr":
            if self.isPSUOn:
                self.set_idle_timer_override(not self._idleTimerOverride)
        else:
            self._logger.exception(
                "Exception GPIO event triggered for {} pin {}, no action linked.".format(btn_name, channel)
            )


    def cleanup_gpio(self):
        GPIO.setwarnings(False)

        for name, options in self._configuredGPIOPins.items():
            self._logger.debug("Cleaning up {} pin {}".format(name, options[0]))
            try:
                GPIO.cleanup(options[0])
            except Exception:
                self._logger.exception(
                    "Exception while cleaning up {} pin {}.".format(name, options[0])
                )
        self._configuredGPIOPins = {}


    def _configure_output_gpio(self, name: str, pin: int, active_mode: str):
        if pin <= 0:
            self._logger.error("GPIO pin is not valid")
            return

        self._logger.info("Configuring output GPIO for {} pin {}".format(name, pin))

        default_low = GPIO.LOW if active_mode == "high" else GPIO.HIGH

        try:
            GPIO.setup(pin, GPIO.OUT)
            GPIO.output(pin, default_low)
            self._configuredGPIOPins[name] = [pin, active_mode]
        except Exception:
            self._logger.exception(
                "Exception while setting up GPIO pin {}".format(pin)
            )


    def _configure_input_gpio(self, name: str, pin: int, active_mode: str, press_trigger: any = None):
        if pin <= 0:
            return

        self._logger.info("Configuring output GPIO for {} pin {}".format(name, pin))

        pull_up_down = GPIO.PUD_UP if active_mode == "low" else GPIO.PUD_DOWN
        gpio_event = GPIO.RISING if active_mode == "low" else GPIO.FALLING

        try:
            GPIO.setup(pin, GPIO.IN, pull_up_down=pull_up_down)
            if not press_trigger == None:
                GPIO.remove_event_detect(pin)
                GPIO.add_event_detect(
                    pin,
                    gpio_event,
                    callback=press_trigger,
                    bouncetime=200
                )
            self._configuredGPIOPins[name] = [pin, active_mode]
        except Exception:
            self._logger.exception(
                "Exception while setting up GPIO pin {}".format(pin)
            )


    def configure_gpio(self):
        if not HAS_GPIO:
            self._logger.error("Error importing RPi.GPIO.")
            return

        GPIO.setwarnings(False)

        if GPIO.getmode() is None:
            if self.config['GPIOMode'] == 'BOARD':
                GPIO.setmode(GPIO.BOARD)
            elif self.config['GPIOMode'] == 'BCM':
                GPIO.setmode(GPIO.BCM)
            else:
                return

        if self.config['switchingMethod'] == 'GPIO':
            self._configure_output_gpio("switch", self._gpio_get_pin(self.config['onoffGPIOPin']), \
                    self.config['onoffGPIOActiveMode'])

        if self.config['sensingMethod'] == 'GPIO':
            self._configure_input_gpio("sense", self._gpio_get_pin(self.config['senseGPIOPin']), \
                    self.config['senseGPIOPinPUD'])

        if self.config['enableExternalButtonPSUOn']:
            self._configure_input_gpio("ext_btn_psuon", self._gpio_get_pin(self.config['externalButtonPSUOn']),\
                    self.config['externalButtonPSUOnActiveMode'], self._event_gpio_btn)
        if self.config['enableExternalLedPSUOn']:
            self._configure_output_gpio("ext_led_psuon", self._gpio_get_pin(self.config['externalLedPSUOn']),\
                    self.config['externalLedPSUOnActiveMode'])
            self.update_psu_led(self.isPSUOn)

        if self.config['enableExternalButtonOverride']:
            self._configure_input_gpio("ext_btn_ovr", self._gpio_get_pin(self.config['externalButtonOverride']),\
                    self.config['externalButtonOverrideActiveMode'], self._event_gpio_btn)
        if self.config['enableExternalLedOverride']:
            self._configure_output_gpio("ext_led_ovr", self._gpio_get_pin(self.config['externalLedOverride']),\
                    self.config['externalLedOverrideActiveMode'])
            self.update_override_led(self._idleTimerOverride)


    def _get_plugin_key(self, implementation):
        for k, v in self._plugin_manager.plugin_implementations.items():
            if v == implementation:
                return k


    def register_plugin(self, implementation):
        k = self._get_plugin_key(implementation)

        self._logger.debug("Registering plugin - {}".format(k))

        if k not in self._sub_plugins:
            self._logger.info("Registered plugin - {}".format(k))
            self._sub_plugins[k] = implementation


    def _write_to_GPIO(self, name: str, state: bool) -> bool:
        if name not in self._configuredGPIOPins.keys():
            return

        gpio_pin = self._configuredGPIOPins[name][0]
        active_mode = self._configuredGPIOPins[name][1]

        if state:
            pin_output = GPIO.HIGH if active_mode == "high" else GPIO.LOW
        else:
            pin_output = GPIO.LOW if active_mode == "high" else GPIO.HIGH

        self._logger.debug("Switching GPIO: {}, state: {}".format(gpio_pin, pin_output))

        try:
            GPIO.output(gpio_pin, pin_output)
        except Exception:
            self._logger.exception("Exception while writing GPIO line")
            return False

        return True


    def update_psu_led(self, state: bool):
        if not self.config["enableExternalLedPSUOn"]:
            return
        
        self._write_to_GPIO("ext_led_psuon", state)


    def update_override_led(self, state: bool):
        if not self.config["enableExternalLedOverride"]:
            return
        
        self._write_to_GPIO("ext_led_ovr", state)


    def check_psu_state(self):
        self._check_psu_state_event.set()


    def _check_psu_state(self):
        while True:
            old_isPSUOn = self.isPSUOn

            self._logger.debug("Polling PSU state...")

            if self.config['sensingMethod'] == 'GPIO':
                r = 0
                try:
                    r = GPIO.input(self._configuredGPIOPins["sense"][0])
                except Exception:
                    self._logger.exception("Exception while reading GPIO line")

                self._logger.debug("Result: {}".format(r))

                new_isPSUOn = r ^ (1 if self.config['senseGPIOActiveMode'] == "low" else 0)

                self.isPSUOn = new_isPSUOn
            elif self.config['sensingMethod'] == 'SYSTEM':
                new_isPSUOn = False

                p = subprocess.Popen(self.config['senseSystemCommand'], shell=True)
                self._logger.debug("Sensing system command executed. PID={}, Command={}".format(p.pid, self.config['senseSystemCommand']))
                while p.poll() is None:
                    time.sleep(0.1)
                r = p.returncode
                self._logger.debug("Sensing system command returned: {}".format(r))

                if r == 0:
                    new_isPSUOn = True
                elif r == 1:
                    new_isPSUOn = False

                self.isPSUOn = new_isPSUOn
            elif self.config['sensingMethod'] == 'INTERNAL':
                self.isPSUOn = self._noSensing_isPSUOn
            elif self.config['sensingMethod'] == 'PLUGIN':
                p = self.config['sensingPlugin']

                r = False

                if p not in self._sub_plugins:
                    self._logger.error('Plugin {} is configured for sensing but it is not registered.'.format(p))
                elif not hasattr(self._sub_plugins[p], 'get_psu_state'):
                    self._logger.error('Plugin {} is configured for sensing but get_psu_state is not defined.'.format(p))
                else:
                    callback = self._sub_plugins[p].get_psu_state
                    try:
                        r = callback()
                    except Exception:
                        self._logger.exception(
                            "Error while executing callback {}".format(
                                callback
                            ),
                            extra={"callback": fqfn(callback)},
                        )

                self.isPSUOn = r
            else:
                self.isPSUOn = False

            self._logger.debug("isPSUOn: {}".format(self.isPSUOn))

            if (old_isPSUOn != self.isPSUOn):
                self._logger.debug("PSU state changed, firing psu_state_changed event.")

                event = Events.PLUGIN_PSUCONTROL_PSU_STATE_CHANGED
                self._event_bus.fire(event, payload=dict(isPSUOn=self.isPSUOn))
                self.update_psu_led(self.isPSUOn)

            if (old_isPSUOn != self.isPSUOn) and self.isPSUOn:
                self._start_idle_timer()
            elif (old_isPSUOn != self.isPSUOn) and not self.isPSUOn:
                self._stop_idle_timer()
                self.set_idle_timer_override(False)

            self._plugin_manager.send_plugin_message(self._identifier, dict(isPSUOn=self.isPSUOn))

            self._check_psu_state_event.wait(self.config['sensePollingInterval'])
            self._check_psu_state_event.clear()

    def _set_start_time(self):
        self._idleStartTime = time.time()

    def _countdown_visible(self):
        return ((self.config['enableNavBar'] and self.config['enableIdleCountdownTimerNavBar']) or
                (self.config['enableSideBar'] and self.config['enableIdleCountdownTimerSideBar']))

    def _refresh_countdown(self):
        if self._idleStartTime == 0 or not self.config['powerOffWhenIdle'] or \
                not self._countdown_visible() or self._idleTimerOverride or \
                self._printer.is_printing() or self._printer.is_paused():
            self.idleTimeLeft = None
        else:
            self.idleTimeLeft = time.strftime("%-M:%S", time.gmtime((self.config['idleTimeout'] * 60) - (time.time() - self._idleStartTime)))
        self._plugin_manager.send_plugin_message(self._identifier, dict(idleTimeLeft=self.idleTimeLeft))

    def _start_idle_timer(self):
        self._stop_idle_timer()

        if self.config['powerOffWhenIdle'] and self.isPSUOn and not self._idleTimerOverride:
            self._idleTimer = ResettableTimer(self.config['idleTimeout'] * 60, self._idle_poweroff)
            self._idleCountdown = RepeatedTimer(1.0, self._refresh_countdown)
            self._idleTimer.start()
            self._set_start_time()
            self._idleCountdown.start()


    def _stop_idle_timer(self):
        if self._idleTimer:
            self._idleTimer.cancel()
            self._idleTimer = None
            self._idleStartTime = 0
            self._idleCountdown.cancel()
            self._idleCountdown = None
            self._refresh_countdown()

    def _reset_idle_timer(self):
        try:
            if self._idleTimer.is_alive():
                self._idleTimer.reset()
                self._set_start_time()
            else:
                raise Exception()
        except:
            self._start_idle_timer()


    def _idle_poweroff(self):
        if not self.config['powerOffWhenIdle']:
            return

        if self._waitForHeaters:
            return

        if self._printer.is_printing() or self._printer.is_paused():
            return

        if self._idleTimerOverride:
            return

        self._logger.info("Idle timeout reached after {} minute(s). Turning heaters off prior to shutting off PSU.".format(self.config['idleTimeout']))
        if self._wait_for_heaters():
            self._logger.info("Heaters below temperature.")
            self.turn_psu_off()
        else:
            self._logger.info("Aborted PSU shut down due to activity.")


    def _wait_for_heaters(self):
        self._waitForHeaters = True
        heaters = self._printer.get_current_temperatures()

        for heater, entry in heaters.items():
            target = entry.get("target")
            if target is None:
                # heater doesn't exist in fw
                continue

            try:
                temp = float(target)
            except ValueError:
                # not a float for some reason, skip it
                continue

            if temp != 0:
                self._logger.info("Turning off heater: {}".format(heater))
                self._skipIdleTimer = True
                self._printer.set_temperature(heater, 0)
                self._skipIdleTimer = False
            else:
                self._logger.debug("Heater {} already off.".format(heater))

        while True:
            if not self._waitForHeaters:
                return False

            heaters = self._printer.get_current_temperatures()

            highest_temp = 0
            heaters_above_waittemp = []
            for heater, entry in heaters.items():
                if not heater.startswith("tool"):
                    continue

                actual = entry.get("actual")
                if actual is None:
                    # heater doesn't exist in fw
                    continue

                try:
                    temp = float(actual)
                except ValueError:
                    # not a float for some reason, skip it
                    continue

                self._logger.debug("Heater {} = {}C".format(heater, temp))
                if temp > self.config['idleTimeoutWaitTemp']:
                    heaters_above_waittemp.append(heater)

                if temp > highest_temp:
                    highest_temp = temp

            if highest_temp <= self.config['idleTimeoutWaitTemp']:
                self._waitForHeaters = False
                return True

            self._logger.info("Waiting for heaters({}) before shutting off PSU...".format(', '.join(heaters_above_waittemp)))
            time.sleep(5)


    def hook_gcode_queuing(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        skipQueuing = False

        if not gcode:
            gcode = cmd.split(' ', 1)[0]

        if self.config['enablePseudoOnOff']:
            if gcode == self.config['pseudoOnGCodeCommand']:
                self.turn_psu_on()
                comm_instance._log("PSUControl: ok")
                skipQueuing = True
            elif gcode == self.config['pseudoOffGCodeCommand']:
                self.turn_psu_off()
                comm_instance._log("PSUControl: ok")
                skipQueuing = True

        if (not self.isPSUOn and self.config['autoOn'] and (gcode in self._autoOnTriggerGCodeCommandsArray)):
            self._logger.info("Auto-On - Turning PSU On (Triggered by {})".format(gcode))
            self.turn_psu_on()

        if self.config['powerOffWhenIdle'] and self.isPSUOn and not self._skipIdleTimer:
            if not (gcode in self._idleIgnoreCommandsArray):
                self._waitForHeaters = False
                self._reset_idle_timer()

        if skipQueuing:
            return (None,)


    def turn_psu_on(self):
        if self.config['switchingMethod'] in ['GCODE', 'GPIO', 'SYSTEM', 'PLUGIN']:
            self._logger.info("Switching PSU On")
            if self.config['switchingMethod'] == 'GCODE':
                self._logger.debug("Switching PSU On Using GCODE: {}".format(self.config['onGCodeCommand']))
                self._printer.commands(self.config['onGCodeCommand'])
            elif self.config['switchingMethod'] == 'SYSTEM':
                self._logger.debug("Switching PSU On Using SYSTEM: {}".format(self.config['onSysCommand']))

                p = subprocess.Popen(self.config['onSysCommand'], shell=True)
                self._logger.debug("On system command executed. PID={}, Command={}".format(p.pid, self.config['onSysCommand']))
                while p.poll() is None:
                    time.sleep(0.1)
                r = p.returncode

                self._logger.debug("On system command returned: {}".format(r))
            elif self.config['switchingMethod'] == 'GPIO':
                if not self._write_to_GPIO("switch", True):
                    return
            elif self.config['switchingMethod'] == 'PLUGIN':
                p = self.config['switchingPlugin']
                self._logger.debug("Switching PSU On Using PLUGIN: {}".format(p))

                if p not in self._sub_plugins:
                    self._logger.error('Plugin {} is configured for switching but it is not registered.'.format(p))
                    return
                elif not hasattr(self._sub_plugins[p], 'turn_psu_on'):
                    self._logger.error('Plugin {} is configured for switching but turn_psu_on is not defined.'.format(p))
                    return
                else:
                    callback = self._sub_plugins[p].turn_psu_on
                    try:
                        r = callback()
                    except Exception:
                        self._logger.exception(
                            "Error while executing callback {}".format(
                                callback
                            ),
                            extra={"callback": fqfn(callback)},
                        )
                        return

            if self.config['sensingMethod'] not in ('GPIO', 'SYSTEM', 'PLUGIN'):
                self._noSensing_isPSUOn = True

            time.sleep(0.1 + self.config['postOnDelay'])

            self.check_psu_state()

            if self.config['connectOnPowerOn'] and self._printer.is_closed_or_error():
                self._printer.connect()
                time.sleep(0.1)

            if not self._printer.is_closed_or_error():
                self._printer.script("psucontrol_post_on", must_be_set=False)


    def turn_psu_off(self):
        if self.config['switchingMethod'] in ['GCODE', 'GPIO', 'SYSTEM', 'PLUGIN']:
            if not self._printer.is_closed_or_error():
                self._printer.script("psucontrol_pre_off", must_be_set=False)

            self._logger.info("Switching PSU Off")
            if self.config['switchingMethod'] == 'GCODE':
                self._logger.debug("Switching PSU Off Using GCODE: {}".format(self.config['offGCodeCommand']))
                self._printer.commands(self.config['offGCodeCommand'])
            elif self.config['switchingMethod'] == 'SYSTEM':
                self._logger.debug("Switching PSU Off Using SYSTEM: {}".format(self.config['offSysCommand']))

                p = subprocess.Popen(self.config['offSysCommand'], shell=True)
                self._logger.debug("Off system command executed. PID={}, Command={}".format(p.pid, self.config['offSysCommand']))
                while p.poll() is None:
                    time.sleep(0.1)
                r = p.returncode

                self._logger.debug("Off system command returned: {}".format(r))
            elif self.config['switchingMethod'] == 'GPIO':
                if not self._write_to_GPIO("switch", False):
                    return
            elif self.config['switchingMethod'] == 'PLUGIN':
                p = self.config['switchingPlugin']
                self._logger.debug("Switching PSU Off Using PLUGIN: {}".format(p))

                if p not in self._sub_plugins:
                    self._logger.error('Plugin {} is configured for switching but it is not registered.'.format(p))
                    return
                elif not hasattr(self._sub_plugins[p], 'turn_psu_off'):
                    self._logger.error('Plugin {} is configured for switching but turn_psu_off is not defined.'.format(p))
                    return
                else:
                    callback = self._sub_plugins[p].turn_psu_off
                    try:
                        r = callback()
                    except Exception:
                        self._logger.exception(
                            "Error while executing callback {}".format(
                                callback
                            ),
                            extra={"callback": fqfn(callback)},
                        )
                        return

            if self.config['disconnectOnPowerOff']:
                self._printer.disconnect()

            if self.config['sensingMethod'] not in ('GPIO', 'SYSTEM', 'PLUGIN'):
                self._noSensing_isPSUOn = False

            time.sleep(0.1)
            self.check_psu_state()


    def get_psu_state(self):
        return self.isPSUOn


    def set_idle_timer_override(self, state, send_event = True):
        self._idleTimerOverride = state
        self.update_override_led(state)

        if send_event:
            self._plugin_manager.send_plugin_message(self._identifier, dict(idleTimerOverride=self._idleTimerOverride))

        if state:
            self._stop_idle_timer()
        else:
            self._start_idle_timer()


    def turn_on_before_printing_after_upload(self):
        if ( self.config['turnOnWhenApiUploadPrint'] and
             not self.isPSUOn and
             flask.request.path.startswith('/api/files/') and
             flask.request.method == 'POST' and
             flask.request.values.get('print', 'false') in valid_boolean_trues):
                self.on_api_command("turnPSUOn", [])


    def on_event(self, event, payload):
        if event == Events.CLIENT_OPENED:
            self._plugin_manager.send_plugin_message(self._identifier, dict(isPSUOn=self.isPSUOn))
            return
        elif event == Events.ERROR and self.config['turnOffWhenError']:
            self._logger.info("Firmware or communication error detected. Turning PSU Off")
            self.turn_psu_off()
            return


    def get_api_commands(self):
        return dict(
            turnPSUOn=[],
            turnPSUOff=[],
            togglePSU=[],
            getPSUState=[],
            setPsuOverride=["state"],
        )


    @Permissions.STATUS.require(403)
    def on_api_get(self, request):
        action = request.args.get("action", default="", type=str)
        if action == "":
            return jsonify(isPSUOn=self.isPSUOn)
        elif action == 'getPSUState':
            return jsonify(isPSUOn=self.isPSUOn)
        elif action == 'getIdleTimerOverride':
            return jsonify(idleTimerOverride=self._idleTimerOverride)
        else:
            return make_response("No api implementation of {}".format(action), 404)


    def on_api_command(self, command, data):
        if command in ['turnPSUOn', 'turnPSUOff', 'togglePSU', "setPsuOverride"]:
            try:
                if not Permissions.PLUGIN_PSUCONTROL_CONTROL.can():
                    return make_response("Insufficient rights", 403)
            except:
                if not user_permission.can():
                    return make_response("Insufficient rights", 403)

        if command == 'turnPSUOn':
            self.turn_psu_on()
        elif command == 'turnPSUOff':
            self.turn_psu_off()
        elif command == 'togglePSU':
            if self.isPSUOn:
                self.turn_psu_off()
            else:
                self.turn_psu_on()
        elif command == "setPsuOverride":
            if 'state' in data.keys():
                self.set_idle_timer_override(data['state'], False)


    def on_settings_save(self, data):
        if 'scripts_gcode_psucontrol_post_on' in data:
            script = data["scripts_gcode_psucontrol_post_on"]
            self._settings.saveScript("gcode", "psucontrol_post_on", u'' + script.replace("\r\n", "\n").replace("\r", "\n"))
            data.pop('scripts_gcode_psucontrol_post_on')

        if 'scripts_gcode_psucontrol_pre_off' in data:
            script = data["scripts_gcode_psucontrol_pre_off"]
            self._settings.saveScript("gcode", "psucontrol_pre_off", u'' + script.replace("\r\n", "\n").replace("\r", "\n"))
            data.pop('scripts_gcode_psucontrol_pre_off')

        old_config = self.config.copy()

        octoprint.plugin.SettingsPlugin.on_settings_save(self, data)

        self.reload_settings()

        #cleanup GPIO
        self.cleanup_gpio()

        #configure GPIO
        if self.config['switchingMethod'] == 'GPIO' or self.config['sensingMethod'] == 'GPIO' or \
                self.config['enableExternalButtonPSUOn'] or self.config['enableExternalButtonOverride'] or \
                self.config['enableExternalLedPSUOn'] or self.config['enableExternalLedOverride']:
            self.configure_gpio()

        self._start_idle_timer()


    def get_wizard_version(self):
        return 1


    def is_wizard_required(self):
        return True


    def get_settings_version(self):
        return 4


    def on_settings_migrate(self, target, current=None):
        pass


    def get_template_vars(self):
        available_plugins = []
        for k in list(self._sub_plugins.keys()):
            available_plugins.append(dict(pluginIdentifier=k, displayName=self._plugin_manager.plugins[k].name))

        return {
            "availablePlugins": available_plugins,
            "hasGPIO": HAS_GPIO,
        }


    def get_template_configs(self):
        return [
            dict(
                type="sidebar",
                icon="bolt",
                template="psucontrol_sidebar.jinja2",
                custom_bindings=True
            ),
            dict(
                type="settings",
                name="PSU Control",
                template="psucontrol_settings.jinja2",
                custom_bindings=True
            )
        ]


    def get_assets(self):
        return {
            "js": ["js/psucontrol.js"],
            "less": ["less/psucontrol.less"],
            "css": ["css/psucontrol.css", "css/psucontrol.min.css"]
        } 


    def get_update_information(self):
        return dict(
            psucontrol=dict(
                displayName="PSU Control",
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="kantlivelong",
                repo="OctoPrint-PSUControl",
                current=self._plugin_version,

                # update method: pip w/ dependency links
                pip="https://github.com/kantlivelong/OctoPrint-PSUControl/archive/{target_version}.zip"
            )
        )


    def register_custom_events(self):
        return ["psu_state_changed"]


    def get_additional_permissions(self, *args, **kwargs):
        return [
            dict(key="CONTROL",
                 name="Control",
                 description=gettext("Allows switching PSU on/off"),
                 roles=["admin"],
                 dangerous=True,
                 default_groups=[Permissions.ADMIN_GROUP])
        ]


    def _hook_octoprint_server_api_before_request(self, *args, **kwargs):
        return [self.turn_on_before_printing_after_upload]


__plugin_name__ = "PSU Control"
__plugin_pythoncompat__ = ">=2.7,<4"

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = PSUControl()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.comm.protocol.gcode.queuing": __plugin_implementation__.hook_gcode_queuing,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.events.register_custom_events": __plugin_implementation__.register_custom_events,
        "octoprint.access.permissions": __plugin_implementation__.get_additional_permissions,
        "octoprint.cli.commands": cli.commands,
        "octoprint.server.api.before_request": __plugin_implementation__._hook_octoprint_server_api_before_request,
    }

    global __plugin_helpers__
    __plugin_helpers__ = dict(
        get_psu_state = __plugin_implementation__.get_psu_state,
        turn_psu_on = __plugin_implementation__.turn_psu_on,
        turn_psu_off = __plugin_implementation__.turn_psu_off,
        register_plugin = __plugin_implementation__.register_plugin
    )
