import io
import tempfile
import time
import unittest
import zlib

try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("Install PAC-Harness[robot-vision] for RGB-D tests")
from PIL import Image

from pac_harness.rgbd_rpc import RGBDRPC


class RPCSnapshotTests(unittest.TestCase):
    def setUp(self):
        buffer = io.BytesIO()
        Image.fromarray(np.zeros((2, 2, 3), dtype=np.uint8)).save(buffer, format='JPEG')
        self.raw = {'requested_at_ns': time.time_ns(),
                    'robot_state': {'tcp_pose': [0]*6, 'pose_frame': 'T_robot_base_from_tcp'},
                    'images_jpeg': {'arbitrary-camera': buffer.getvalue()},
                    'depth_zlib_uint16_le': {'arbitrary-camera': zlib.compress(np.full((2, 2), 500, dtype='<u2').tobytes())},
                    'depth_shapes': {'arbitrary-camera': [2, 2]}, 'depth_scale_mm': {'arbitrary-camera': 1.},
                    'camera_timestamps_ns': {'arbitrary-camera': 1}, 'camera_age_ms': {'arbitrary-camera': 10}}
        self.calls = []
        def request(url, payload):
            self.calls.append(payload)
            return {'ok': True, 'result': self.raw}
        self.reader = RGBDRPC({'observation_url': 'http://fake', 'views': ['arbitrary-camera']}, request=request)

    def test_explicit_metric_conversion_and_frozen_frame_refusal(self):
        first = self.reader.capture(None)
        np.testing.assert_allclose(first.depths['arbitrary-camera'], .5)
        self.assertEqual(self.calls[0]['method'], 'hardware.read_observation')
        self.assertEqual(first.stamp_ns, self.raw['requested_at_ns'])
        with self.assertRaisesRegex(ValueError, 'frozen'):
            self.reader.capture(None)
        self.raw['camera_timestamps_ns']['arbitrary-camera'] += 1
        self.assertNotEqual(first.id, self.reader.capture(None).id)

    def test_stale_camera_and_bad_compressed_shape_refused(self):
        self.raw['camera_age_ms']['arbitrary-camera'] = 501
        with self.assertRaises(ValueError):
            self.reader.capture(None)
        self.raw['camera_age_ms']['arbitrary-camera'] = 1
        self.raw['depth_shapes']['arbitrary-camera'] = [3, 3]
        with self.assertRaisesRegex(ValueError, 'size/shape'):
            self.reader.capture(None)
        self.assertEqual(self.reader.last_stamps, {})

    def test_missing_scale_never_guesses_millimetres(self):
        self.raw['depth_scale_mm'] = {}
        with self.assertRaisesRegex(ValueError, 'scale'):
            self.reader.capture(None)
