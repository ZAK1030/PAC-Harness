"""Read-only camera scenario: no robot commands or automatic enable."""
from pac_harness.devices import CameraSet


class CameraEnvironment:
    def __init__(self, root, run_directory, config):
        self.cameras = CameraSet(root=root, run_directory=run_directory, cameras=config.get("cameras"))

    def observe(self):
        return self.cameras.observe()

    def actions(self):
        return []

    def validate(self, action, observation):
        raise ValueError("This scene exposes no physical actions")

    def execute(self, action, request_id):
        raise ValueError("This scene exposes no physical actions")

    def reconcile(self, request_id):
        return {"status": "not_executed"}

    def tools(self):
        return []

    def close(self):
        self.cameras.close()


def create(*, root, run_directory, config):
    return CameraEnvironment(root, run_directory, config)
