from copy import deepcopy
import tempfile
import time
import unittest
from pathlib import Path

try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("Install PAC-Harness[robot-vision] for geometry tests")

from examples.calibrated_motion import create
from pac_harness.agents import Agent
from pac_harness.cartesian_motion import approach_candidate
from pac_harness.runtime import Harness
from pac_harness.storage import Journal, loads, write_json
from pac_harness.tools import Knowledge


def calibrated(snapshot, **extra):
    return {"name": "move_calibrated", "arguments": {"observation_id": snapshot["observation_id"],
        "target": "synthetic_surface", "view": "overview", "pixel_uv": [4, 4], **extra}}


class CartesianTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = create(root=self.root, run_directory=self.root/'run', config={})
        self.env.output = lambda _: None
        self.addCleanup(self.env.close)

    def test_calibrated_then_relative_motion_one_send_each(self):
        snapshot = self.env.observe()
        action = calibrated(snapshot)
        located = self.env.tools()[0].handler(snapshot['observation_id'], 'synthetic_surface', 'overview', [4, 4])
        self.assertEqual(self.env.backend.state().pose[2], .3)  # Lookup never moves.
        np.testing.assert_allclose(located['object_xyz_base_m'], [0, 0, .5])
        first = self.env.execute(action, 'one')
        self.assertTrue(first['ok'])
        self.assertAlmostEqual(first['target_pose'][2], .35)
        self.assertEqual(first['grounding']['observation_id'], snapshot['observation_id'])
        self.assertIn('sha256', first['grounding']['provenance']['extrinsics_path'])
        snapshot = self.env.observe()
        second = self.env.execute({'name': 'move_relative', 'arguments': {
            'observation_id': snapshot['observation_id'], 'target': 'synthetic_surface', 'delta_m': [.001, 0, 0]}}, 'two')
        self.assertTrue(second['ok'])
        np.testing.assert_allclose(second['actual_pose'][:3], [.001, 0, .35])
        self.assertEqual(self.env.execute(action, 'one'), first)
        self.assertEqual(self.env.reconcile('one')['status'], 'completed')

    def test_stale_changed_pose_bad_depth_and_metric_guess_refused(self):
        snapshot = self.env.observe()
        action = calibrated(snapshot)
        with self.assertRaises(ValueError):
            self.env.candidate(calibrated(snapshot, target_xyz_m=[0, 0, .5]), snapshot)
        self.env.frames[snapshot['observation_id']].depths['overview'][:] = np.nan
        with self.assertRaises(ValueError):
            self.env.candidate(action, snapshot)
        snapshot = self.env.observe()
        action = calibrated(snapshot)
        self.env.backend.move_pose([.01, 0, .3, 0, 0, 0])
        with self.assertRaisesRegex(ValueError, 'Robot moved'):
            self.env.validate(action, self.env.observe())
        current = self.env.observe()
        self.env.frames[current['observation_id']].captured_monotonic = time.monotonic()-31
        with self.assertRaisesRegex(ValueError, 'stale'):
            self.env.candidate(calibrated(current), current)

    def test_workspace_failure_and_near_object_have_no_send(self):
        self.env.safety['xyz_max'] = [1, 1, .31]
        snapshot = self.env.observe()
        with self.assertRaisesRegex(ValueError, 'outside workspace'):
            self.env.execute(calibrated(snapshot), 'outside')
        self.assertEqual(self.env.backend.state().pose[2], .3)
        self.env.safety['xyz_max'] = [1, 1, 1]
        self.env.backend.move_pose([0, 0, .46, 0, 0, 0])
        snapshot = self.env.observe()
        with self.assertRaisesRegex(ValueError, 'fine entry'):
            self.env.execute(calibrated(snapshot), 'near')

    def test_coarse_stops_at_object_sphere_not_offset_waypoint_radius(self):
        snapshot = self.env.observe()
        self.env.safety['coarse_step_m'] = 1.
        candidate = self.env.candidate(calibrated(snapshot), snapshot)
        self.assertAlmostEqual(candidate['target_pose'][2], .45)
        # Lateral staging ray misses the sphere; it can reach the waypoint.
        candidate = self.env.candidate(calibrated(snapshot, approach_offset_m=[.2, 0, 0]), snapshot)
        np.testing.assert_allclose(candidate['target_pose'][:3], [.2, 0, .5])

    def test_direct_calibrated_target_then_fine_correction(self):
        self.env.config['calibrated_direct'] = True
        snapshot = self.env.observe()
        result = self.env.execute(calibrated(snapshot, approach_offset_m=[0, 0, .08]), 'direct')
        # The full 28 cm waypoint is commanded despite the 5 cm coarse cap.
        np.testing.assert_allclose(result['target_pose'][:3], [0, 0, .58])
        self.assertEqual(result['motion_mode'], 'coarse_direct')
        self.assertAlmostEqual(result['planned_distance_m'], .28)
        self.assertTrue(result['ok'])
        snapshot = self.env.observe()
        result = self.env.execute({'name': 'move_relative', 'arguments': {
            'observation_id': snapshot['observation_id'], 'target': 'synthetic_surface',
            'delta_m': [0, 0, -.008]}}, 'fine')
        np.testing.assert_allclose(result['actual_pose'][:3], [0, 0, .572])
        snapshot = self.env.observe()
        with self.assertRaisesRegex(ValueError, 'fine step'):
            self.env.candidate({'name': 'move_relative', 'arguments': {
                'observation_id': snapshot['observation_id'], 'target': 'synthetic_surface',
                'delta_m': [0, 0, -.02]}}, snapshot)

    def test_direct_validates_full_endpoint_and_allows_nearby_staging(self):
        self.env.config['calibrated_direct'] = True
        self.env.safety['xyz_max'] = [1, 1, .4]
        snapshot = self.env.observe()
        with self.assertRaisesRegex(ValueError, 'outside workspace'):
            self.env.execute(calibrated(snapshot), 'outside')
        self.assertEqual(self.env.backend.state().pose[2], .3)
        self.env.safety['xyz_max'] = [1, 1, 1]
        self.env.backend.move_pose([0, 0, .46, 0, 0, 0])
        snapshot = self.env.observe()
        candidate = self.env.candidate(calibrated(snapshot, approach_offset_m=[0, 0, .08]), snapshot)
        self.assertAlmostEqual(candidate['target_pose'][2], .58)

    def test_calibration_reload_updates_provenance_and_refuses_unmarked(self):
        snapshot = self.env.observe()
        original = self.env.candidate(calibrated(snapshot), snapshot)
        path = Path(self.env.grounder.config['extrinsics_path'])
        calibration = loads(path.read_text(encoding="utf-8"))
        calibration['cameras']['overview']['T_robot_base_from_camera'][3] = .02
        write_json(path, calibration)
        updated = self.env.candidate(calibrated(snapshot), snapshot)
        self.assertNotEqual(original['grounding']['provenance']['extrinsics_path']['sha256'],
                            updated['grounding']['provenance']['extrinsics_path']['sha256'])
        calibration['cameras']['overview']['calibrated'] = False
        write_json(path, calibration)
        with self.assertRaises(ValueError):
            self.env.candidate(calibrated(snapshot), snapshot)

    def test_timeout_receipt_survives_restart_without_replay(self):
        snapshot = self.env.observe()
        sends = []
        def lose_reply(target):
            sends.append(target)
            raise TimeoutError('unknown after transmission')
        self.env.backend.move_pose = lose_reply
        self.assertTrue(self.env.execute(calibrated(snapshot), 'lost')['outcome_unknown'])
        restarted = create(root=self.root, run_directory=self.root/'run', config={})
        self.addCleanup(restarted.close)
        self.assertEqual(restarted.reconcile('lost')['status'], 'unknown')
        self.assertTrue(restarted.execute(calibrated(snapshot), 'lost')['outcome_unknown'])
        self.assertEqual(len(sends), 1)

    def test_two_mode_planner_tools_and_postcheck_integration(self):
        class PlannerClient:
            mode = 0
            def complete(client, messages):
                # Fresh payload after system message at the start of each turn.
                payload = loads(messages[1]['content'])
                snapshot = payload['observation']
                if client.mode == 0:
                    client.mode += 1
                    return {'tool_calls': [{'name': 'locate_calibrated_target', 'arguments': calibrated(snapshot)['arguments']}]}
                if client.mode == 1:
                    action = calibrated(snapshot)
                elif client.mode == 2:
                    action = {'name': 'move_relative', 'arguments': {'observation_id': snapshot['observation_id'],
                              'target': 'synthetic_surface', 'delta_m': [.001, 0, 0]}}
                else:
                    action = {'name': 'done', 'arguments': {}}
                client.mode += 1
                return {'reasoning': 'synthetic offline test', 'remaining_plan': [], 'action': action}
        class Detector:
            def respond(self, payload, *_):
                return {'status': 'verified', 'reason': 'test feedback', 'evidence': ['simulated pose'],
                        'facts': {}, 'progress': 'improved', 'task_complete': payload['action']['name'] == 'done'}
        planner = Agent('planner', PlannerClient(), Knowledge(self.root))
        result = Harness(self.root, self.env, planner, Detector(), identity={}, output=lambda _: None).run('two modes', self.root/'run')
        self.assertEqual(result['status'], 'completed')
        events = Journal(self.root/'run').search(limit=100)
        self.assertEqual([e['action']['name'] for e in events if e['event']=='action_pending'],
                         ['move_calibrated', 'move_relative'])
        self.assertEqual([e['turn'] for e in events if e['event']=='turn_completed'], [1, 2, 3])


class CartesianBridgeTests(unittest.TestCase):
    def test_messagepack_translation_matches_existing_receiver_once(self):
        from pac_harness.rokae_bridge import RokaeJointBridge
        health = {'ok': True, 'execute': True, 'instance_id': 'one', 'heartbeat_required': False,
                  'controller': {'ok': True, 'connected': True, 'armed': True, 'busy': False,
                    'received': 0, 'executed': 0, 'rejected': 0, 'limits': {
                        'translation_per_axis_m': .05, 'translation_norm_m_by_mode': {'coarse': .05, 'fine': .01},
                        'move_timeout_base_s': .01, 'move_timeout_distance_factor': 2., 'move_speed_mm_s': 100}}}
        pose = [0, 0, .3, 0, 0, 0]
        sent = []
        def request(url, payload=None, timeout=None, packed=False):
            if url.endswith('/health'):
                return deepcopy(health)
            if url.endswith('/call'):
                return {'joint_positions_rad': [0]*6, 'tcp_pose': pose.copy(), 'sampled_at_ns': time.time_ns(),
                        'operation_state': 'idle', 'pose_frame': 'T_robot_base_from_tcp'}
            self.assertTrue(url.endswith('/action'))
            sent.append((deepcopy(payload), packed))
            pose[2] += payload['actions'][0][2]
            reply = {'ok': True, 'accepted': True, 'kind': 'action', 'status': 'completed',
                     'execution_outcome': 'completed', 'sent_at_ns': payload['sent_at_ns'], 'request_id': 1,
                     'instance_id': 'one', 'target': pose.copy()}
            health['controller'].update(received=1, executed=1, last_result=deepcopy(reply))
            return reply
        bridge = RokaeJointBridge({'action_url': 'http://fake', 'observation_url': 'http://fake',
                                   'joint_min_rad': [-3]*6, 'joint_max_rad': [3]*6}, request=request)
        bridge.connect()
        result = bridge.move_cartesian_checked({'target_pose': [0, 0, .35, 0, 0, 0], 'initial_pose': pose.copy(),
            'delta_m': [0, 0, .05], 'motion_mode': 'coarse', 'pose_reference': 'tcp', 'observation_stamp_ns': 42})
        self.assertEqual(result['execution_outcome'], 'completed')
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent[0][1])
        self.assertEqual(len(sent[0][0]['actions']), 8)
        self.assertTrue(all(row == [0]*6 for row in sent[0][0]['actions'][1:]))
