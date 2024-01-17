#!/usr/bin/env python

"""
Author: Nick Russo (njrusmc@gmail.com)
Purpose: Utilize Wake On LAN (WOL) to power on devices in a
small home lab.
"""

import sys
from wakeonlan import send_magic_packet

def main(argv):
    """
    Execution starts here.
    """

    # Map short device name to WOL-enabled MAC address
    device_map = {
        "s1": "00-30-48-67-a2-a0",
        "s2": "00-30-48-d7-60-be",
        "nas": "00-11-32-3e-3a-c9",
    }

    # If no CLI args are supplied, power on all devices
    # Else, limit the devices to be powered on to CLI args
    if len(argv) < 2:
        devices_to_boot = device_map.keys()
    else:
        devices_to_boot = argv[1:]

    # Iterate over list of devices, waking up each one
    for device in devices_to_boot:
        mac = device_map.get(device.lower())
        if mac:
            print(f"Booting device {device} with MAC {mac}")
            send_magic_packet(mac)
        else:
            print(f"Unknown device {device}; choose {list(device_map.keys())}")

if __name__ == "__main__":
    main(sys.argv)
