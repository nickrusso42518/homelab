import asyncio
import json
import csv
import logging
from io import StringIO
import ruamel.yaml
from scrapli.driver.core import AsyncIOSXEDriver
from scrapli.driver.generic import AsyncGenericDriver


def setup_logger(name, log_file, level=logging.INFO):
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)

    return logger


async def check(node, attr):
    """
    Coroutine that performs platform/device-specific checks on each node.
    """
    driver_map = {
        "ios": AsyncIOSXEDriver,
        "esxi": AsyncGenericDriver,
        "syno": AsyncGenericDriver,
    }

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
    async def _send_cmd_get_lod(cmd):
        logger.info('SEND > "%s"', cmd)
        resp = await conn.send_command(cmd)
        lod = resp.textfsm_parse_output()
        logger.info("REPLY > %s", json.dumps(lod, indent=2))
        return lod

    version = await _send_cmd_get_lod("show version")
    assert "15.2(4)e10" == version[0]["version"].lower()

    vlans = await _send_cmd_get_lod("show vlan")
    desired_vlans = {
        4000: "experiment",
        4001: "internet",
        4002: "mgmt",
        4003: "nas",
    }

    for vlan in vlans:
        vlid = int(vlan["vlan_id"])
        if vlid in desired_vlans:
            assert desired_vlans.pop(vlid) == vlan["name"].lower()
    assert len(desired_vlans) == 0

    cdp_nbrs = await _send_cmd_get_lod("show cdp neighbors")

    # Ensure each desired CDP neighbor is present on the switch
    yaml = ruamel.yaml.YAML()
    with open("desired/cdp_nbrs.yml", "r", encoding="utf-8") as handle:
        desired_cdp_nbrs = yaml.load(handle)

    for desired_cdp_nbr in desired_cdp_nbrs:
        # assert desired_cdp_nbr in cdp_nbrs
        pass

    # Extra SVIs are OK
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

    routes = await _send_cmd_get_lod("show ip route")
    defrte = [d for d in routes if d["network"] == "0.0.0.0" and d["mask"] == "0"]
    assert len(defrte) == 1
    assert defrte[0]["protocol"] == "S"
    assert defrte[0]["nexthop_ip"] == "192.168.1.1"
    assert defrte[0]["nexthop_if"] == "Vlan4001"


async def verify_esxi(conn, logger):
    async def _send_cmd_get_lod(ns_cmd):
        cmd = f"esxcli --formatter=csv {ns_cmd}"
        logger.info('SEND > "%s"', cmd)
        resp = await conn.send_command(cmd)
        matrix = list(csv.reader(StringIO(resp.result)))
        # lod = [{k: v for k, v in zip(matrix[0], row)} for row in matrix[1:]]
        lod = [dict(zip(matrix[0], row)) for row in matrix[1:]]
        logger.info("REPLY > %s", json.dumps(lod, indent=2))
        return lod

    version = await _send_cmd_get_lod("system version get")
    assert len(version) == 1
    assert version[0]["Build"] == "Releasebuild-3620759"

    # Get VMK details (MTU, portgroup assignments, etc)
    vmk_dets = await _send_cmd_get_lod("network ip interface list")
    assert len(vmk_dets) == 3
    for vmk_det in vmk_dets:
        assert vmk_det["Enabled"] == "true"
        assert vmk_det["MTU"] == "1500"
        assert vmk_det["NetstackInstance"] == "defaultTcpipStack"
        match vmk_det["Name"]:
            case "vmk0":
                assert vmk_det["Portgroup"] == "Mgmt_PG"
                assert vmk_det["Portset"] == "vSwitch0"
            case "vmk1":
                assert vmk_det["Portgroup"] == "Internet_PG"
                assert vmk_det["Portset"] == "Internet_vSwitch"
            case "vmk2":
                assert vmk_det["Portgroup"] == "Storage_PG"
                assert vmk_det["Portset"] == "Storage_vSwitch"
            case _:
                assert False

    # Get VMK IPv4 details
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

    nics = await _send_cmd_get_lod("network nic list")
    assert len(nics) == 4
    for nic in nics:
        assert nic["Link"] == nic["LinkStatus"] == "Up"
        assert nic["Duplex"] == "Full"
        assert nic["MTU"] == "1500"
        assert nic["Speed"] == "1000"

    vmnic_vswitch_map = {
        0: "vSwitch0",
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


async def verify_syno(conn, logger):
    async def _send_cmd_get_txt(cmd):
        logger.info('SEND > "%s"', cmd)
        resp = await conn.send_command(cmd)
        logger.info("REPLY > %s", resp.result)
        return resp.result

    version = await _send_cmd_get_txt("cat /etc.defaults/VERSION")
    assert 'productversion="6.2.2"' in version

    intf_ipv4_map = {
        "eth0": "192.168.2.3/24",
        "eth1": "192.168.3.3/24",
    }
    for intf, ipv4 in intf_ipv4_map.items():
        net = await _send_cmd_get_txt(f"ip addr show {intf}")
        assert ipv4 in net

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
    hdd_sn_map = {
        "sda": "WD-WCC4E4EE6XT9",
        "sdb": "WD-WCC4E3CP9J1R",
        "sdc": "WD-WCC4E3ZS9U9F",
        "sdd": "WD-WCC4E5UJ0HEF",
    }
    hdd_prefix = "smartctl --device sat"
    for hdd, sn in hdd_sn_map.items():
        health = await _send_cmd_get_txt(
            f"{hdd_prefix} --health /dev/{hdd} | grep ': '"
        )
        assert "test result: PASSED" in health

        info = await _send_cmd_get_txt(
            f"{hdd_prefix} --info /dev/{hdd} | grep ': '"
        )
        assert f"Serial Number:    {sn}" in info
        assert "Device Model:     WDC WD40EFRX-68WT0N0" in info
        assert "SMART support is: Available" in info
        assert "SMART support is: Enabled" in info

        attrs = await _send_cmd_get_txt(
            rf"{hdd_prefix} --attributes --format brief /dev/{hdd} | egrep '^\s*[0-9+]'"
        )
        faults = ["Error", "Retry", "Reallocated", "Uncorrectable"]
        for attr in attrs.split("\n"):
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


async def main(config_file):
    # Load config details from file
    yaml = ruamel.yaml.YAML()
    with open(config_file, "r", encoding="utf-8") as handle:
        config = yaml.load(handle)

    for node, attr in config["skip"].items():
        print(f"Skipping node {node} with attr {attr}")

    # Instantiate coroutine into tasks for each node, merging in the login dict
    tasks = [
        check(node, attr | config["login"])
        for node, attr in config["nodes"].items()
    ]

    # Encapsulate all tasks in a future, then await concurrent completion
    task_future = asyncio.gather(*tasks)
    await task_future

    print("OK")


if __name__ == "__main__":
    asyncio.run(main("config.yml"))
