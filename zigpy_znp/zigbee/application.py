import os
import logging

import zigpy.types
import zigpy.application

import zigpy.zdo.types as zdo_t
from zigpy.types import ExtendedPanId

import zigpy_znp.types as t
import zigpy_znp.commands as c

from zigpy_znp.types.nvids import NwkNvIds
from zigpy_znp.commands.zdo import StartupState
from zigpy_znp.commands.types import DeviceState


LOGGER = logging.getLogger(__name__)


class ControllerApplication(zigpy.application.ControllerApplication):
    def __init__(self, api, database_file=None):
        super().__init__(database_file=database_file)
        self._api = api
        api.set_application(self)

        self._api.callback_for_response(
            c.AFCommands.IncomingMsg.Callback(partial=True), self.on_af_message
        )

    def on_af_message(self, msg):
        if msg.ClusterId == zdo_t.ZDOCmd.Device_annce and msg.DstEndpoint == 0:
            # [Sequence Number] + [16-bit address] + [64-bit address] + [Capability]
            sequence, data = t.uint8_t.deserialize(msg.Data)
            nwk, data = t.NWK.deserialize(data)
            ieee, data = t.EUI64.deserialize(data)
            capability = data

            LOGGER.info("ZDO Device announce: 0x%04x, %s, %s", nwk, ieee, capability)
            self.handle_join(nwk, ieee, parent_nwk=0x0000)

        try:
            device = self.get_device(nwk=msg.SrcAddr)
        except KeyError:
            LOGGER.warning(
                "Received an AF message from an unknown device: 0x%04x", msg.SrcAddr
            )
            return

        device.radio_details(lqi=msg.LQI, rssi=None)

        self.handle_message(
            sender=device,
            profile=zigpy.profiles.zha.PROFILE_ID,
            cluster=msg.ClusterId,
            src_ep=msg.SrcEndpoint,
            dst_ep=msg.DstEndpoint,
            message=msg.Data,
        )

    async def shutdown(self):
        """Shutdown application."""
        self._api.close()

    async def startup(self, auto_form=False):
        """Perform a complete application startup"""
        should_form = [False]

        if auto_form and any(should_form):
            await self.form_network()

        startup_rsp = await self._api.command(
            c.ZDOCommands.StartupFromApp.Req(StartDelay=100)
        )

        if startup_rsp.State == StartupState.NotStarted:
            raise RuntimeError("Network failed to start")

        # await self._api.wait_for_response(
        #    c.ZDOCommands.StateChangeInd.Callback(State=DeviceState.StartedAsCoordinator)
        # )

    async def form_network(self, channel=15, pan_id=None, extended_pan_id=None):
        # These options are read only on startup so we perform a soft reset right after
        await self._api.nvram_write(
            NwkNvIds.STARTUP_OPTION, t.StartupOptions.ClearState
        )
        await self._api.nvram_write(
            NwkNvIds.LOGICAL_TYPE, t.DeviceLogicalType.Coordinator
        )
        await self._api.command(c.SysCommands.ResetReq.Req(Type=t.ResetType.Soft))

        # If zgPreConfigKeys is set to TRUE, all devices should use the same
        # pre-configured security key. If zgPreConfigKeys is set to FALSE, the
        # pre-configured key is set only on the coordinator device, and is handed to
        # joining devices. The key is sent in the clear over the last hop. Upon reset,
        # the device will retrieve the pre-configured key from NV memory if the NV_INIT
        # compile option is defined (the NV item is called ZCD_NV_PRECFGKEY).
        network_key = zigpy.types.KeyData(os.urandom(16))
        await self._api.nvram_write(NwkNvIds.PRECFGKEY, network_key)
        await self._api.nvram_write(NwkNvIds.PRECFGKEYS_ENABLE, zigpy.types.bool(True))

        channel_mask = t.Channels.from_channels([channel])
        await self._api.nvram_write(NwkNvIds.CHANLIST, channel_mask)

        # Receive verbose callbacks
        await self._api.nvram_write(NwkNvIds.ZDO_DIRECT_CB, zigpy.types.bool(True))

        # 0xFFFF means "don't care", according to the documentation
        pan_id = t.PanId(0xFFFF if pan_id is None else pan_id)
        await self._api.nvram_write(NwkNvIds.PANID, pan_id)

        extended_pan_id = ExtendedPanId(
            os.urandom(8) if extended_pan_id is None else extended_pan_id
        )
        await self._api.nvram_write(NwkNvIds.EXTENDED_PAN_ID, extended_pan_id)

        await self._api.command(
            c.APPConfigCommands.BDBSetChannel(IsPrimary=True, Channel=channel_mask)
        )
        await self._api.command(
            c.APPConfigCommands.BDBSetChannel(
                IsPrimary=False, Channel=t.Channels.NO_CHANNELS
            )
        )

        await self._api.command(
            c.APPConfigCommands.BDBStartCommissioning(
                Mode=t.BDBCommissioningMode.NetworkFormation
            )
        )

        # This may take a while because of some sort of background scanning.
        # This can probably be disabled.
        await self._api.wait_for_response(
            c.ZDOCommands.StateChangeInd.Rsp(State=DeviceState.StartedAsCoordinator)
        )

        await self._api.command(
            c.APPConfigCommands.BDBStartCommissioning(
                Mode=t.BDBCommissioningMode.NetworkSteering
            )
        )

    async def request(
        self,
        device,
        profile,
        cluster,
        src_ep,
        dst_ep,
        sequence,
        data,
        expect_reply=True,
        use_ieee=False,
    ):
        """Submit and send data out as an unicast transmission.
        :param device: destination device
        :param profile: Zigbee Profile ID to use for outgoing message
        :param cluster: cluster id where the message is being sent
        :param src_ep: source endpoint id
        :param dst_ep: destination endpoint id
        :param sequence: transaction sequence number of the message
        :param data: Zigbee message payload
        :param expect_reply: True if this is essentially a request
        :param use_ieee: use EUI64 for destination addressing
        :returns: return a tuple of a status and an error_message. Original requestor
                  has more context to provide a more meaningful error message
        """

        if use_ieee:
            raise ValueError("use_ieee: AFCommands.DataRequestExt is not supported yet")

        tx_options = c.af.TransmitOptions.NONE

        # if expect_reply:
        #    tx_options |= c.af.TransmitOptions.APSAck

        data_request = c.AFCommands.DataRequest.Req(
            DstAddr=device.nwk,
            DstEndpoint=dst_ep,
            SrcEndpoint=src_ep,
            ClusterId=cluster,
            TSN=sequence,
            Options=tx_options,
            Radius=30,
            Data=data,
        )
        response = await self._api.command(data_request)

        # XXX: sometimes routes need to be re-discovered
        if response.Status == t.Status.NwkNoRoute:
            LOGGER.warning(
                "No route to %s. Forcibly discovering a route and re-sending request",
                device.nwk,
            )

            await self._api.command(
                c.ZDOCommands.ExtRouteDisc.Req(
                    Dst=device.ieee,
                    Options=c.zdo.RouteDiscoveryOptions.Force,
                    Radius=2 * 0x0F,
                )
            )

            response = await self._api.command(data_request)

            if response.Status != t.Status.Success:
                return (
                    response.Status,
                    "Failed to send a message after discovering route",
                )

        response = await self._api.wait_for_response(
            c.AFCommands.DataConfirm.Callback(
                partial=True, Endpoint=dst_ep, TSN=sequence
            )
        )

        LOGGER.info("Received a data request confirmation: %s", response)

        if response.Status != t.Status.Success:
            return response.Status, "Invalid response status"

        return response.Status, "Request sent successfully"

    async def mrequest(
        self,
        group_id,
        profile,
        cluster,
        src_ep,
        sequence,
        data,
        *,
        hops=0,
        non_member_radius=3
    ):
        """Submit and send data out as a multicast transmission.
        :param group_id: destination multicast address
        :param profile: Zigbee Profile ID to use for outgoing message
        :param cluster: cluster id where the message is being sent
        :param src_ep: source endpoint id
        :param sequence: transaction sequence number of the message
        :param data: Zigbee message payload
        :param hops: the message will be delivered to all nodes within this number of
                     hops of the sender. A value of zero is converted to MAX_HOPS
        :param non_member_radius: the number of hops that the message will be forwarded
                                  by devices that are not members of the group. A value
                                  of 7 or greater is treated as infinite
        :returns: return a tuple of a status and an error_message. Original requestor
                  has more context to provide a more meaningful error message
        """
        raise NotImplementedError()

    async def broadcast(
        self,
        profile,
        cluster,
        src_ep,
        dst_ep,
        grpid,
        radius,
        sequence,
        data,
        broadcast_address=zigpy.types.BroadcastAddress.RX_ON_WHEN_IDLE,
    ):
        """Submit and send data out as an broadcast transmission.
        :param profile: Zigbee Profile ID to use for outgoing message
        :param cluster: cluster id where the message is being sent
        :param src_ep: source endpoint id
        :param dst_ep: destination endpoint id
        :param grpid: group id to address the broadcast to
        :param radius: max radius of the broadcast
        :param sequence: transaction sequence number of the message
        :param data: zigbee message payload
        :param broadcast_address: broadcast address.
        :returns: return a tuple of a status and an error_message. Original requestor
                  has more context to provide a more meaningful error message
        """

        raise NotImplementedError()

    async def force_remove(self, dev):
        """Forcibly remove device from NCP."""
        raise NotImplementedError()

    async def permit_ncp(self, time_s):
        response = await self._api.request(
            c.ZDOCommands.MgmtPermitJoinReq.Req(
                AddrMode=0x0F,  # docs say 0xFF for broadcast, everybody uses 0x0F ???
                DstAddr=zigpy.types.BroadcastAddress.ALL_DEVICES,
                Duration=time_s,
                TCSignificance=0,
            )
        )

        if response.Status != t.Status.Success:
            raise ValueError(
                "Permit join request failed with status: %s", response.Status
            )
