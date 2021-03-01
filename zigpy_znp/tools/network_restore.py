import sys
import json
import typing
import asyncio
import argparse

import zigpy_znp.types as t
import zigpy_znp.config as conf
from zigpy_znp.types.nvids import OsalNvIds
from zigpy_znp.tools.common import setup_parser
from zigpy_znp.znp.security import StoredDevice, write_devices, write_tc_frame_counter
from zigpy_znp.zigbee.application import ControllerApplication

BACKUP_VERSION = 1


async def restore_network(
    radio_path: str,
    backup: typing.Dict[str, typing.Any],
    counter_increment: int,
):
    backup_format = backup.get("metadata", {}).get("format")
    backup_version = backup.get("metadata", {}).get("version")

    if backup_format != "zigpy/open-coordinator-backup":
        raise ValueError(f"Backup format not recognized: {backup_format!r}")

    if backup_version != BACKUP_VERSION:
        raise ValueError(f"Backup format version is not compatible: {backup_version}")

    pan_id, _ = t.NWK.deserialize(bytes.fromhex(backup["pan_id"])[::-1])
    extended_pan_id, _ = t.EUI64.deserialize(
        bytes.fromhex(backup["extended_pan_id"])[::-1]
    )
    coordinator_ieee, _ = t.EUI64.deserialize(
        bytes.fromhex(backup["coordinator_ieee"])[::-1]
    )
    nwk_key, _ = t.KeyData.deserialize(bytes.fromhex(backup["network_key"]["key"]))

    devices = []

    for obj in backup["devices"]:
        nwk, _ = t.NWK.deserialize(bytes.fromhex(obj["nwk_address"])[::-1])
        ieee, _ = t.EUI64.deserialize(bytes.fromhex(obj["ieee_address"])[::-1])

        device = StoredDevice(nwk=nwk, ieee=ieee)

        if "link_key" in obj:
            key, _ = t.KeyData.deserialize(bytes.fromhex(obj["link_key"]["key"]))
            device = device.replace(
                aps_link_key=key,
                tx_counter=obj["link_key"]["tx_counter"],
                rx_counter=obj["link_key"]["rx_counter"],
            )

        devices.append(device)

    config = ControllerApplication.SCHEMA({"device": {"path": radio_path}})
    app = ControllerApplication(config)

    app.config[conf.CONF_NWK][conf.CONF_NWK_KEY] = nwk_key
    app.config[conf.CONF_NWK][conf.CONF_NWK_PAN_ID] = pan_id
    app.config[conf.CONF_NWK][conf.CONF_NWK_CHANNEL] = backup["channel"]
    app.config[conf.CONF_NWK][conf.CONF_NWK_EXTENDED_PAN_ID] = extended_pan_id

    await app.startup(force_form=True)

    znp = app._znp

    await znp.load_network_info()
    await znp.reset()

    await znp.nvram.osal_write(OsalNvIds.EXTADDR, coordinator_ieee)

    nib = await znp.nvram.osal_read(OsalNvIds.NIB, item_type=t.NIB)
    nib.channelList = t.Channels.from_channel_list(backup["channel_mask"])
    nib.nwkUpdateId = backup["nwk_update_id"]
    nib.SecurityLevel = backup["security_level"]
    await znp.nvram.osal_write(OsalNvIds.NIB, nib)

    tclk_seed = None

    if znp.version > 1.20:
        if backup.get("stack_specific", {}).get("zstack", {}).get("tclk_seed"):
            tclk_seed = bytes.fromhex(backup["stack_specific"]["zstack"]["tclk_seed"])
            await znp.nvram.osal_write(OsalNvIds.TCLK_SEED, tclk_seed)

    nwk_frame_counter = backup["network_key"]["frame_counter"]
    nwk_frame_counter += counter_increment

    key_info = t.NwkActiveKeyItems(
        Active=t.NwkKeyDesc(
            KeySeqNum=backup["network_key"]["sequence_number"],
            Key=nwk_key,
        ),
        FrameCounter=nwk_frame_counter,
    )

    await znp.nvram.osal_write(OsalNvIds.NWKKEY, key_info)
    await znp.nvram.osal_write(OsalNvIds.NWK_ACTIVE_KEY_INFO, key_info.Active)
    await znp.nvram.osal_write(OsalNvIds.NWK_ALTERN_KEY_INFO, key_info.Active)
    await write_tc_frame_counter(znp, nwk_frame_counter)

    await write_devices(
        znp, devices, seed=tclk_seed, counter_increment=counter_increment
    )

    await znp.reset()

    znp.close()


async def main(argv):
    parser = setup_parser("Restore adapter network settings")
    parser.add_argument(
        "--input", "-i", type=argparse.FileType("r"), help="Input file", required=True
    )
    parser.add_argument(
        "--counter-increment",
        "-c",
        type=t.uint32_t,
        help="Counter increment",
        default=2500,
    )
    args = parser.parse_args(argv)

    backup = json.load(args.input)

    await restore_network(
        radio_path=args.serial,
        backup=backup,
        counter_increment=args.counter_increment,
    )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))  # pragma: no cover
