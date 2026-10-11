import copy
import json
import math
from pathlib import Path
import tempfile
import unittest

try:
    import numpy as np
except ImportError:
    raise unittest.SkipTest("Install PAC-Harness[robot-vision] for geometry tests")

from pac_harness.grounding import Grounder, GroundingError
from pac_harness.rgbd import Observation


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        intrinsic = {"width": 9, "height": 9,
                     "K_row_major_3x3": [100, 0, 4, 0, 100, 4, 0, 0, 1],
                     "distortion_model_id": 4,
                     "distortion_order": ["k1", "k2", "k3", "k4", "k5", "k6", "p1", "p2"],
                     "distortion": [0] * 8}
        camera = {"rgb_intrinsics": copy.deepcopy(intrinsic),
                  "depth_intrinsics": copy.deepcopy(intrinsic),
                  "depth_aligned_to_rgb": False, "mirrored": False,
                  "depth_to_rgb_extrinsic": {"direction": "T_rgb_camera_from_depth_camera",
                                             "T_row_major_4x4": np.eye(4).ravel().tolist()}}
        self.factory = {"cameras": {view: copy.deepcopy(camera) for view in ("scene", "detail", "wrist")}}
        self.extrinsics = {"cameras": {
            "scene": {"calibrated": True, "T_robot_base_from_camera": np.eye(4).ravel().tolist()},
            "detail": {"calibrated": True, "T_robot_base_from_camera": np.eye(4).ravel().tolist()},
            "wrist": {"calibrated": True, "T_tool_from_camera": np.eye(4).ravel().tolist()},
        }}
        self.config = {"factory_calibration_path": str(root / "factory.json"),
                       "extrinsics_path": str(root / "extrinsics.json"), "sample_radius_px": 0}
        self.hardware = {"pose_reference": "receiver_reported", "orientation_unit": "radians",
                         "tcp_offset_tool_m": [0, 0, .1765]}
        self.observation = Observation("current-image", 1, [0, 0, 0, 0, 0, 0], 42,
            {view: np.zeros((9, 9, 3), dtype=np.uint8) for view in ("scene", "detail", "wrist")},
            {view: np.full((9, 9), .5, dtype=np.float32) for view in ("scene", "detail", "wrist")},
            {}, {"tcp_pose_frame": "T_robot_base_from_tool"})
        self.plan = {"action": {"name": "move", "target": "test_object",
                               "grounding": {"view": "scene", "pixel_uv": [4, 4],
                                             "approach_offset_m": [0, 0, .05]}}}

    def grounder(self):
        Path(self.config["factory_calibration_path"]).write_text(json.dumps(self.factory), encoding="utf-8")
        Path(self.config["extrinsics_path"]).write_text(json.dumps(self.extrinsics), encoding="utf-8")
        return Grounder(self.config, self.hardware)

    def test_calibration_preflight_accepts_complete_files(self):
        report = self.grounder().validate_calibration_files()
        self.assertTrue(report["ok"])
        self.assertEqual(set(report["cameras"]), {"wrist", "scene", "detail"})

    def test_calibration_preflight_rejects_old_missing_detail(self):
        self.extrinsics["cameras"]["detail"] = {"calibrated": False, "T_robot_base_from_camera": None}
        with self.assertRaisesRegex(GroundingError, "detail robot extrinsic is not marked calibrated"):
            self.grounder().validate_calibration_files()

    def test_calibrated_flag_alone_cannot_bypass_matrix_validation(self):
        self.extrinsics["cameras"]["detail"]["T_robot_base_from_camera"] = None
        with self.assertRaisesRegex(GroundingError, "detail T_robot_base_from_camera"):
            self.grounder().validate_calibration_files()

    def test_static_rgbd_point_and_binding(self):
        matrix = np.eye(4)
        matrix[:3, 3] = [.1, -.2, .3]
        self.extrinsics["cameras"]["scene"]["T_robot_base_from_camera"] = matrix.tolist()
        result = self.grounder().resolve(self.plan, self.observation)
        np.testing.assert_allclose(result["object_xyz_base_m"], [.1, -.2, .8])
        np.testing.assert_allclose(result["target_xyz_m"], [.1, -.2, .85])
        self.assertEqual(result["source"], "calibrated_rgbd")
        self.assertEqual(result["observation_id"], "current-image")
        self.assertEqual(result["observation_stamp_ns"], 42)
        self.assertFalse(result["provenance"]["semantic_identity_verified"])
        self.assertEqual(len(result["provenance"]["factory_calibration_path"]["sha256"]), 64)

    def test_unaligned_depth_uses_translation_not_same_rgb_pixel(self):
        # A depth ray at [4,4] projects to RGB [6,4] because sensors have a baseline.
        transform = np.eye(4)
        transform[0, 3] = .01
        self.factory["cameras"]["scene"]["depth_to_rgb_extrinsic"]["T_row_major_4x4"] = transform.tolist()
        self.observation.depths["scene"][:] = np.nan
        self.observation.depths["scene"][4, 4] = .5
        self.plan["action"]["grounding"]["pixel_uv"] = [6, 4]
        result = self.grounder().resolve(self.plan, self.observation)
        np.testing.assert_allclose(result["object_xyz_base_m"], [.01, 0, .5])
        self.plan["action"]["grounding"]["pixel_uv"] = [4, 4]
        with self.assertRaisesRegex(GroundingError, "no registered depth"):
            self.grounder().resolve(self.plan, self.observation)

    def test_missing_registration_never_assumes_identity(self):
        del self.factory["cameras"]["scene"]["depth_to_rgb_extrinsic"]
        with self.assertRaisesRegex(GroundingError, "direction"):
            self.grounder().resolve(self.plan, self.observation)

    def test_occluded_rgb_pixel_uses_nearest_surface(self):
        # A low-focal-length RGB sensor sends two different depth rays to its
        # same central pixel; foreground must win over a later background ray.
        self.factory["cameras"]["scene"]["rgb_intrinsics"]["K_row_major_3x3"][0] = 1
        self.observation.depths["scene"][:] = np.nan
        self.observation.depths["scene"][4, 4] = .5
        self.observation.depths["scene"][4, 5] = 1
        result = self.grounder().resolve(self.plan, self.observation)
        np.testing.assert_allclose(result["object_xyz_base_m"], [0, 0, .5])

    def test_two_depth_surfaces_near_target_are_ambiguous(self):
        self.config["sample_radius_px"] = 1
        self.observation.depths["scene"][4, 5] = 1
        with self.assertRaisesRegex(GroundingError, "ambiguous depth surfaces"):
            self.grounder().resolve(self.plan, self.observation)

    def test_wrist_uses_live_pose_with_tool_transform(self):
        self.plan["action"]["grounding"]["view"] = "wrist"
        self.observation.pose = [.1, .2, .3, math.pi, 0, 0]
        matrix = np.eye(4)
        matrix[0, 3] = .02
        self.extrinsics["cameras"]["wrist"]["T_tool_from_camera"] = matrix.tolist()
        result = self.grounder().resolve(self.plan, self.observation)
        np.testing.assert_allclose(result["object_xyz_base_m"], [.12, .2, -.2], atol=1e-12)
        np.testing.assert_allclose(result["target_xyz_m"], [.12, .2, -.15], atol=1e-12)

    def test_flange_reference_subtracts_rotated_tcp_offset_once(self):
        self.hardware["pose_reference"] = "flange"
        self.observation.pose[3] = math.pi
        result = self.grounder().resolve(self.plan, self.observation)
        np.testing.assert_allclose(result["target_xyz_m"], [0, 0, .7265], atol=1e-12)
        self.assertEqual(result["provenance"]["receiver_pose_reference"], "flange")
        provenance = result["provenance"]
        np.testing.assert_allclose(provenance["current_tcp_xyz_m"], [0, 0, -.1765], atol=1e-12)
        np.testing.assert_allclose(provenance["object_target_receiver_xyz_m"], [0, 0, .6765], atol=1e-12)
        self.assertAlmostEqual(provenance["distance_to_object_m"], .6765)
        self.assertAlmostEqual(provenance["distance_to_approach_target_m"], .7265)

    def test_unknown_receiver_reference_is_refused(self):
        self.observation.state = {}
        with self.assertRaisesRegex(GroundingError, "ambiguous receiver"):
            self.grounder().resolve(self.plan, self.observation)

    def test_metric_model_guess_cannot_become_grounding(self):
        self.plan["action"]["grounding"]["target_xyz_m"] = [.5, 0, .3]
        with self.assertRaisesRegex(GroundingError, "model-supplied"):
            self.grounder().resolve(self.plan, self.observation)

    def test_pixel_bounds_and_units_are_not_guessed(self):
        self.plan["action"]["grounding"]["pixel_uv"] = [400, 400]
        with self.assertRaisesRegex(GroundingError, "outside native"):
            self.grounder().resolve(self.plan, self.observation)

    def test_resizing_requires_capture_provenance(self):
        self.observation.images["scene"] = np.zeros((18, 18, 3), dtype=np.uint8)
        self.plan["action"]["grounding"]["pixel_uv"] = [8, 8]
        with self.assertRaisesRegex(GroundingError, "resize metadata"):
            self.grounder().resolve(self.plan, self.observation)
        self.observation.metadata["transport_resize"] = {
            "scene": {"rgb_source_wh": [9, 9], "rgb_output_wh": [18, 18]}}
        result = self.grounder().resolve(self.plan, self.observation)
        np.testing.assert_allclose(result["object_xyz_base_m"], [0, 0, .5])

    def test_rgb_distortion_changes_which_depth_ray_is_selected(self):
        # Pixel ray x=.4, k1=.5 projects at u=8.32; with fx=10 the
        # k1 distortion difference is observable at subpixel selection scale.
        camera = self.factory["cameras"]["scene"]
        camera["rgb_intrinsics"]["K_row_major_3x3"][0] = 10
        camera["depth_intrinsics"]["K_row_major_3x3"][0] = 10
        camera["rgb_intrinsics"]["distortion"][0] = 1
        self.observation.depths["scene"][:] = np.nan
        self.observation.depths["scene"][4, 8] = .5
        self.plan["action"]["grounding"]["pixel_uv"] = [8, 4]
        # Distorted projection is rounded to u=9, outside this image.
        with self.assertRaisesRegex(GroundingError, "no registered depth"):
            self.grounder().resolve(self.plan, self.observation)
        camera["rgb_intrinsics"]["distortion"][0] = 0
        result = self.grounder().resolve(self.plan, self.observation)
        np.testing.assert_allclose(result["object_xyz_base_m"], [.2, 0, .5])

    def test_float_depth_does_not_auto_detect_millimetres(self):
        self.observation.depths["scene"][:] = 500
        with self.assertRaisesRegex(GroundingError, "no valid metric depth"):
            self.grounder().resolve(self.plan, self.observation)

    def test_missing_or_bad_extrinsic_never_fabricates_world_position(self):
        self.extrinsics["cameras"]["scene"]["T_robot_base_from_camera"][0] = 2
        with self.assertRaisesRegex(GroundingError, "proper rigid transform"):
            self.grounder().resolve(self.plan, self.observation)

    def test_registered_depth_uses_rgb_optical_z_and_preserves_holes(self):
        transform = np.eye(4)
        transform[0, 3] = .01
        transform[2, 3] = .1
        self.factory["cameras"]["scene"]["depth_to_rgb_extrinsic"]["T_row_major_4x4"] = transform.tolist()
        self.observation.depths["scene"][:] = np.nan
        self.observation.depths["scene"][4, 4] = .5
        registered = self.grounder().register_depth(self.observation, "scene")
        np.testing.assert_array_equal(registered["u"], [6])
        np.testing.assert_array_equal(registered["v"], [4])
        np.testing.assert_allclose(registered["points_rgb_m"], [[.01, 0, .6]])
        self.assertEqual(registered["depth_rgb_m"].dtype, np.float64)
        self.assertEqual(registered["depth_rgb_m"].shape, (9, 9))
        self.assertAlmostEqual(registered["depth_rgb_m"][4, 6], .6)
        self.assertTrue(np.isnan(registered["depth_rgb_m"][4, 4]))
        self.assertEqual(np.isfinite(registered["depth_rgb_m"]).sum(), 1)
        provenance = registered["provenance"]
        self.assertEqual(provenance["registered_pixels"], 1)
        self.assertEqual(provenance["observation_id"], self.observation.id)
        self.assertEqual(provenance["observation_stamp_ns"], self.observation.stamp_ns)
        self.assertEqual(provenance["source_depth_filter_m"], [.05, 2.5])
        self.assertEqual(len(provenance["factory_calibration_path"]["sha256"]), 64)
        json.dumps(provenance, allow_nan=False)

    def test_registered_depth_keeps_nearest_point_in_rgb_pixel(self):
        self.factory["cameras"]["scene"]["rgb_intrinsics"]["K_row_major_3x3"][0] = 1
        self.observation.depths["scene"][:] = np.nan
        self.observation.depths["scene"][4, 4] = 1
        self.observation.depths["scene"][4, 5] = .5
        registered = self.grounder().register_depth(self.observation, "scene")
        np.testing.assert_array_equal(registered["u"], [4])
        np.testing.assert_array_equal(registered["v"], [4])
        # Retain the actual source point, not a synthetic point on the rounded RGB ray.
        np.testing.assert_allclose(registered["points_rgb_m"], [[.005, 0, .5]])
        self.assertAlmostEqual(registered["depth_rgb_m"][4, 4], .5)
        self.assertEqual(registered["provenance"]["valid_source_samples"], 2)

    def test_registered_depth_discards_samples_outside_rgb_view(self):
        self.factory["cameras"]["scene"]["rgb_intrinsics"]["K_row_major_3x3"][0] = 1000
        self.observation.depths["scene"][:] = np.nan
        self.observation.depths["scene"][4, 4] = .5
        self.observation.depths["scene"][4, 8] = .5
        registered = self.grounder().register_depth(self.observation, "scene")
        self.assertEqual(registered["provenance"]["valid_source_samples"], 2)
        self.assertEqual(registered["provenance"]["registered_pixels"], 1)
        np.testing.assert_array_equal(registered["u"], [4])

    def test_registered_depth_rejects_behind_camera_samples(self):
        transform = np.eye(4)
        transform[2, 3] = -1
        self.factory["cameras"]["scene"]["depth_to_rgb_extrinsic"]["T_row_major_4x4"] = transform.tolist()
        with self.assertRaisesRegex(GroundingError, "in front of RGB"):
            self.grounder().register_depth(self.observation, "scene")

    def test_registered_depth_rejects_already_aligned_capture(self):
        grounder = self.grounder()
        for flag in (True, {"scene": True}):
            with self.subTest(flag=flag):
                self.observation.metadata["depth_aligned_to_rgb"] = flag
                with self.assertRaisesRegex(GroundingError, "already aligned"):
                    grounder.register_depth(self.observation, "scene")

    def test_registered_depth_requires_native_factory_registration(self):
        self.factory["cameras"]["scene"]["depth_aligned_to_rgb"] = True
        with self.assertRaisesRegex(GroundingError, "native unaligned"):
            self.grounder().register_depth(self.observation, "scene")
        self.factory["cameras"]["scene"]["depth_aligned_to_rgb"] = False
        del self.factory["cameras"]["scene"]["depth_to_rgb_extrinsic"]
        with self.assertRaisesRegex(GroundingError, "direction"):
            self.grounder().register_depth(self.observation, "scene")

    def test_registered_depth_rejects_integer_or_unverified_resized_depth(self):
        self.observation.depths["scene"] = np.full((9, 9), 500, dtype=np.uint16)
        with self.assertRaisesRegex(GroundingError, "floating-point metres"):
            self.grounder().register_depth(self.observation, "scene")
        self.observation.depths["scene"] = np.full((8, 8), .5, dtype=np.float32)
        with self.assertRaisesRegex(GroundingError, "resolution changed"):
            self.grounder().register_depth(self.observation, "scene")

    def test_registered_depth_does_not_require_robot_handeye_transform(self):
        grounder = self.grounder()
        del grounder.config["extrinsics_path"]
        registered = grounder.register_depth(self.observation, "scene")
        self.assertEqual(registered["provenance"]["registered_pixels"], 81)
        self.assertNotIn("extrinsics_path", registered["provenance"])

    def test_registered_depth_range_is_finite_and_bound_to_provenance(self):
        grounder = self.grounder()
        for bounds in ((0, 2.5), (.5, .5), (.5, float("inf")), (float("nan"), 2.5)):
            with self.subTest(bounds=bounds):
                grounder.config.update(min_depth_m=bounds[0], max_depth_m=bounds[1])
                with self.assertRaisesRegex(GroundingError, "depth range must be finite"):
                    grounder.register_depth(self.observation, "scene")
        grounder.config.update(min_depth_m=.1, max_depth_m=1.)
        registered = grounder.register_depth(self.observation, "scene")
        self.assertEqual(registered["provenance"]["source_depth_filter_m"], [.1, 1.])


if __name__ == "__main__":
    unittest.main()
