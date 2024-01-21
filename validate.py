#!/usr/bin/env python

"""
Author: Nick Russo (njrusmc@gmail.com)
Purpose: Validates the Home lab network components.
"""

import asyncio
import json
import csv
import os
import logging
from io import StringIO
from scrapli.driver.core import AsyncIOSXEDriver
from scrapli.driver.generic import AsyncGenericDriver


def setup_logger(name, log_file, level=logging.INFO):
    """
    Given a logger name and file name, this function returns a new logger
    object to write into the specified file. You can absolutely specify the
    login level which defaults to INFO.
    """

    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    return logger


async def check(node, attr):
    """
    Coroutine that performs platform/device-specific checks on each node.
    It selects the correct scrapli driver and OS-specific verify coroutine.
    """
    # Map the textual platform names to the Srapli async driver constructors
    driver_map = {
        "ios": AsyncIOSXEDriver,
        "esxi": AsyncGenericDriver,
        "syno": AsyncGenericDriver,
    }

    # Map the textual platform names to the verify coroutines
    verify_map = {
        "ios": verify_ios,
        "esxi": verify_esxi,
        "syno": verify_syno,
    }

    # Remove and retain the non-scrapli "platform" option
    platform = attr.pop("platform")

    # Open a new connection with the proper driver and input attrs
    async with driver_map[platform](**attr) as conn:
        # Get the prompt and ensure the node name exists inside it
        prompt = await conn.get_prompt()
        assert node.lower() in prompt.lower()

        # Perform detailed checks on a given device using conn w/ logging
        logger = setup_logger(node, f"logs/{node}.txt")
        await verify_map[platform](conn, logger)


async def verify_ios(conn, logger):
    """
    Verify Cisco IOS devices.
    """

    async def _send_cmd_get_lod(cmd):
        """
        Send the specified command and return a list of dictionaries (lod)
        using textFSM. This relies on the network to code (NTC) templates.
        Both the text command and JSON output are logged.
        """
        logger.info('SEND > "%s"', cmd)
        resp = await conn.send_command(cmd)
        lod = resp.textfsm_parse_output()
        logger.info("REPLY > %s", json.dumps(lod, indent=2))
        return lod

    # Check for correct versions
    version = await _send_cmd_get_lod("show version")
    assert "15.2(4)e10" == version[0]["version"].lower()

    # Ensure the 4 infrastructure VLANs exist
    vlans = await _send_cmd_get_lod("show vlan")
    desired_vlans = {
        4000: "EXPERIMENT",
        4001: "INTERNET",
        4002: "MGMT",
        4003: "NAS",
    }
    for vlan in vlans:
        vlid = int(vlan["vlan_id"])
        if vlid in desired_vlans:
            assert vlan["name"] == desired_vlans.pop(vlid)
            assert vlan["status"] == "active"
    assert len(desired_vlans) == 0

    # Ensure port VLAN, descriptions, speed, and duplex are correct
    intf_up_map = {
        # MGMT
        1: ("4002", "ESXI1 NIC0"),
        2: ("4002", "ESXI2 NIC0"),
        3: ("4002", "RS814 LAN1"),
        4: ("4002", "LAB PC"),
        # NAS
        7: ("4003", "ESXI1 NIC1"),
        8: ("4003", "ESXI2 NIC1"),
        9: ("4003", "RS814 LAN2"),
        # PROD
        13: ("trunk", "ESXI1 NIC2"),
        14: ("trunk", "ESXI2 NIC2"),
        # INET (will be down if servers are off, no WOL)
        19: ("4001", "ESXI1 NIC3"),
        20: ("4001", "ESXI2 NIC3"),
    }
    intfs = await _send_cmd_get_lod("show interfaces status")
    for port_id, attr in intf_up_map.items():
        for intf in intfs:
            if intf["port"] == f"Gi1/0/{port_id}":
                vlan, desc = attr
                assert intf["vlan"] == vlan
                assert intf["name"].endswith(desc)
                assert intf["type"] == "10/100/1000BaseTX"
                assert intf["status"] == "connected"
                assert intf["duplex"] == "a-full"

    # RS-814 and laptop do not support CDP; remove those pairs then get CDP nbrs
    _ = [intf_up_map.pop(port_id) for port_id in [3, 4, 9]]
    cdp_nbrs = await _send_cmd_get_lod("show cdp neighbors")

    # Ensure each desired CDP neighbor is present on the switch
    assert len(intf_up_map) == len(cdp_nbrs) == 8

    for port_id, attr in intf_up_map.items():
        for cdp_nbr in cdp_nbrs:
            if cdp_nbr["local_interface"] == f"Gig 1/0/{port_id}":
                assert attr[1][:5] == cdp_nbr["neighbor"][:5].upper()
                assert "S" in cdp_nbr["capability"]
                assert cdp_nbr["platform"] == "VMware"
                assert attr[1][-4:] == cdp_nbr["neighbor_interface"][-4:].upper()

    # Ensure infrastructure SVIs are up (minus 4000)
    # Extra SVIs are OK; do not assert failure on default case
    svis = await _send_cmd_get_lod("show ip interface brief | exclude unassigned")
    for svi in svis:
        assert svi["status"] == svi["proto"] == "up"
        match svi["intf"]:
            case "Vlan4001":
                assert svi["ipaddr"] == "192.168.1.254"
            case "Vlan4002":
                assert svi["ipaddr"] == "192.168.2.254"
            case "Vlan4003":
                assert svi["ipaddr"] == "192.168.3.254"

    # Ensure there is one default route via the Internet gateway (home router)
    routes = await _send_cmd_get_lod("show ip route")
    defrte = [d for d in routes if d["network"] == "0.0.0.0" and d["mask"] == "0"]
    assert len(defrte) == 1
    assert defrte[0]["protocol"] == "S"
    assert defrte[0]["nexthop_ip"] == "192.168.1.1"
    assert defrte[0]["nexthop_if"] == "Vlan4001"


async def verify_esxi(conn, logger):
    """
    Verify VMware ESXi servers.
    """

    async def _send_cmd_get_lod(ns_cmd):
        """
        Send the specified command and return a list of dictionaries (lod)
        using textFSM. This relies on the network to code (NTC) templates.
        Both the text command and JSON output are logged. Note that the
        supplied command is prepending with "esxcli --formatter=csv" so this
        should not be included.
        """

        cmd = f"esxcli --formatter=csv {ns_cmd}"
        logger.info('SEND > "%s"', cmd)
        resp = await conn.send_command(cmd)
        matrix = list(csv.reader(StringIO(resp.result)))

        # LC makes it harder to remove {"": ""} binding if the rows
        # end in commas, which wastes memory and log space
        # lod = [dict(zip(matrix[0], row)) for row in matrix[1:]]
        lod = []
        for row in matrix[1:]:
            record = dict(zip(matrix[0], row))
            record.pop("", None)
            lod.append(record)
        logger.info("REPLY > %s", json.dumps(lod, indent=2))
        return lod

    # Get the system version and check against expected output
    version = await _send_cmd_get_lod("system version get")
    assert len(version) == 1
    assert version[0]["Build"] == "Releasebuild-3620759"

    # Get VMK details (MTU, portgroup assignments, etc)
    # The default case asserts failure to ensure no extra VMKs exist
    vmk_dets = await _send_cmd_get_lod("network ip interface list")
    assert len(vmk_dets) == 3
    for vmk_det in vmk_dets:
        assert vmk_det["Enabled"] == "true"
        assert vmk_det["MTU"] == "1500"
        assert vmk_det["NetstackInstance"] == "defaultTcpipStack"
        match vmk_det["Name"]:
            case "vmk0":
                assert vmk_det["Portgroup"] == "Mgmt_VMK"
                assert vmk_det["Portset"] == "Mgmt_vSwitch"  # was vSwitch0
            case "vmk1":
                assert vmk_det["Portgroup"] == "Internet_VMK"
                assert vmk_det["Portset"] == "Internet_vSwitch"
            case "vmk2":
                assert vmk_det["Portgroup"] == "Storage_VMK"
                assert vmk_det["Portset"] == "Storage_vSwitch"
            case _:
                assert False

    # Get VMK IPv4 details and check them
    vmk_ipv4s = await _send_cmd_get_lod("network ip interface ipv4 get")
    assert len(vmk_ipv4s) == 3
    for vmk_ipv4 in vmk_ipv4s:
        match vmk_ipv4["Name"]:
            case "vmk0":
                assert vmk_ipv4["AddressType"] == "STATIC"
                assert vmk_ipv4["DHCPDNS"] == "false"
                assert vmk_ipv4["IPv4Netmask"] == "255.255.255.0"
                assert vmk_ipv4["IPv4Address"].startswith("192.168.2.")
            case "vmk1":
                assert vmk_ipv4["AddressType"] == "DHCP"
                assert vmk_ipv4["DHCPDNS"] == "true"
                # assert vmk_ipv4["IPv4Address"].startswith("192.168.1.")
                # assert vmk_ipv4["IPv4Netmask"] = "255.255.255.0"
            case "vmk2":
                assert vmk_ipv4["AddressType"] == "STATIC"
                assert vmk_ipv4["DHCPDNS"] == "false"
                assert vmk_ipv4["IPv4Address"].startswith("192.168.3.")
                assert vmk_ipv4["IPv4Netmask"] == "255.255.255.0"
            case _:
                assert False

    # Ensure that the port groups are properly named, which is uniform
    # for VMK and VM portgroups
    pgs = await _send_cmd_get_lod("network vswitch standard portgroup list")
    for pg in pgs:
        assert pg["VirtualSwitch"].startswith(pg["Name"][: pg["Name"].find("_")])

    # Exactly one VMK PG per vSwitch, always untagged (VLAN 0)
    vmk_pgs = [pg for pg in pgs if pg["Name"].endswith("_VMK")]
    assert len(vmk_pgs) == 3
    for vmk_pg in vmk_pgs:
        assert vmk_pg["ActiveClients"] == "1"
        assert vmk_pg["VLANID"] == "0"

    # At least one VM PG per vSwitch, access (VLAN 0) or trunk (VLAN 4095)
    vm_pgs = [pg for pg in pgs if pg["Name"].endswith("_VM")]
    assert len(vm_pgs) >= 4
    for vm_pg in vm_pgs:
        if "trunk" in vm_pg["Name"].lower():
            assert vm_pg["VLANID"] == "4095"
        else:
            assert vm_pg["VLANID"] == "0"

    # Ensure all physical NICs are up and negotiated 1 Gbps/full duplex
    nics = await _send_cmd_get_lod("network nic list")
    assert len(nics) == 4
    for nic in nics:
        assert nic["Link"] == nic["LinkStatus"] == "Up"
        assert nic["Duplex"] == "Full"
        assert nic["MTU"] == "1500"
        assert nic["Speed"] == "1000"

    # Ensure that the 4 vSwitches are maped to the 4 physical NICs correctly.
    vmnic_vswitch_map = {
        0: "Mgmt_vSwitch",  # was vSwitch0
        1: "Storage_vSwitch",
        2: "Prod_vSwitch",
        3: "Internet_vSwitch",
    }
    vswitches = await _send_cmd_get_lod("network vswitch standard list")
    assert len(vswitches) == 4
    for i, vswitch in enumerate(vswitches):
        assert vswitch["Uplinks"][:-1] == f"vmnic{i}"
        assert vswitch["Name"] == vmnic_vswitch_map.pop(i)
    assert len(vmnic_vswitch_map) == 0

    # Get the NFS volume and ensure it's setup correctly
    nfs_vols = await _send_cmd_get_lod("storage nfs list")
    assert len(nfs_vols) == 1
    assert nfs_vols[0]["Host"] == "192.168.3.3"
    assert nfs_vols[0]["Share"] == "/volume1/vsphere_nfs"
    assert nfs_vols[0]["Accessible"] == "true"
    assert nfs_vols[0]["Mounted"] == "true"
    assert nfs_vols[0]["ReadOnly"] == "false"

    # Get the VMFS (disk) volumes and ensure they are named correctly
    vmfs_vols = await _send_cmd_get_lod(
        "storage filesystem list | egrep '(Free|_)'"
    )
    assert len(vmfs_vols) == 4
    for vmfs_vol in vmfs_vols:
        assert vmfs_vol["Mounted"] == "true"
        if "VMFS" in vmfs_vol["Type"]:
            assert vmfs_vol["VolumeName"].startswith("ESXi")

    # Ensure host firewall has enabled/disabled certain services
    fw_svc_map = {
        "sshServer": "true",
        "nfsClient": "true",
        "ftpClient": "true",
        "remoteSerialPort": "true",
        "webAccess": "true",
        "vSphereClient": "true",
        "WOL": "true",
        "sshClient": "false",
        "vMotion": "false",
        "vSPC": "false",
        "updateManager": "false",
        "esxupdate": "false",
        "vpxHeartbeats": "false",
        "DVSSync": "false",
        "faultTolerance": "false",
        "activeDirectoryAll": "false",
    }
    fw_rules = await _send_cmd_get_lod("network firewall ruleset list")
    for fw_rule in fw_rules:
        if fw_rule["Name"] in fw_svc_map:
            assert fw_rule["Enabled"] == fw_svc_map.pop(fw_rule["Name"])
    assert len(fw_svc_map) == 0


async def verify_syno(conn, logger):
    """
    Verify Synology storage systems (such as RackStation or DiskStation).
    """

    async def _send_cmd_get_txt(cmd):
        """
        Send the specified command and return the text output. There is
        no quick way to parse this data.
        """
        logger.info('SEND > "%s"', cmd)
        resp = await conn.send_command(cmd)
        logger.info("REPLY > %s", resp.result)
        return resp.result

    # Check for the correct version
    version = await _send_cmd_get_txt("cat /etc.defaults/VERSION")
    assert 'productversion="6.2.2"' in version

    # Check for the correct IP addresses and interfaces
    intf_ipv4_map = {
        "eth0": "192.168.2.3/24",
        "eth1": "192.168.3.3/24",
    }
    for intf, ipv4 in intf_ipv4_map.items():
        net = await _send_cmd_get_txt(f"ip addr show {intf}")
        assert ipv4 in net

    # Ensure there is a default route via the management gateway
    defrte = await _send_cmd_get_txt("ip route show 0.0.0.0/0")
    assert defrte.startswith("default via 192.168.2.254")

    # Become root for smartctl disk commands
    sudo = await conn.send_interactive(
        [
            ("sudo -i", "Password:", False),
            (conn.auth_password, "root@", True),
        ]
    )
    assert sudo.result.endswith(":~#")

    # Perform disk-related checks
    hdd_sn_map = {
        "sda": "WD-WCC4E4EE6XT9",
        "sdb": "WD-WCC4E3CP9J1R",
        "sdc": "WD-WCC4E3ZS9U9F",
        "sdd": "WD-WCC4E5UJ0HEF",
    }
    hdd_prefix = "smartctl --device sat"
    for hdd, sn in hdd_sn_map.items():
        # Check overall disk health
        health = await _send_cmd_get_txt(
            f"{hdd_prefix} --health /dev/{hdd} | grep ': '"
        )
        assert "test result: PASSED" in health

        # Check serial numbers and SMART support
        info = await _send_cmd_get_txt(
            f"{hdd_prefix} --info /dev/{hdd} | grep ': '"
        )
        assert f"Serial Number:    {sn}" in info
        assert "Device Model:     WDC WD40EFRX-68WT0N0" in info
        assert "SMART support is: Available" in info
        assert "SMART support is: Enabled" in info

        # Collect detailed attributes for more checks
        attrs = await _send_cmd_get_txt(
            rf"{hdd_prefix} --attributes --format brief /dev/{hdd} | egrep '^\s*[0-9+]'"
        )
        faults = ["Error", "Retry", "Reallocated", "Uncorrectable"]
        for attr in attrs.split("\n"):
            # Ensure none of the attributes report a failure
            s_attr = attr.strip().split()
            assert s_attr[6] == "-"

            # Any fault-like counter should be 0
            if any(fault in s_attr[1] for fault in faults):
                assert s_attr[7] == "0"

            # Check spin-up time, average is around 8100 ms for this device
            elif s_attr[0] == "3":
                assert int(s_attr[7]) < 8500

            # Check temperature, average max is 49 celsius for this device
            elif s_attr[0] == "194":
                assert int(s_attr[7]) < 50

    # Ensure there are 2 NFS client connections from each ESXi server
    nfs_conns = await _send_cmd_get_txt("netstat -tn | grep :2049")
    for esxi_ip in ["192.168.3.1", "192.168.3.2"]:
        assert esxi_ip in nfs_conns
    assert all(nc.strip().endswith("ESTABLISHED") for nc in nfs_conns.split("\n"))


async def main(config_file):
    """
    Execution starts here.
    """

    # Load config details from supplied file
    with open(config_file, "r", encoding="utf-8") as handle:
        config = json.load(handle)

    # Create the "logs" directory if it doesn't exist
    if not os.path.exists("logs"):
        os.makedirs("logs")

    # Instantiate coroutine into tasks for each node, merging in the login dict
    tasks = [
        check(node, attr | config["login"])
        for node, attr in config["nodes"].items()
    ]

    # Encapsulate all tasks in a future, then await concurrent completion
    task_future = asyncio.gather(*tasks)
    await task_future

    # Print simple message to indicate success
    print("OK")


if __name__ == "__main__":
    asyncio.run(main("config.json"))
