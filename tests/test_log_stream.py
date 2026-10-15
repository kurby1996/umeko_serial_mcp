"""Synthetic streaming tests: no physical serial ports are opened."""
import asyncio
import json
import unittest
from unittest.mock import patch
import websockets

from umeko_serial_mcp.hub import hub, WS_BATCH_MAX, _ws_handler


class Viewer:
    def __init__(self, receipt, acknowledge=True):
        self.receipt = receipt
        self.acknowledge = acknowledge
        self.messages = []
        self.closed = None

    async def send(self, payload):
        self.messages.append(json.loads(payload))
        if self.acknowledge:
            self.receipt.set()

    async def close(self, **kwargs):
        self.closed = kwargs


class StreamingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        hub.clear_logs()

    async def wait_messages(self, viewer, count):
        async with asyncio.timeout(2):
            while len(viewer.messages) < count:
                await asyncio.sleep(0.005)

    async def test_burst_skips_to_latest_and_mcp_is_independent(self):
        start = hub._seq
        for i in range(20000):
            hub.append_log("HARDWARE", str(i), hex_msg="41")
        receipt = asyncio.Event()
        viewer = Viewer(receipt)
        task = asyncio.create_task(hub.stream_logs(viewer, start, receipt))
        try:
            await self.wait_messages(viewer, 1)
            batch = viewer.messages[0]
            self.assertEqual(len(batch["items"]), WS_BATCH_MAX)
            self.assertEqual(batch["skipped"], 20000 - WS_BATCH_MAX)
            self.assertEqual(batch["items"][-1]["msg"], "19999")
            self.assertEqual(batch["items"][-1]["hex"], "41")
            # Web viewers don't consume the shared ring used by MCP.
            self.assertEqual(len(hub.buffer), 5000)
            self.assertEqual(hub.read_since(start)["items"][0]["msg"], "15000")
            hub.append_log("HARDWARE", "after burst")
            await self.wait_messages(viewer, 2)
            self.assertEqual(viewer.messages[1]["items"][0]["msg"], "after burst")
            await asyncio.sleep(0.15)
            self.assertEqual(len(viewer.messages), 2)  # No replay after input stops.
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_unresponsive_viewer_has_one_batch_in_flight(self):
        start = hub._seq
        hub.append_log("HARDWARE", "first")
        receipt = asyncio.Event()
        viewer = Viewer(receipt, acknowledge=False)
        with patch("umeko_serial_mcp.hub.WS_ACK_TIMEOUT", 0.15):
            task = asyncio.create_task(hub.stream_logs(viewer, start, receipt))
            await self.wait_messages(viewer, 1)
            for i in range(10000):
                hub.append_log("HARDWARE", str(i))
            await asyncio.wait_for(task, 2)
        self.assertEqual(len(viewer.messages), 1)
        self.assertEqual(viewer.closed["code"], 1013)

    async def test_clear_does_not_replay_old_logs(self):
        start = hub._seq
        hub.append_log("HARDWARE", "old")
        hub.clear_logs()
        hub.append_log("HARDWARE", "new")
        receipt = asyncio.Event()
        viewer = Viewer(receipt)
        task = asyncio.create_task(hub.stream_logs(viewer, start, receipt))
        try:
            await self.wait_messages(viewer, 1)
            self.assertEqual([x["msg"] for x in viewer.messages[0]["items"]], ["new"])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_websocket_history_ack_controls_and_clear(self):
        async def receive_type(client, kind):
            async with asyncio.timeout(2):
                while True:
                    data = json.loads(await client.recv())
                    if data.get("type") == kind:
                        return data

        hub.append_log("HARDWARE", "history")
        with patch.object(hub, "enumerate_ports", return_value=[]):
            async with websockets.serve(_ws_handler, "127.0.0.1", 0) as server:
                hub.ws_loop = asyncio.get_running_loop()
                port = server.sockets[0].getsockname()[1]
                try:
                    async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
                        health = await receive_type(client, "health")
                        self.assertEqual(health["instance_id"], hub.instance_id)
                        history = await receive_type(client, "history")
                        self.assertEqual(history["items"][-1]["msg"], "history")
                        hub.append_log("HARDWARE", "live")
                        batch = await receive_type(client, "logs")
                        self.assertEqual(batch["items"][-1]["msg"], "live")
                        # Control messages remain responsive while the log sender waits for ack.
                        await client.send(json.dumps({"action": "ping"}))
                        await receive_type(client, "pong")
                        hub.clear_logs()
                        cleared = await receive_type(client, "logs_cleared")
                        self.assertEqual(cleared["through_seq"], batch["items"][-1]["seq"])
                        await client.send(json.dumps({"action": "logs_ack", "seq": batch["items"][-1]["seq"]}))
                        hub.append_log("HARDWARE", "after clear")
                        batch = await receive_type(client, "logs")
                        self.assertEqual([x["msg"] for x in batch["items"]], ["after clear"])
                        await client.send(json.dumps({"action": "logs_ack", "seq": batch["items"][-1]["seq"]}))
                finally:
                    hub.ws_loop = None

    async def test_failed_viewer_is_removed(self):
        class BrokenViewer(Viewer):
            async def send(self, payload):
                raise OSError("simulated socket failure")

        start = hub._seq
        hub.append_log("HARDWARE", "live")
        receipt = asyncio.Event()
        viewer = BrokenViewer(receipt)
        hub.ws_clients.add(viewer)
        await asyncio.wait_for(hub.stream_logs(viewer, start, receipt), 2)
        self.assertNotIn(viewer, hub.ws_clients)
        self.assertIsNotNone(viewer.closed)


if __name__ == "__main__":
    unittest.main()
