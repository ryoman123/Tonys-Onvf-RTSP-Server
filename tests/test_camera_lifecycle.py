import threading
import unittest

from app.camera import VirtualONVIFCamera


class _FakeDiscovery:
    def __init__(self, order):
        self.order = order

    def stop_discovery_service(self):
        self.order.append("discovery")


class _FakeServer:
    def __init__(self, order):
        self.order = order

    def shutdown(self):
        self.order.append("server")


class _FakeThread:
    def __init__(self, order, alive=True):
        self.order = order
        self._alive = alive

    def join(self, timeout=None):
        self.order.append(("join", timeout))
        self._alive = False

    def is_alive(self):
        return self._alive


class CameraLifecycleTests(unittest.TestCase):
    def make_camera(self):
        order = []
        camera = VirtualONVIFCamera.__new__(VirtualONVIFCamera)
        camera.name = "Lifecycle Test"
        camera.path_name = "lifecycle_test"
        camera.status = "running"
        camera.enable_event_forwarding = False
        camera._lifecycle_lock = threading.RLock()
        camera.onvif_service = _FakeDiscovery(order)
        camera.server = _FakeServer(order)
        camera.flask_thread = _FakeThread(order)
        camera.flask_app = object()
        camera.use_virtual_nic = False
        camera.network_mgr = None
        return camera, order

    def test_stop_releases_listener_before_returning(self):
        camera, order = self.make_camera()

        camera.stop()

        self.assertEqual(camera.status, "stopped")
        self.assertIsNone(camera.server)
        self.assertIsNone(camera.flask_thread)
        self.assertIsNone(camera.flask_app)
        self.assertIsNone(camera.onvif_service)
        self.assertEqual(order[0], "discovery")
        self.assertEqual(order[1], "server")
        self.assertEqual(order[2], ("join", 5.0))

    def test_start_rejects_a_stale_server_thread(self):
        camera, _ = self.make_camera()
        camera.status = "stopped"
        camera.server = None

        with self.assertRaisesRegex(RuntimeError, "previous ONVIF server thread is still running"):
            camera.start()

    def test_event_forwarder_stop_joins_thread(self):
        camera, order = self.make_camera()
        camera._event_forwarding_running = True
        camera._event_forwarding_thread = _FakeThread(order)
        camera.onvif_subscription_active = True
        camera.onvif_subscription_error = None

        camera.stop_event_forwarding()

        self.assertFalse(camera._event_forwarding_running)
        self.assertFalse(camera.onvif_subscription_active)
        self.assertIsNone(camera._event_forwarding_thread)
        self.assertIn(("join", 8.0), order)


if __name__ == "__main__":
    unittest.main()
