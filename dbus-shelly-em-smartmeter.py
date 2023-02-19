#!/usr/bin/env python

# import normal packages
import platform
import logging
import sys
import os
import sys

if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import time
import requests  # for http GET
import configparser  # for config/ini file

# our own packages from victron
sys.path.insert(
    1,
    os.path.join(
        os.path.dirname(__file__),
        "/opt/victronenergy/dbus-systemcalc-py/ext/velib_python",
    ),
)
from vedbus import VeDbusService


PHASES = ["L1", "L2", "L3"]


class DbusShellyemService:
    def __init__(
        self,
        servicename,
        paths,
        productname="Shelly EM",
        connection="Shelly EM HTTP JSON service",
    ):
        self.config = self._getConfig()
        deviceinstance = int(self.config["DEFAULT"]["Deviceinstance"])
        customname = self.config["DEFAULT"]["CustomName"]

        self.phase = self.config["DEFAULT"]["Phase"].upper()
        if self.phase not in PHASES:
            raise ValueError(
                "Phase %s is not supported, must be one of 'L1', 'L2', 'L3'"
                % (self.config["DEFAULT"]["Phase"])
            )

        self.shelly_url = self._getShellyStatusUrl()

        self._dbusservice = VeDbusService(
            "{}.http_{:02d}".format(servicename, deviceinstance)
        )
        self._paths = paths

        logging.debug("%s /DeviceInstance = %d" % (servicename, deviceinstance))

        # Create the management objects, as specified in the ccgx dbus-api document
        self._dbusservice.add_path("/Mgmt/ProcessName", __file__)
        self._dbusservice.add_path(
            "/Mgmt/ProcessVersion",
            "Unkown version, and running on Python " + platform.python_version(),
        )
        self._dbusservice.add_path("/Mgmt/Connection", connection)

        # Create the mandatory objects
        self._dbusservice.add_path("/DeviceInstance", deviceinstance)
        # self._dbusservice.add_path('/ProductId', 16) # value used in ac_sensor_bridge.cpp of dbus-cgwacs
        # self._dbusservice.add_path('/ProductId', 0xFFFF) # id assigned by Victron Support from SDM630v2.py
        # self._dbusservice.add_path('/ProductId', 45069) # found on https://www.sascha-curth.de/projekte/005_Color_Control_GX.html#experiment - should be an ET340 Engerie Meter
        self._dbusservice.add_path(
            "/ProductId", 0xB023
        )  # id needs to be assigned by Victron Support current value for testing
        self._dbusservice.add_path(
            "/DeviceType", 345
        )  # found on https://www.sascha-curth.de/projekte/005_Color_Control_GX.html#experiment - should be an ET340 Engerie Meter
        self._dbusservice.add_path("/ProductName", productname)
        self._dbusservice.add_path("/CustomName", customname)
        self._dbusservice.add_path("/Latency", None)
        self._dbusservice.add_path("/FirmwareVersion", 0.1)
        self._dbusservice.add_path("/HardwareVersion", 0)
        self._dbusservice.add_path("/Connected", 1)
        self._dbusservice.add_path("/Role", "grid")
        self._dbusservice.add_path("/Position", 0)  # normaly only needed for pvinverter
        self._dbusservice.add_path("/Serial", self._getShellySerial())
        self._dbusservice.add_path("/UpdateIndex", 0)

        # add path values to dbus
        for path, settings in self._paths.items():
            self._dbusservice.add_path(
                path,
                settings["initial"],
                gettextcallback=settings["textformat"],
                writeable=True,
                onchangecallback=self._handlechangedvalue,
            )

        # last update
        self._lastUpdate = 0

        # add _update function 'timer'
        gobject.timeout_add(250, self._update)  # pause 250ms before the next request

        # add _signOfLife 'timer' to get feedback in log every 5minutes
        gobject.timeout_add(self._getSignOfLifeInterval() * 60 * 1000, self._signOfLife)

    def _getShellySerial(self):
        meter_data = self._getShellyData()

        if not meter_data["mac"]:
            raise ValueError("Response does not contain 'mac' attribute")

        serial = meter_data["mac"]
        return serial

    def _getConfig(self):
        config = configparser.ConfigParser()
        config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
        return config

    def _getSignOfLifeInterval(self):
        value = self.config["DEFAULT"]["SignOfLifeLog"]

        if not value:
            value = 0

        return int(value)

    def _getShellyStatusUrl(self):
        accessType = self.config["DEFAULT"]["AccessType"]

        if accessType == "OnPremise":
            URL = "http://%s:%s@%s/status" % (
                self.config["ONPREMISE"]["Username"],
                self.config["ONPREMISE"]["Password"],
                self.config["ONPREMISE"]["Host"],
            )
            URL = URL.replace(":@", "")
        else:
            raise ValueError(
                "AccessType %s is not supported"
                % (self.config["DEFAULT"]["AccessType"])
            )

        return URL

    def _getShellyData(self):
        meter_r = requests.get(url=self.shelly_url)

        # check for response
        if not meter_r:
            raise ConnectionError("No response from Shelly EM - %s" % (self.shelly_url))

        meter_data = meter_r.json()

        # check for Json
        if not meter_data:
            raise ValueError("Converting response to JSON failed")

        return meter_data

    def _signOfLife(self):
        logging.info("--- Start: sign of life ---")
        logging.info("Last _update() call: %s" % (self._lastUpdate))
        logging.info("Last '/Ac/Power': %s" % (self._dbusservice["/Ac/Power"]))
        logging.info("--- End: sign of life ---")
        return True

    def _map_meter_data(self, phase, d=None):
        self._dbusservice[f"/Ac/{ phase }/Voltage"] = d["voltage"] if d else 0
        self._dbusservice[f"/Ac/{ phase }/Current"] = (
            d["power"] / d["voltage"] if d else 0
        )
        self._dbusservice[f"/Ac/{ phase }/Power"] = d["power"] if d else 0
        self._dbusservice[f"/Ac/{ phase }/Energy/Forward"] = (
            (d["total"] / 1000) if d else 0
        )
        self._dbusservice[f"/Ac/{ phase }/Energy/Reverse"] = (
            (d["total_returned"] / 1000) if d else 0
        )

    def _calculate_total(self):
        self._dbusservice["/Ac/Current"] = sum(
            [self._dbusservice[f"/Ac/{ ph }/Current"] for ph in PHASES]
        )
        self._dbusservice["/Ac/Power"] = sum(
            [self._dbusservice[f"/Ac/{ ph }/Power"] for ph in PHASES]
        )
        self._dbusservice["/Ac/Energy/Forward"] = sum(
            [self._dbusservice[f"/Ac/{ ph }/Energy/Forward"] for ph in PHASES]
        )
        self._dbusservice["/Ac/Energy/Reverse"] = sum(
            [self._dbusservice[f"/Ac/{ ph }/Energy/Reverse"] for ph in PHASES]
        )

    def _update(self):
        try:
            # get data from Shelly EM
            meter_data = self._getShellyData()

            for phase in PHASES:
                self._map_meter_data(
                    phase=phase,
                    d=meter_data["emeters"][0] if phase == self.phase else None,
                )

            self._calculate_total()

            # logging
            logging.debug(
                "House Consumption (/Ac/Power): %s" % (self._dbusservice["/Ac/Power"])
            )
            logging.debug(
                "House Forward (/Ac/Energy/Forward): %s"
                % (self._dbusservice["/Ac/Energy/Forward"])
            )
            logging.debug(
                "House Reverse (/Ac/Energy/Revers): %s"
                % (self._dbusservice["/Ac/Energy/Reverse"])
            )
            logging.debug("---")

            # increment UpdateIndex - to show that new data is available (value between 0 and 255)
            self._dbusservice["/UpdateIndex"] = (
                self._dbusservice["/UpdateIndex"] + 1
            ) % 256

            # update lastupdate vars
            self._lastUpdate = time.time()
        except Exception as e:
            logging.critical("Error at %s", "_update", exc_info=e)

        # return true, otherwise add_timeout will be removed from GObject - see docs http://library.isr.ist.utl.pt/docs/pygtk2reference/gobject-functions.html#function-gobject--timeout-add
        return True

    def _handlechangedvalue(self, path, value):
        logging.debug("someone else updated %s to %s" % (path, value))
        return True  # accept the change


def main():
    # configure logging
    logging.basicConfig(
        format="%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        level=logging.INFO,
        handlers=[
            logging.FileHandler(
                "%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))
            ),
            logging.StreamHandler(),
        ],
    )

    try:
        logging.info("Start")

        from dbus.mainloop.glib import DBusGMainLoop

        # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
        DBusGMainLoop(set_as_default=True)

        # formatting
        _kwh = lambda p, v: (str(round(v, 2)) + "KWh")
        _a = lambda p, v: (str(round(v, 1)) + "A")
        _w = lambda p, v: (str(round(v, 1)) + "W")
        _v = lambda p, v: (str(round(v, 1)) + "V")

        # start our main-service
        pvac_output = DbusShellyemService(
            servicename="com.victronenergy.grid",
            paths={
                "/Ac/Energy/Forward": {
                    "initial": 0,
                    "textformat": _kwh,
                },  # energy bought from the grid
                "/Ac/Energy/Reverse": {
                    "initial": 0,
                    "textformat": _kwh,
                },  # energy sold to the grid
                "/Ac/Power": {"initial": 0, "textformat": _w},
                "/Ac/Current": {"initial": 0, "textformat": _a},
                "/Ac/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L1/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L2/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L3/Voltage": {"initial": 0, "textformat": _v},
                "/Ac/L1/Current": {"initial": 0, "textformat": _a},
                "/Ac/L2/Current": {"initial": 0, "textformat": _a},
                "/Ac/L3/Current": {"initial": 0, "textformat": _a},
                "/Ac/L1/Power": {"initial": 0, "textformat": _w},
                "/Ac/L2/Power": {"initial": 0, "textformat": _w},
                "/Ac/L3/Power": {"initial": 0, "textformat": _w},
                "/Ac/L1/Energy/Forward": {"initial": 0, "textformat": _kwh},
                "/Ac/L2/Energy/Forward": {"initial": 0, "textformat": _kwh},
                "/Ac/L3/Energy/Forward": {"initial": 0, "textformat": _kwh},
                "/Ac/L1/Energy/Reverse": {"initial": 0, "textformat": _kwh},
                "/Ac/L2/Energy/Reverse": {"initial": 0, "textformat": _kwh},
                "/Ac/L3/Energy/Reverse": {"initial": 0, "textformat": _kwh},
            },
        )

        logging.info(
            "Connected to dbus, and switching over to gobject.MainLoop() (= event based)"
        )
        mainloop = gobject.MainLoop()
        mainloop.run()
    except Exception as e:
        logging.critical("Error at %s", "main", exc_info=e)


if __name__ == "__main__":
    main()
