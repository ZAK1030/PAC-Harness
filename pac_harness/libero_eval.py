"""Batch LIBERO evaluation through the actual PAC Harness runtime."""
from __future__ import annotations
import argparse
from pathlib import Path
import time
import uuid

from adapters.libero import LiberoEnvironment, SUITES, load_suite, load_init_states
from .agents import Agent, ChatClient
from .runtime import Harness
from .storage import Journal, loads, write_json
from .tools import Knowledge


class SmokePlanner:
    def respond(self, payload, toolbox, journal):
        obs = payload['observation']
        return {'reasoning': 'Smoke test only: zero motion, then completion check.', 'remaining_plan': [],
                'action': {'name': 'done', 'arguments': {}} if obs['success'] or obs['truncated'] else
                {'name': 'robot_action', 'arguments': {'actions': [[0, 0, 0, 0, 0, 0, -1]]}}}


class MotionSmokePlanner:
    """Exercise both calibrated cameras, raw gripper control and fine motion."""
    def __init__(self, environment):
        self.environment = environment

    def respond(self, payload, toolbox, journal):
        env, obs = self.environment, payload['observation']
        stage = len(env.data['receipts'])
        if stage in (0, 2):
            view = 'agentview' if stage == 0 else 'robot0_eye_in_hand'
            shape = next(v['rgb_shape'] for v in obs['views'] if v['view'] == view)
            action = None
            pixels = [(int(u*shape[1]), int(v*shape[0])) for v, u in
                      ((.9, .5), (.8, .3), (.8, .7), (.625, .5), (.5, .3), (.5, .7), (.3, .5))]
            # Probe near the projected TCP as well: different Pro layouts may
            # leave all fixed grid samples far from the starting gripper.
            from adapters.libero_motion import camera_intrinsics, world_camera
            np = env.np
            sim = env.env.env.sim
            camera_tcp = np.linalg.inv(world_camera(sim, view)) @ env.motion.backend.world_tcp()[:, 3]
            if camera_tcp[2] > 0:
                uv = camera_intrinsics(sim, view, *shape[:2]) @ camera_tcp[:3]
                u, v = (uv[:2] / uv[2]).astype(int)
                pixels = [(int(u+dx), int(v+dy)) for dy in (0, -8, 8, -16, 16)
                          for dx in (0, -8, 8, -16, 16)] + pixels
            for u, v in pixels:
                if not (0 <= u < shape[1] and 0 <= v < shape[0]):
                    continue
                proposed = {'name': 'move_calibrated', 'arguments': {
                    'observation_id': obs['observation_id'], 'target': 'visible surface for motion integration test',
                    'view': view, 'pixel_uv': [u, v], 'approach_offset_m': [0, 0, 0]}}
                try:
                    # A smoke test should exercise calibration without driving
                    # toward an arbitrary object or colliding with its support.
                    # Derive a short upward target using only RGB-D and TCP pose.
                    measured = toolbox.invoke('planner', 'locate_calibrated_target', proposed['arguments'])
                    desired = list(obs['robot']['pose'][:3])
                    desired[2] += .008
                    proposed['arguments']['approach_offset_m'] = [
                        goal-point for goal, point in zip(desired, measured['object_xyz_base_m'])]
                    env.motion.candidate(proposed, obs)
                    toolbox.invoke('planner', 'locate_calibrated_target', proposed['arguments'])
                    action = proposed
                    break
                except ValueError:
                    continue
            if action is None:
                raise ValueError(f'Motion smoke could not find a valid visible surface in {view}')
        elif stage == 1:
            action = {'name': 'robot_action', 'arguments': {'actions': [[0, 0, 0, 0, 0, 0, 1]]}}
        else:
            action = {'name': 'move_relative', 'arguments': {'observation_id': obs['observation_id'],
                'target': 'upward fine correction for integration test', 'delta_m': [0, 0, .008]}}
        return {'reasoning': 'Motion integration test only; no model accuracy claim.',
                'remaining_plan': [], 'action': action}


class SuccessDetector:
    def respond(self, payload, toolbox, journal):
        success = payload['after']['success']
        return {'status': 'verified', 'reason': 'Official LIBERO check_success predicate.',
                'evidence': [f'LIBERO success={success}'], 'task_complete': success,
                'progress': 'improved' if success else 'unknown', 'facts': {'libero_success': success}}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='.')
    parser.add_argument('--config', default='config.libero.json')
    parser.add_argument('--suite', choices=SUITES)
    parser.add_argument('--task-ids', type=int, nargs='+')
    initial_group = parser.add_mutually_exclusive_group()
    initial_group.add_argument('--episodes', type=int)
    initial_group.add_argument('--init-state-ids', type=int, nargs='+',
                               help='Explicit stored initial-state indices (not random seeds)')
    parser.add_argument('--seed', type=int)
    parser.add_argument('--max-steps', type=int, help='Override episode control-step horizon (changes evaluation protocol)')
    smoke_group = parser.add_mutually_exclusive_group()
    smoke_group.add_argument('--smoke', action='store_true', help='Zero-action integration test, not model accuracy')
    smoke_group.add_argument('--smoke-motion', action='store_true',
        help='Real OSC test of both calibrated cameras, fine correction and gripper preservation; no model calls')
    parser.add_argument('--output')
    parser.add_argument('--resume', metavar='BATCH_DIR', help='Resume an existing batch using its saved protocol; preserve finished episodes')
    review_group = parser.add_mutually_exclusive_group()
    review_group.add_argument('--post-run-review', action='store_true', dest='post_run_review', default=None,
                              help='analyzer reviews logs/images and saves concise role rules after each episode')
    review_group.add_argument('--no-post-run-review', action='store_false', dest='post_run_review',
                              help='Disable experience updates for a fixed-policy benchmark')
    parser.add_argument('--detector', choices=('official', 'model'),
                        help='Official predicate detector, or configured model Detector; scoring always uses official success')
    args = parser.parse_args(argv)
    testing = args.smoke or args.smoke_motion
    if args.resume and (testing or args.output):
        parser.error('--resume cannot be combined with smoke modes or --output')
    root = Path(args.root).resolve()
    previous = None
    if args.resume:
        output = Path(args.resume)
        output = output.resolve() if output.is_absolute() else (root / output).resolve()
        output.relative_to(root)
        previous = loads((output / 'summary.json').read_text())
        if previous.get('mode') != 'evaluation':
            parser.error('Only evaluation batches can be resumed')
        for name, key in [('suite', 'suite'), ('task_ids', 'task_ids'),
                          ('episodes', 'episodes_per_task'), ('seed', 'seed'), ('detector', 'detector')]:
            if getattr(args, name) is not None and getattr(args, name) != previous[key]:
                parser.error('Resume protocol mismatch: --' + name.replace('_', '-'))
            setattr(args, name, previous[key])
        saved_initials = previous.get('init_state_ids', list(range(previous['episodes_per_task'])))
        if args.init_state_ids is not None and args.init_state_ids != saved_initials:
            parser.error('Resume protocol mismatch: --init-state-ids')
        args.init_state_ids = saved_initials
        if args.max_steps is not None and args.max_steps != previous['settings'].get('max_steps', SUITES[args.suite]):
            parser.error('Resume protocol mismatch: --max-steps')
    args.episodes = args.episodes if args.episodes is not None else 50
    if args.init_state_ids is not None and (any(i < 0 for i in args.init_state_ids) or
            len(set(args.init_state_ids)) != len(args.init_state_ids)):
        parser.error('initial-state indices must be unique and nonnegative')
    args.seed = args.seed if args.seed is not None else 0
    args.detector = args.detector or 'official'
    if args.episodes < 1:
        parser.error('--episodes must be positive')
    if args.max_steps is not None and args.max_steps < 1:
        parser.error('--max-steps must be positive')
    config = loads((root / args.config).read_text())
    settings = dict(previous['settings'] if previous else config['adapter'].get('settings', {}))
    if args.max_steps is not None:
        settings['max_steps'] = args.max_steps
    suite_name = args.suite or settings.get('suite', 'libero_spatial')
    if not testing:
        model = config['models']['planner']
        if model['model'] == 'SET_YOUR_MODEL':
            parser.error('Configure models.planner before evaluating; --smoke needs no model')
        if previous and previous['planner'] != model['model']:
            parser.error('Resume planner model differs from the saved batch')
    suite = load_suite(suite_name, root / 'maintenance/libero-config')
    ids = args.task_ids if args.task_ids is not None else ([0] if testing else list(range(suite.n_tasks)))
    if any(i < 0 or i >= suite.n_tasks for i in ids) or len(set(ids)) != len(ids):
        parser.error('task ids must be unique and inside suite')
    initials = args.init_state_ids if args.init_state_ids is not None else list(range(1 if testing else args.episodes))
    if testing and len(initials) != 1:
        parser.error('Smoke modes require exactly one initial-state index')
    episodes = len(initials)
    # Do not silently cycle through initial states when asked for more episodes.
    for task_id in ids:
        if max(initials) >= len(load_init_states(suite, task_id)):
            parser.error('requested index exceeds available official initial states')
    if not args.resume:
        output = Path(args.output).resolve() if args.output else root / 'logs' / f'libero-{time.strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:8]}'
    output.relative_to(root)
    output.mkdir(parents=True, exist_ok=bool(args.resume))
    results = list(previous['results']) if previous else []
    def save():
        successes = sum(r['success'] for r in results)
        write_json(output / 'summary.json', {'suite': suite_name, 'mode': 'smoke_motion' if args.smoke_motion else 'smoke' if args.smoke else 'evaluation',
            'seed': args.seed, 'detector': 'official' if testing else args.detector, 'settings': settings, 'task_ids': ids, 'episodes_per_task': episodes, 'init_state_ids': initials, 'planner': None if testing else config['models']['planner']['model'],
            'episodes': len(results), 'successes': successes, 'success_rate': successes / len(results) if results else None,
            'per_task': {str(i): {'episodes': sum(r['task_id'] == i for r in results),
                'successes': sum(r['success'] for r in results if r['task_id'] == i),
                'success_rate': (sum(r['success'] for r in results if r['task_id'] == i) / sum(r['task_id'] == i for r in results)) if any(r['task_id'] == i for r in results) else None} for i in ids}, 'results': results})
        if (output.parent / 'protocol.json').exists():
            from .metrics import export_metrics
            export_metrics(output.parent)
    # Persist the protocol before simulator construction, so an interrupted
    # first environment initialization still leaves a resumable batch.
    save()
    for task_id in ids:
        for episode in initials:
            directory = output / f'task-{task_id:03d}-episode-{episode:03d}'
            if args.resume and (directory / 'result.json').exists():
                saved = loads((directory / 'result.json').read_text())
                if saved['runtime']['status'] not in {'interrupted', 'restart_required'}:
                    if not any(r['task_id'] == task_id and r['episode'] == episode for r in results):
                        results.append(saved)
                        save()
                    print(f'Resume: preserving task={task_id} episode={episode} success={saved["success"]}', flush=True)
                    continue
            if args.resume:
                results[:] = [r for r in results if (r['task_id'], r['episode']) != (task_id, episode)]
            resume_episode = bool(args.resume and (directory / 'task_state.json').exists())
            episode_settings = {**settings, 'suite': suite_name, 'task_id': task_id,
                                'init_state_id': episode, 'seed': args.seed + episode}
            if args.smoke:
                episode_settings['max_steps'] = 2
            elif args.smoke_motion:
                episode_settings.update(max_steps=160, calibrated_motion=True)
            session_id = uuid.uuid4().hex
            session_start = time.monotonic()
            timing = Journal(directory)
            timing.event('episode_session_start', session_id=session_id, resume=resume_episode)
            env = LiberoEnvironment(root=root, run_directory=directory, config=episode_settings)
            print(f'\nTask: suite={suite_name} task_id={task_id} episode={episode} max_steps={env.max_steps}\n'
                  f'Instruction: {env.task.language}', flush=True)
            planner = MotionSmokePlanner(env) if args.smoke_motion else SmokePlanner() if args.smoke else Agent('planner', ChatClient(config['models']['planner']), Knowledge(root),
                max_tool_rounds=config['models']['planner'].get('max_tool_rounds', 6))
            # Ground truth detector avoids scoring language-model completion claims as success.
            detector = SuccessDetector() if testing or args.detector == 'official' else Agent(
                'detector', ChatClient(config['models']['detector']), Knowledge(root),
                max_tool_rounds=config['models']['detector'].get('max_tool_rounds', 4))
            harness = Harness(root, env, planner, detector, identity={'factory': 'adapters.libero:create', 'settings': episode_settings})
            try:
                status = harness.run(env.task.language, directory, max_turns=4 if args.smoke_motion else 3 if args.smoke else config.get('run', {}).get('max_turns', 600),
                                     max_failures=config.get('run', {}).get('max_failures', 3), resume=resume_episode)
            finally:
                timing.event('episode_session_end', session_id=session_id,
                             elapsed_s=round(time.monotonic() - session_start, 3))
            record = {'task_id': task_id, 'instruction': env.task.language, 'episode': episode, 'success': env.data['success'],
                            'steps': env.data['steps'], 'max_steps': env.max_steps, 'init_state_id': episode, 'runtime': status}
            if args.smoke_motion:
                receipts = list(env.data['receipts'].values())
                moves = [r for r in receipts if 'motion_mode' in r]
                record['motion_checks'] = {'passed': len(receipts) == 4 and len(moves) == 3
                    and all(r['arrived'] and r['ok'] for r in moves)
                    and [r['motion_mode'] for r in moves] == [
                        'coarse_direct' if env.motion.config.get('calibrated_direct', False) else 'coarse']*2 + ['fine']
                    and {r['grounding']['provenance']['view'] for r in moves if 'grounding' in r}
                        == {'agentview', 'robot0_eye_in_hand'}
                    and all(r['gripper_command'] == 1 for r in moves[1:]) and env.data['gripper_command'] == 1,
                    'moves': moves, 'final_gripper_command': env.data['gripper_command']}
            results.append(record)
            write_json(directory / 'result.json', record)
            save()
            print(f'task={task_id} episode={episode} success={env.data["success"]} '
                  f'sim_steps={env.data["steps"]}/{env.max_steps} stop={status["status"]}', flush=True)
            if status['status'] == 'interrupted':
                return 130
            if not testing and status['status'] != 'restart_required':
                from .post_run import maybe_review
                review = maybe_review(root, directory, config, enabled=args.post_run_review)
                if review is not None:
                    record['post_run_review'] = {'status': review['status'],
                        'report': (directory / 'review/report.json').relative_to(root).as_posix(),
                        'changed_files': review.get('changed_files', [])}
                    save()
            if args.smoke and status['status'] not in {'completed', 'episode_exhausted'}:
                return 1
            if args.smoke_motion and not record['motion_checks']['passed']:
                return 1
    print(f'Results: {output / "summary.json"}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
