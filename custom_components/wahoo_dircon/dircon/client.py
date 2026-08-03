import asyncio

import logging

from . import protocol

_LOGGER = logging.getLogger(__name__)

DC_STATUS_DISCONNECTED = 0
DC_STATUS_CONNECTING = 1
DC_STATUS_CONFIGURING = 2
DC_STATUS_CONNECTED = 3

class DirconTcpClient:
    def __init__(self, host: str, port: int):
        self._host = host
        self._port = port

        self._status = DC_STATUS_DISCONNECTED

        self._reader = None
        self._writer = None

        self._seq = 0

        self._chr_listeners = []
        self._status_listeners = []

    def add_chr_listener(self, callback):
        self._chr_listeners.append(callback)

    def add_status_listener(self, callback):
        self._status_listeners.append(callback)

    def _set_status(self, status: int):
        self._status = status
        for l in self._status_listeners:
            l(self._status)

    @property
    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    async def _async_read_packet(self) -> protocol.DirconPacket:
        try:
            header = await self._reader.readexactly(protocol.DPKT_MESSAGE_HEADER_LENGTH)
            body_len = int.from_bytes(header[4:6], 'big')
            body = await self._reader.readexactly(body_len) if body_len else b""
        except asyncio.IncompleteReadError:
            _LOGGER.debug("_async_read_packet(): connection closed while reading")
            header = bytes([0x01, protocol.DPKT_MSGID_ERROR, 0x00, protocol.DPKT_RESPCODE_UNEXPECTED_ERROR, 0x00, 0x00])
            body = b""
        return protocol.DirconPacket().parse_response(header, body)

    async def _async_write_packet(self, packet: protocol.DirconPacket):
        self._writer.write(packet.serialize_request())
        await self._writer.drain()
    
    async def _async_configure(self, read_chrs: list, notify_chrs: list) -> list | None:
        disc = protocol.DirconPacket().build(protocol.DPKT_MSGID_DISCOVER_SERVICES, seq = self._next_seq)
        await self._async_write_packet(disc)

        resp = await self._async_read_packet()
        if not resp.is_success():
            _LOGGER.warning(f"_async_configure(): Failed to discover services")
            return None

        result = []

        for uuid in resp._uuids:
            _LOGGER.debug(f"_async_configure(): Discovered service: 0x{uuid:x}")
            disc_chr = protocol.DirconPacket().build(protocol.DPKT_MSGID_DISCOVER_CHARACTERISTICS, seq = self._next_seq, uuids = [uuid])
            await self._async_write_packet(disc_chr)
            resp = await self._async_read_packet()
            if not resp.is_success():
                _LOGGER.warning(f"_async_configure(): Failed to discover characteristics of 0x{uuid:x}")
                return None
            for i in range(len(resp._uuids)):
                ch_uuid = resp._uuids[i]
                ch_flag = resp._data[i];
                _LOGGER.debug(f"_async_configure(): Discovered char: 0x{ch_uuid:x}, {resp._data}")
                if ch_uuid in read_chrs and ch_flag == 1:
                    _LOGGER.debug(f"_async_configure(): Request read: 0x{ch_uuid:x}")
                    read_chr = protocol.DirconPacket().build(protocol.DPKT_MSGID_READ_CHARACTERISTIC, seq = self._next_seq, uuids = [ch_uuid])
                    result.append(read_chr)
                if ch_uuid in notify_chrs and ch_flag == 4:
                    _LOGGER.debug(f"_async_configure(): Request notify: 0x{ch_uuid:x}")
                    notify_chr = protocol.DirconPacket().build(protocol.DPKT_MSGID_ENABLE_CHARACTERISTIC_NOTIFICATIONS, seq = self._next_seq, uuids = [ch_uuid])
                    result.append(notify_chr)

        return result

    async def async_write(self, uuid: int, data: bytearray) -> bool:
        if self._status != DC_STATUS_CONNECTED:
            _LOGGER.info(f"async_write(): Skip writing as not connected")
            return False
        try:
            req = protocol.DirconPacket().build(
                protocol.DPKT_MSGID_WRITE_CHARACTERISTIC, 
                seq = self._next_seq,
                uuids = [uuid],
                data = data
            )
            _LOGGER.debug(f"async_write(): 0x{uuid:x} {data.hex(':')}")
            await self._async_write_packet(req)

            # resp = await self._async_read_packet()
            # _LOGGER.debug(f"async_write(): Write result: {resp._data.hex(':')}")
            # return resp._data
            return True

        except OSError as ex:
            _LOGGER.warning("async_write(): Failed to write: %s", ex)
            return False
        except Exception:
            _LOGGER.exception("async_write(): Failed to write")
            return False

    async def async_close(self):
        if self._status == DC_STATUS_DISCONNECTED or self._writer is None:
            _LOGGER.info(f"async_close(): Skip closing as not connected")
            return
        try:
            self._writer.close()
            await self._writer.wait_closed()
            _LOGGER.info(f"async_close(): Closed connection")
        except OSError as ex:
            _LOGGER.warning("async_close(): Failed to close: %s", ex)
        except Exception:
            _LOGGER.exception("async_close(): Failed to close")


    async def async_run(self, read_chrs: list, notify_chrs: list, listen: bool = False) -> bool:
        try:
            self._set_status(DC_STATUS_CONNECTING)
            self._reader, self._writer = await asyncio.open_connection(self._host, self._port)

            _LOGGER.debug(f"async_run(): TCP connection opened")
            self._set_status(DC_STATUS_CONFIGURING)
            
            commands = await self._async_configure(read_chrs, notify_chrs)

            if commands:
                self._set_status(DC_STATUS_CONNECTED)
                index = 0
                while True:
                    if index < len(commands):
                        _LOGGER.debug(f"async_run(): Sending next command")
                        await self._async_write_packet(commands[index])
                        index += 1
                    else:
                        if not listen:
                            break
                    resp = await self._async_read_packet()
                    if not resp.is_success():
                        _LOGGER.warning(f"async_run(): Invalid response received: 0x{resp._code:x}")
                        break
                    if not resp._uuids:
                        # e.g. bare acknowledgements with no characteristic UUID
                        _LOGGER.debug(f"async_run(): Skipping message with no UUID: 0x{resp._id:x}")
                        continue
                    _LOGGER.debug(f"async_run(): Process message: 0x{resp._id:x} 0x{resp._uuids[0]:x}: {resp._data.hex(':')}")
                    for _l in self._chr_listeners:
                        _l(resp._uuids[0], resp._data, resp._id)
            self._set_status(DC_STATUS_DISCONNECTED)
            self._writer.close()
            await self._writer.wait_closed()
            return True if commands else False

        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as ex:
            _LOGGER.warning("async_run(): Connection to %s:%s failed: %s", self._host, self._port, ex)
            self._set_status(DC_STATUS_DISCONNECTED)
            return False
        except asyncio.CancelledError:
            self._set_status(DC_STATUS_DISCONNECTED)
            raise
        except Exception:
            _LOGGER.exception("async_run(): Unexpected error talking to %s:%s", self._host, self._port)
            self._set_status(DC_STATUS_DISCONNECTED)
            return False
        finally:
            if self._writer is not None:
                self._writer.close()
            self._reader = None
            self._writer = None
