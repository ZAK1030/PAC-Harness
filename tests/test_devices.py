from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from pac_harness.devices import CameraSet, load_device, OpenCVCamera
from pac_harness.__main__ import _main
from pac_harness.storage import write_json


class DeviceTests(unittest.TestCase):
    def test_camera_artifacts_are_unique_and_confined(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            device = Mock()
            device.read_png.return_value = b'\x89PNG\r\n\x1a\nfixture'
            with patch('pac_harness.devices.load_device', return_value=device):
                cameras = CameraSet(root=root, run_directory=root/'logs/run', cameras={'wrist': {}})
                first = cameras.observe()['artifacts'][0]
                second = cameras.observe()['artifacts'][0]
                self.assertNotEqual(first['path'], second['path'])
                self.assertEqual((root/first['path']).read_bytes(), device.read_png.return_value)
                cameras.close()
                device.close.assert_called_once()

    def test_partial_open_failure_closes_previous_camera(self):
        with tempfile.TemporaryDirectory() as tmp:
            device = Mock()
            with patch('pac_harness.devices.load_device', side_effect=[device, RuntimeError('missing')]):
                with self.assertRaises(RuntimeError):
                    CameraSet(root=tmp, run_directory=Path(tmp)/'logs/run', cameras={'a': {}, 'b': {}})
            device.close.assert_called_once()

    def test_opencv_failure_releases_device(self):
        cv = Mock()
        cv.VideoCapture.return_value.isOpened.return_value = False
        with patch.dict('sys.modules', {'cv2': cv}):
            with self.assertRaises(RuntimeError):
                OpenCVCamera(0)
        cv.VideoCapture.return_value.release.assert_called_once()

    def test_observe_only_never_calls_execute_or_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root/'config.json', {'adapter': {'factory': 'examples.camera_observation:create'}})
            environment = Mock()
            environment.observe.return_value = {'artifacts': []}
            with patch('examples.camera_observation.create', return_value=environment), patch('pac_harness.__main__.ChatClient') as client:
                self.assertEqual(_main(['--root', tmp, '--observe-only']), 0)
            environment.execute.assert_not_called()
            environment.close.assert_called_once()
            client.assert_not_called()

    def test_scene_creation_cli_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('sys.stderr'), self.assertRaises(SystemExit) as error:
                _main(['--root', tmp, '--init-scene', 'lab'])
            self.assertEqual(error.exception.code, 2)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_first_assistance_message_is_delivered(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_json(Path(tmp)/'config.json', {'adapter': {}})
            with patch('pac_harness.__main__.ToUser') as assistant:
                assistant.return_value.run.return_value = {'status': 'no_change'}
                _main(['--root', tmp, '--assist', '--message', '请记录当前项目的设备信息'])
                reader = assistant.call_args.kwargs['input_fn']
                self.assertEqual(reader(''), '请记录当前项目的设备信息')
                with patch('pac_harness.dialogue_input.read_dialogue_input', return_value='C'):
                    self.assertEqual(reader(''), 'C')
