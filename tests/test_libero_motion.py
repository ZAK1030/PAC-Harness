"""Geometry / receipt tests; opt-in tests also render and move a real robot."""
from dataclasses import asdict
import math
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("Install robot-vision extras for geometry tests")

from adapters.libero import LiberoEnvironment
from adapters.libero_motion import (SimRGBD, camera_intrinsics,
    metric_depth, rotation_xyz, transform, world_camera)
from pac_harness.robot_backends import RobotState
from pac_harness.storage import loads


class FrameTests(unittest.TestCase):
    def test_one_result_per_turn_and_bounded_dual_camera_cache(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as folder:
            adapter = LiberoEnvironment.__new__(LiberoEnvironment)
            adapter.root = Path(folder)
            adapter.directory = adapter.root / 'episode'
            adapter.np, adapter.Image, adapter.motion = np, Image, None
            adapter.task = SimpleNamespace(language='test')
            adapter.suite_name, adapter.task_id, adapter.max_steps = 'libero_spatial', 0, 220
            adapter.data = {'steps': 0, 'success': False}
            adapter.obs = {view+'_image': np.zeros((2, 2, 3), dtype=np.uint8)
                for view in ('agentview', 'robot0_eye_in_hand')}
            first = adapter.observe()
            self.assertEqual(adapter.observe()['artifacts'], first['artifacts'])
            self.assertFalse((adapter.directory/'frames').exists())
            action = {'name': 'robot_action', 'arguments': {'actions': [[0]*7]}}
            for turn in (1, 2):
                adapter.data['steps'] = turn
                adapter.obs['agentview_image'][:] = turn*40
                after = adapter.observe()
                for _ in range(2):  # A Detector retry must not add result images.
                    record = adapter.record_action_frame(turn=turn, action=action,
                        execution={'executed_steps': 1}, observation=after, request_id=f'r{turn}')
                self.assertEqual(record['sim_steps'], turn)
                with Image.open(adapter.directory/'frames'/f'turn-{turn:03d}.png') as image:
                    np.testing.assert_array_equal(np.asarray(image), adapter.obs['agentview_image'][::-1])
            self.assertEqual(sorted(p.name for p in (adapter.directory/'frames').iterdir()),
                ['turn-001.png', 'turn-002.png'])
            self.assertEqual(len(list((adapter.directory/'.observations').glob('*.png'))), 4)
            self.assertTrue(all((adapter.root/a['path']).exists() for a in after['artifacts']))
            self.assertFalse((adapter.root/first['artifacts'][0]['path']).exists())
            records = loads((adapter.directory/'frames.json').read_text(encoding="utf-8"))
            self.assertEqual(list(records), ['1', '2'])
            self.assertEqual(records['2']['request_id'], 'r2')
            self.assertEqual(records['2']['action'], action)


class Backend:
    def __init__(self):
        self.pose = [.3, .02, .4, 0, 0, 0]
        self.base = transform([.2, -.4, .8], rotation_xyz([0, 0, math.pi/2]))
        self.robot = SimpleNamespace(controller=SimpleNamespace(use_delta=True))

    def world_base(self):
        return self.base

    def world_tcp(self):
        return self.base @ transform(self.pose[:3], rotation_xyz(self.pose[3:]))

    def state(self):
        return RobotState(connected=True, enabled=True, pose=list(self.pose))


class FakeSimulator:
    def __init__(self, backend, *, fail=False, move=True, success=False):
        self.backend, self.fail, self.move, self.success = backend, fail, move, success
        self.commands = []
        self.env = SimpleNamespace(sim=self)

    def get_state(self):
        return self

    def flatten(self):
        return np.zeros(3)

    def step(self, vector):
        self.commands.append((list(vector), self.backend.robot.controller.use_delta))
        if self.fail:
            raise RuntimeError('simulator failure')
        if self.move:
            world = self.backend.world_tcp()[:3, 3]
            xyz = world + np.asarray(vector[:3]) * .05 if self.backend.robot.controller.use_delta else vector[:3]
            self.backend.pose[:3] = (np.linalg.inv(self.backend.base) @ np.r_[xyz, 1])[:3].tolist()
        return {}, 0, False, {}

    def check_success(self):
        return self.success


class MotionTests(unittest.TestCase):
    def setUp(self):
        # Only the rotation conversion import is stubbed; frame mapping,
        # stepping, arrival checks and durable LIBERO execution run unchanged.
        module = SimpleNamespace(mat2quat=lambda matrix: matrix,
            quat2axisangle=lambda matrix: np.array([0, 0, math.atan2(matrix[1, 0], matrix[0, 0])]))
        self.mock_import = patch.dict('sys.modules', {'robosuite.utils.transform_utils': module})
        self.mock_import.start()
        self.addCleanup(self.mock_import.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def adapter(self, **options):
        adapter = LiberoEnvironment.__new__(LiberoEnvironment)
        adapter.path = Path(self.temp.name) / 'state.json'
        adapter.pending = Path(self.temp.name) / 'pending.json'
        adapter.data = {'steps': 0, 'success': False, 'receipts': {}, 'gripper_command': 1}
        adapter.max_steps, adapter.chunk_size = 10, 10
        backend = Backend()
        adapter.env = FakeSimulator(backend, **options)
        adapter.motion = SimpleNamespace(backend=backend, move_max_steps=3,
            arrival_tolerance_m=.002, orientation_tolerance_rad=.02,
            frames={'frame': SimpleNamespace(state={'sim_steps': 0})},
            validate=Mock(), candidate=lambda action, before: {
                'initial_pose': list(backend.pose), 'motion_mode': 'fine',
                'target_pose': [backend.pose[0]+.008, *backend.pose[1:]]})
        adapter.observe = lambda: {'robot': asdict(backend.state()), 'pose_reference': 'tcp',
            'success': adapter.data['success'], 'truncated': adapter.data['steps'] >= adapter.max_steps}
        return adapter

    def action(self):
        return {'name': 'move_relative', 'arguments': {'observation_id': 'frame', 'target': 'test', 'delta_m': [.008, 0, 0]}}

    def refresh(self, adapter):
        adapter.motion.frames['frame'].state['sim_steps'] = adapter.data['steps']

    def test_absolute_world_transform_gripper_and_dedup(self):
        adapter = self.adapter()
        result = adapter.execute(self.action(), 'first')
        command, use_delta = adapter.env.commands[0]
        np.testing.assert_allclose(command[:3], [.18, -.092, 1.2], atol=1e-12)
        np.testing.assert_allclose(command[3:6], [0, 0, math.pi/2], atol=1e-12)
        self.assertEqual(command[-1], 1)
        self.assertFalse(use_delta)
        self.assertTrue(adapter.motion.backend.robot.controller.use_delta)
        self.assertTrue(result['arrived'])
        self.assertFalse(result['success'])  # Arrival is not task completion.
        self.assertEqual(adapter.execute(self.action(), 'first'), result)
        self.assertEqual(len(adapter.env.commands), 1)

    def test_measured_miss_is_known_and_can_replan(self):
        adapter = self.adapter(move=False)
        result = adapter.execute(self.action(), 'first')
        self.assertFalse(result['ok'])
        self.assertFalse(result['outcome_unknown'])
        self.assertEqual(result['stop_reason'], 'move_step_limit')
        self.assertEqual(result['executed_steps'], 3)
        self.assertFalse(adapter.pending.exists())
        self.assertEqual(adapter.reconcile('first')['status'], 'completed')
        self.refresh(adapter)
        adapter.execute(self.action(), 'second')
        self.assertEqual(len(adapter.env.commands), 6)

    def test_horizon_counts_each_internal_step(self):
        adapter = self.adapter(move=False)
        adapter.max_steps = 2
        result = adapter.execute(self.action(), 'first')
        self.assertEqual(result['stop_reason'], 'episode_exhausted')
        self.assertEqual(result['executed_steps'], 2)
        self.assertEqual(adapter.data['steps'], 2)
        with self.assertRaisesRegex(ValueError, 'Episode ended'):
            adapter.execute(self.action(), 'second')

    def test_exception_restores_delta_and_never_replays(self):
        adapter = self.adapter(fail=True)
        with self.assertRaises(RuntimeError):
            adapter.execute(self.action(), 'first')
        self.assertTrue(adapter.motion.backend.robot.controller.use_delta)
        self.assertEqual(adapter.reconcile('first')['status'], 'unknown')
        self.assertTrue(adapter.execute(self.action(), 'second')['outcome_unknown'])
        self.assertEqual(len(adapter.env.commands), 1)

    def test_raw_action_restores_gripper_command_for_next_move(self):
        adapter = self.adapter()
        adapter.execute({'name': 'robot_action', 'arguments': {'actions': [[0, 0, 0, 0, 0, 0, -1]]}}, 'open')
        self.assertTrue(adapter.env.commands[0][1])
        self.refresh(adapter)
        result = adapter.execute(self.action(), 'fine')
        self.assertEqual(result['gripper_command'], -1)
        self.assertEqual(adapter.env.commands[1][0][-1], -1)

    def test_gripper_only_step_invalidates_old_snapshot(self):
        adapter = self.adapter(move=False)
        adapter.execute({'name': 'robot_action', 'arguments': {'actions': [[0, 0, 0, 0, 0, 0, -1]]}}, 'open')
        with self.assertRaisesRegex(ValueError, 'Simulator advanced'):
            adapter.execute(self.action(), 'stale')
        self.assertEqual(len(adapter.env.commands), 1)

    def test_depth_conversion_and_matching_vertical_flip(self):
        sim = SimpleNamespace(model=SimpleNamespace(stat=SimpleNamespace(extent=2),
            vis=SimpleNamespace(map=SimpleNamespace(znear=.1, zfar=10))))
        np.testing.assert_allclose(metric_depth(sim, np.array([0, 1])), [.2, 20])
        for value in (float('nan'), -1, 2):
            with self.assertRaises(ValueError):
                metric_depth(sim, [value])
        image = np.arange(12, dtype=np.uint8).reshape(2, 2, 3)
        buffer = np.array([[0, .25], [.5, .75]])[..., None]
        adapter = SimpleNamespace(env=SimpleNamespace(env=SimpleNamespace(sim=sim)), data={'steps': 0},
            obs={view+suffix: data for view in ('agentview', 'robot0_eye_in_hand')
                 for suffix, data in (('_image', image), ('_depth', buffer))})
        frame = SimRGBD(adapter).capture(Backend().state())
        np.testing.assert_array_equal(frame.images['agentview'], image[::-1])
        np.testing.assert_allclose(frame.depths['agentview'], metric_depth(sim, buffer)[::-1, :, 0])

    def test_camera_optical_axes_and_pixel_centres(self):
        sim = SimpleNamespace(model=SimpleNamespace(camera_name2id=lambda name: 0, cam_fovy=[90]),
            data=SimpleNamespace(cam_xpos=np.array([[1, 2, 3]]), cam_xmat=np.eye(3).reshape(1, 9)))
        np.testing.assert_allclose(camera_intrinsics(sim, 'test', 100, 200),
            [[50, 0, 99.5], [0, 50, 49.5], [0, 0, 1]], atol=1e-12)
        camera = world_camera(sim, 'test')
        np.testing.assert_allclose(camera @ [0, 0, 1, 1], [1, 2, 2, 1])
        np.testing.assert_allclose(camera @ [0, 1, 0, 1], [1, 1, 3, 1])


@unittest.skipUnless(os.environ.get('PAC_LIBERO_SIM_TESTS') == '1', 'Set PAC_LIBERO_SIM_TESTS=1 for real LIBERO tests')
class SimulatorTests(unittest.TestCase):
    def test_calibration_surfaces_wrist_after_motion_and_stale_frame(self):
        import mujoco
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(dir=root/'logs', prefix='libero-geometry-test-') as directory:
            adapter = LiberoEnvironment(root=root, run_directory=directory,
                config={'cartesian': {'grounding': {'sample_radius_px': 0}}})
            try:
                old = None
                for iteration in range(2):
                    observation = adapter.observe()
                    sim = adapter.env.env.sim
                    for view in ('agentview', 'robot0_eye_in_hand'):
                        for u, v in ((128, 160), (76, 204), (179, 204)):
                            arguments = {'observation_id': observation['observation_id'],
                                'target': 'visible surface', 'view': view, 'pixel_uv': [u, v]}
                            grounded = adapter.motion.tools()[0].handler(**arguments)
                            point = np.asarray(grounded['object_xyz_base_m'])
                            point_world = (adapter.motion.backend.world_base() @ np.r_[point, 1])[:3]
                            camera = world_camera(sim, view)
                            height, width = adapter.obs[view+'_image'].shape[:2]
                            ray = camera[:3, :3] @ np.linalg.solve(camera_intrinsics(sim, view, height, width), [u, v, 1])
                            ray /= np.linalg.norm(ray)
                            distance = mujoco.mj_ray(sim.model._model, sim.data._data, camera[:3, 3].copy(),
                                ray, np.array([0, 1, 0, 0, 0, 0], dtype=np.uint8), 1, -1, np.array([-1], dtype=np.int32))
                            self.assertGreater(distance, 0)
                            self.assertLess(np.linalg.norm(point_world - (camera[:3, 3]+distance*ray)), .001)
                    if iteration == 0:
                        old = observation
                        adapter.execute({'name': 'robot_action', 'arguments': {'actions': [[.3, .2, 0, 0, 0, .1, -1]]*3}}, 'raw')
                with self.assertRaisesRegex(ValueError, 'Robot moved'):
                    adapter.validate({'name': 'move_relative', 'arguments': {'observation_id': old['observation_id'],
                        'target': 'stale image', 'delta_m': [0, 0, .005]}}, observation)
            finally:
                adapter.close()
