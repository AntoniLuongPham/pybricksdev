# SPDX-License-Identifier: MIT
# Copyright (c) 2019-2020 The Pybricks Authors

import asyncio
from bleak import BleakClient
import logging

from pybricksdev.compile import (
    compile_argparser,
    compile_file,
    compile_str
)
from pybricksdev.connections import find_ble_device

bleNusCharRXUUID = '6e400002-b5a3-f393-e0a9-e50e24dcca9e'
bleNusCharTXUUID = '6e400003-b5a3-f393-e0a9-e50e24dcca9e'


class HubDataReceiver():

    UNKNOWN = 0
    IDLE = 1
    RUNNING = 2
    ERROR = 3
    CHECKING = 4

    def __init__(self, debug=False):
        self.buf = b''
        self.state = self.UNKNOWN
        self.reply = None

        # Get a logger
        self.logger = logging.getLogger('Hub Data')
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            '\t\t\t\t %(asctime)s: %(levelname)7s: %(message)s'
        )
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
        self.logger.setLevel(logging.DEBUG if debug else logging.INFO)

        # Data log state
        self.log_file = None

    def process_line(self, line):
        # Decode the output
        text = line.decode()

        # Output tells us to open a log file
        if 'PB_OF' in text:
            if self.log_file is not None:
                raise OSError("Log file is already open!")
            name = text[6:]
            self.logger.info("Saving log to {0}.".format(name))
            self.log_file = open(name, 'w')
            return

        # Enf of data log file, so close it
        if 'PB_EOF' in text:
            if self.log_file is None:
                raise OSError("No log file is currently open!")
            self.logger.info("Done saving log.")
            self.log_file.close()
            self.log_file = None
            return

        # We are processing datalog, so save this line
        if self.log_file is not None:
            print(text, file=self.log_file)
            self.logger.debug(text)
            return

        # If it is not special, just print it
        print(text)

    def update_data_buffer(self, sender, data):
        # If we are transmitting, the replies are checksums
        if self.state == self.CHECKING:
            self.reply = data[-1]
            self.logger.debug("\t\t\t\tCS: {0}".format(self.reply))
            return

        # Otherwise, append incoming data to buffer
        self.buf += data

        # Break up data into lines as soon as a line is complete
        while True:
            try:
                # Try to find line break and split there
                index = self.buf.index(b'\r\n')
                line = self.buf[0:index]
                self.buf = self.buf[index+2:]

                # Check line contents to see if state needs updating
                self.logger.debug("\t\t\t\tRX: {0}".format(line))

                # If the retrieved line is a state, update it
                if self.map_state(line) is not None:
                    self.update_state(self.map_state(line))

                # Process special lines else print as human readable
                self.process_line(line)
            # Exit the loop once no more line breaks are found
            except ValueError:
                break

    def map_state(self, line):
        """"Maps state strings to states."""
        if line == b'>>>> IDLE':
            return self.IDLE
        if line == b'>>>> RUNNING':
            return self.RUNNING
        if line == b'>>>> ERROR':
            return self.ERROR
        return None

    def update_state(self, new_state):
        """Updates state if data contains state information."""
        if new_state != self.state:
            self.logger.debug("New State: {0}".format(new_state))
            self.state = new_state

    def update_state_disconnected(self, client, *args):
        self.update_state(self.UNKNOWN)
        self.logger.info("Disconnected!")

    async def wait_for_checksum(self):
        self.update_state(self.CHECKING)
        for i in range(50):
            await asyncio.sleep(0.01)
            if self.reply is not None:
                reply = self.reply
                self.reply = None
                self.update_state(self.IDLE)
                return reply
        raise TimeoutError("Hub did not return checksum")

    async def wait_until_not_running(self):
        await asyncio.sleep(0.5)
        while True:
            await asyncio.sleep(0.1)
            if self.state != self.RUNNING:
                break


class PybricksHubConnection(HubDataReceiver):

    async def connect(self, address):
        self.logger.info("Connecting to {0}".format(address))
        self.client = BleakClient(address)
        await self.client.connect()
        self.client.set_disconnected_callback(self.update_state_disconnected)
        self.logger.info("Connected successfully!")
        await self.client.start_notify(
            bleNusCharTXUUID, self.update_data_buffer
        )

    async def disconnect(self):
        await self.client.stop_notify(bleNusCharTXUUID)
        await self.client.disconnect()

    async def write(self, data):
        n = 20
        chunks = [data[i: i + n] for i in range(0, len(data), n)]
        for i, chunk in enumerate(chunks):
            self.logger.debug("\t\t\t\tTX: {0}".format(chunk))
            await asyncio.sleep(0.05)
            await self.client.write_gatt_char(bleNusCharRXUUID, bytearray(chunk))

    async def send_message(self, data):
        """Send bytes to the hub, and check if reply matches checksum."""

        if len(data) > 100:
            raise ValueError("Cannot send this much data at once")

        # Compute expected reply
        checksum = 0
        for b in data:
            checksum ^= b

        # Send the data
        await self.write(data)

        # Await the reply
        reply = await self.wait_for_checksum()
        self.logger.debug("expected: {0}, reply: {1}".format(checksum, reply))

        # Raise errors if we did not get the checksum we wanted
        if reply is None:
            raise OSError("Did not receive reply.")

        if checksum != reply:
            raise ValueError("Did not receive expected checksum.")

    async def download_and_run(self, mpy):
        # Get length of file and send it as bytes to hub
        length = len(mpy).to_bytes(4, byteorder='little')
        await self.send_message(length)

        # Divide script in chunks of bytes
        n = 100
        chunks = [mpy[i: i + n] for i in range(0, len(mpy), n)]

        # Send the data chunk by chunk
        for i, chunk in enumerate(chunks):
            self.logger.info("Sending: {0}%".format(
                round((i+1)/len(chunks)*100))
            )
            await self.send_message(chunk)

        # Wait for the program to finish
        await self.wait_until_not_running()


if __name__ == "__main__":
    # Add arguments to the base parser, then parse
    parser = compile_argparser
    parser.description = (
        "Run Pybricks MicroPython scripts via BLE."
    )
    args = parser.parse_args()

    # Convert either the file or the string to mpy format
    if args.file is not None:
        data = compile_file(args.file, args.mpy_cross)
    else:
        data = compile_str(args.string, args.mpy_cross)

    async def main(mpy):

        print("Scanning for Pybricks Hub")
        address = await find_ble_device('Pybricks Hub', timeout=5)
        print("Found {0}!".format(address))

        hub = PybricksHubConnection(debug=False)
        await hub.connect(address)
        await hub.download_and_run(mpy)

    # Asynchronously send and run the script
    asyncio.run(main(data))
