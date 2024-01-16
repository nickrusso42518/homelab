import asyncio
import ruamel.yaml
from scrapli.driver.core import AsyncIOSXEDriver
from scrapli.driver.generic import AsyncGenericDriver
from io import StringIO
import csv

async def verify_ios(conn):
    resp = await conn.send_command("show version")
    version = resp.textfsm_parse_output()
    assert "15.2(4)e10" == version[0]["version"].lower()

    resp = await conn.send_command("show vlan")
    vlans = resp.textfsm_parse_output()
 
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

    resp = await conn.send_command("show cdp neighbors")
    cdp_nbrs = resp.textfsm_parse_output()
    
    # Ensure each desired CDP neighbor is present on the switch
    yaml = ruamel.yaml.YAML()
    with open("desired/cdp_nbrs.yml", "r", encoding="utf-8") as handle:
        desired_cdp_nbrs = yaml.load(handle)

    for desired_cdp_nbr in desired_cdp_nbrs:
        assert desired_cdp_nbr in cdp_nbrs

async def verify_esxi(conn):

    async def _send_cmd_get_lod(ns_cmd):
        cmd_prefix = "esxcli --formatter=csv"
        resp = await conn.send_command(f"{cmd_prefix} {ns_cmd}")
        matrix = list(csv.reader(StringIO(resp.result)))
        print(matrix)
        
        return [{k: v for k, v in zip(matrix[0], row)} for row in matrix[1:]]

    version = await _send_cmd_get_lod("system version get")
    assert len(version) == 1
    assert version[0]["version"] == "Build123"

    # Get VMK details (MTU, portgroup assignments, etc)
    vmk_det = await _send_cmd_get_lod("")
    assert len(vmk_det) == 3
    assert vmk_det

    # Get VMK IPv4 details
    vmk_ipv4 = await _send_cmd_get_lod("")
    assert len(vmk_ipv4) == 3
    assert vmk_ipv4

async def verify_syno(conn):
    print("syno")

DRIVER_MAP = {
  "ios": AsyncIOSXEDriver,
  "esxi": AsyncGenericDriver,
  "syno": AsyncGenericDriver,
}

VERIFY_MAP = {
  "ios": verify_ios,
  "esxi": verify_esxi,
  "syno": verify_syno,
}

async def check(node, attr):
    """
    Coroutine that performs platform/device-specific checks on each node.
    """

    # Remove and retain the non-scrapli "platform" option
    platform = attr.pop("platform")

    # Open a new connection with the proper driver and input attrs
    async with DRIVER_MAP[platform](**attr) as conn:

        # Get the prompt and ensure the node name exists inside it
        prompt = await conn.get_prompt()
        assert node.lower() in prompt.lower()

        # Perform detail checks on a given device using the open connection
        await VERIFY_MAP[platform](conn)


async def main():

    # Load config details from file
    yaml = ruamel.yaml.YAML()
    with open("config.yml", "r", encoding="utf-8") as handle:
      config = yaml.load(handle)
    
    # Instantiate coroutine into tasks for each node, merging in the login dict
    tasks = [
        check(node, attr | config["login"])
        for node, attr in config["nodes"].items()
    ]

    # Encapsulate all tasks in a future, then await concurrent completion
    task_future = asyncio.gather(*tasks)
    await task_future


if __name__ == "__main__":
    asyncio.run(main())
