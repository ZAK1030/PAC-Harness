from copy import deepcopy
import math
from pathlib import Path
import tempfile
import unittest

from pac_harness.joint_motion import JointEnvironment, joint_target
from pac_harness.robot_backends import SimRobotBackend
from pac_harness.rokae_bridge import RokaeJointBridge


def action(mode='relative', **joints):
    return {'name': 'joint_move', 'arguments': {'mode': mode, 'unit': 'deg', 'joints': joints}}


class JointTests(unittest.TestCase):
    def test_joint_action_runs_through_planner_execution_and_detector(self):
        from pac_harness.runtime import Harness
        from pac_harness.storage import Journal
        with tempfile.TemporaryDirectory() as tmp:
            backend = SimRobotBackend(); backend.connect(); backend.enable()
            env = JointEnvironment(backend, tmp, motion_enabled=True, output=lambda _: None)
            class Planner:
                calls = 0
                def respond(self, payload, *args):
                    self.calls += 1
                    return {'reasoning': 'rotate once then verify', 'remaining_plan': [],
                            'action': action(J6=45) if self.calls == 1 else {'name': 'done', 'arguments': {}}}
            class Detector:
                def respond(self, payload, *args):
                    actual = payload['after']['robot']['joints'][5]
                    assert abs(actual - math.pi/4) < .002
                    return {'status': 'verified', 'reason': 'measured J6', 'evidence': ['joint feedback'],
                            'facts': {}, 'progress': 'improved', 'task_complete': payload['action']['name'] == 'done'}
            result = Harness(tmp, env, Planner(), Detector(), identity={}, output=lambda _: None).run('rotate', tmp)
            self.assertEqual(result['status'], 'completed')
            events = Journal(tmp).search(limit=100)
            sends = [e for e in events if e['event'] == 'action_pending']
            self.assertEqual(len(sends), 1)
            self.assertEqual(sends[0]['turn'], 1)
            self.assertEqual([e['turn'] for e in events if e['event'] == 'turn_completed'], [1, 2])

    def test_j6_return_preserves_current_other_joints_and_seven_axes(self):
        with tempfile.TemporaryDirectory() as tmp:
            for dof in (6, 7):
                backend = SimRobotBackend(dof); backend.connect(); backend.enable()
                lines = []
                env = JointEnvironment(backend, Path(tmp)/str(dof), motion_enabled=True, output=lines.append)
                self.assertTrue(env.execute(action(J6=90), 'first')['ok'])
                backend.move_joints([.1, .2, -.3, .4, -.5, math.pi/2] + ([.7] if dof == 7 else []))
                result = env.execute(action('absolute', J6=0), 'second')
                self.assertTrue(result['ok'])
                self.assertEqual(backend.state().joints[:5], [.1, .2, -.3, .4, -.5])
                if dof == 7:
                    self.assertEqual(backend.state().joints[6], .7)
                self.assertTrue(any('actual (deg)' in line for line in lines))
                self.assertEqual(env.reconcile('second')['status'], 'completed')
                self.assertEqual(env.execute(action(J6=80), 'second'), result)

    def test_timeout_cannot_replay_even_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            class LostReply(SimRobotBackend):
                calls = 0
                def move_joints(self, target):
                    self.calls += 1
                    super().move_joints(target)
                    raise TimeoutError('lost after moving')
            backend = LostReply(); backend.connect(); backend.enable()
            env = JointEnvironment(backend, tmp, motion_enabled=True, output=lambda _: None)
            self.assertTrue(env.execute(action(J6=90), 'same')['outcome_unknown'])
            restarted = JointEnvironment(backend, tmp, motion_enabled=True, output=lambda _: None)
            self.assertEqual(restarted.reconcile('same')['status'], 'unknown')
            restarted.execute(action(J6=90), 'same')
            self.assertEqual(backend.calls, 1)

    def test_invalid_arguments_and_disabled_motion(self):
        for joints in ({}, {'J7': 2}, {'J1': True}, {'J1': '1'}, {'J1': float('nan')}):
            with self.assertRaises(ValueError):
                joint_target([0]*6, action(**joints)['arguments'])
        with tempfile.TemporaryDirectory() as tmp:
            backend = SimRobotBackend(); backend.connect(); backend.enable()
            env = JointEnvironment(backend, tmp)
            with self.assertRaises(ValueError):
                env.execute(action(J6=1), 'disabled')


class BridgeTests(unittest.TestCase):
    def bridge(self, mutation=None):
        joints = [0.] * 6
        health = {'ok': True, 'execute': True, 'instance_id': 'receiver1', 'heartbeat_required': False,
                  'controller': {'ok': True, 'connected': True, 'armed': True, 'busy': False,
                                 'received': 0, 'executed': 0, 'rejected': 0,
                                 'limits': {'joint_commands_allowed': True, 'joint_timeout_s': .01,
                                            'joint_tolerance_rad': .002, 'workspace_enabled': False}}}
        sent = []
        def request(url, payload=None, timeout=None):
            if url.endswith('/health'):
                return deepcopy(health)
            if url.endswith('/call'):
                return {'joint_positions_rad': joints.copy(), 'tcp_pose': [0.]*6,
                        'sampled_at_ns': 12345, 'operation_state': 'idle'}
            self.assertTrue(url.endswith('/joints'))
            sent.append(deepcopy(payload))
            joints[:] = payload['target_joints_rad']
            result = {'ok': True, 'accepted': True, 'kind': 'joints', 'status': 'completed',
                      'execution_outcome': 'completed', 'sent_at_ns': payload['sent_at_ns'],
                      'request_id': 1, 'instance_id': 'receiver1', 'target_joints_rad': joints.copy(),
                      'actual_joint_positions_rad': joints.copy()}
            if mutation:
                mutation(result)
            health['controller'].update(received=1, executed=1, last_result=deepcopy(result))
            return result
        bridge = RokaeJointBridge({'action_url': 'http://fake', 'observation_url': 'http://fake',
                                   'joint_min_rad': [-7]*6, 'joint_max_rad': [7]*6}, request=request)
        bridge.connect()
        return bridge, sent

    def test_actual_receiver_wire_contract_and_once_only_dispatch(self):
        bridge, sent = self.bridge()
        result = bridge.move_joints_checked([0]*6, [0]*5+[1.])
        self.assertEqual(result['execution_outcome'], 'completed')
        self.assertEqual(len(sent), 1)
        self.assertEqual(set(sent[0]), {'state', 'joint_positions_rad', 'target_joints_rad',
                                       'sent_at_ns', 'observation_requested_at_ns'})

    def test_mismatched_receipt_and_raw_full_rotation_are_not_completion(self):
        for mutate in (lambda r: r.update(request_id=99),
                       lambda r: r.update(actual_joint_positions_rad=[0]*5+[1+2*math.pi])):
            bridge, sent = self.bridge(mutate)
            with self.assertRaises(RuntimeError):
                bridge.move_joints([0]*5+[1.])
            self.assertEqual(len(sent), 1)

    def test_changed_joint_source_refused_before_send(self):
        bridge, sent = self.bridge()
        with self.assertRaises(RuntimeError):
            bridge.move_joints_checked([.1]*6, [0]*6)
        self.assertEqual(sent, [])
