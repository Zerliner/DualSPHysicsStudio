from __future__ import annotations

import math
from pathlib import Path
import shutil
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dualsphysics_studio.core import (  # noqa: E402
    CaseDocument,
    GeometryEditError,
    GeometryType,
    ObjectRole,
    ReadOnlyGeometryError,
    UnsafeXmlError,
    Vec3,
    XmlSyntaxError,
)
from dualsphysics_studio.core.document import _boxfill_prism_spec  # noqa: E402
from dualsphysics_studio.core.geometry import prism_mass_properties  # noqa: E402
from dualsphysics_studio.core.source import MAX_XML_BYTES  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "sample_case.xml"


def assert_vec_almost(testcase: unittest.TestCase, actual: Vec3, expected: Vec3) -> None:
    testcase.assertAlmostEqual(actual.x, expected.x, places=10)
    testcase.assertAlmostEqual(actual.y, expected.y, places=10)
    testcase.assertAlmostEqual(actual.z, expected.z, places=10)


class CaseDocumentLoadTests(unittest.TestCase):
    def test_loads_nine_objects_and_six_floatings(self) -> None:
        original = FIXTURE.read_bytes().decode("utf-8")
        document = CaseDocument.from_file(FIXTURE)

        self.assertEqual(document.raw_text, original)
        self.assertEqual(len(document.objects), 9)
        self.assertEqual(document.objects[0].shapeout, "Water")
        self.assertEqual(document.objects[1].geometry_type, GeometryType.DRAWBOX)
        self.assertEqual(document.objects[2].shapeout, "FixedSlope")
        self.assertEqual(
            [scene.marker for scene in document.objects[3:]], [10, 11, 12, 13, 14, 15]
        )
        self.assertTrue(all(scene.role is ObjectRole.FLOATING for scene in document.objects[3:]))
        self.assertEqual(document.facts.object_count, 9)
        self.assertEqual(document.facts.floating_count, 6)
        self.assertAlmostEqual(document.facts.dp or 0.0, 0.01)
        self.assertAlmostEqual(document.facts.smoothing_length or 0.0, math.sqrt(3) * 0.01)
        self.assertEqual(document.facts.sound_speed, 80.0)

    def test_v54_diagnostics_and_explanation(self) -> None:
        document = CaseDocument.from_file(FIXTURE)
        codes = {item.code for item in document.diagnostics}
        self.assertIn("INACTIVE_VERLET_STEPS", codes)
        self.assertIn("INACTIVE_SHIFT_PARAMETER", codes)
        self.assertIn("DEPRECATED_PARTSOUTMAX", codes)
        explanation = document.explain_node(
            "/case[1]/execution[1]/parameters[1]/parameter[2]"
        )
        self.assertFalse(explanation.effective)
        self.assertIn("不生效", explanation.notes[0])

    def test_rejects_dtd_and_entity_without_resolution(self) -> None:
        payload = """<?xml version='1.0'?>
<!DOCTYPE case [<!ENTITY xxe SYSTEM 'file:///C:/Windows/win.ini'>]>
<case><name>&xxe;</name></case>"""
        with self.assertRaises(UnsafeXmlError):
            CaseDocument.from_text(payload)

    def test_rejects_oversized_xml_before_reading_the_complete_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "oversized.xml"
            with target.open("wb") as stream:
                stream.seek(MAX_XML_BYTES)
                stream.write(b"\0")

            with self.assertRaisesRegex(UnsafeXmlError, "80 MiB"):
                CaseDocument.from_file(target)

    def test_loads_utf16_xml_within_the_byte_and_character_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "utf16.xml"
            source = "<case><name>中文</name></case>"
            target.write_bytes(source.encode("utf-16"))

            document = CaseDocument.from_file(target)

            self.assertEqual(document.raw_text, source)
            self.assertIn(document.encoding, {"utf-16-le", "utf-16-be"})

    def test_expression_geometry_is_source_only(self) -> None:
        source = """<case><casedef><geometry><definition dp="0.1"><pointmin x="0" y="0" z="0"/><pointmax x="2" y="2" z="2"/></definition><commands><mainlist>
<setmkfluid mk="0"/><drawbox><point x="${x}" y="0" z="0"/><size x="1" y="1" z="1"/></drawbox>
</mainlist></commands></geometry></casedef></case>"""
        document = CaseDocument.from_text(source)
        self.assertEqual(len(document.objects), 1)
        self.assertFalse(document.objects[0].editable)
        self.assertIn("表达式", document.objects[0].readonly_reason or "")
        with self.assertRaises(ReadOnlyGeometryError):
            document.translate_object(0, (1, 0, 0))

    def test_invalid_source_draft_keeps_last_valid_scene(self) -> None:
        document = CaseDocument.from_file(FIXTURE)
        previous_text = document.text
        self.assertFalse(document.stage_source("<case>"))
        self.assertEqual(len(document.objects), 9)
        self.assertEqual(document.text, previous_text)
        self.assertEqual(document.editor_text, "<case>")
        self.assertEqual(document.draft_diagnostics[0].code, "XML_DRAFT_ERROR")
        document.discard_draft()
        self.assertIsNone(document.draft_text)


class CaseDocumentEditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = CaseDocument.from_file(FIXTURE)

    def test_local_attribute_edit_preserves_comments_and_layout(self) -> None:
        comment = "<!-- 中文注释必须原样保留 -->"
        self.document.update_attribute(
            "/case[1]/casedef[1]/geometry[1]/definition[1]", "dp", "0.02"
        )
        self.assertIn(comment, self.document.text)
        self.assertIn('<definition dp="0.02" units_comment="metres (m)">', self.document.text)
        self.assertAlmostEqual(self.document.dp or 0.0, 0.02)

    def test_translate_floating_updates_points_and_center_atomically(self) -> None:
        scene = self.document.objects[3]
        old_first = scene.vertices[0]
        old_center = scene.floating_center
        self.document.translate_object(scene.id, (0.02, -0.01, 0.03))
        moved = self.document.objects[3]
        assert_vec_almost(self, moved.vertices[0], old_first + Vec3(0.02, -0.01, 0.03))
        assert_vec_almost(self, moved.floating_center, old_center + Vec3(0.02, -0.01, 0.03))
        self.assertTrue(self.document.can_undo)

        self.assertTrue(self.document.undo())
        self.assertEqual(self.document.objects[3].vertices[0], old_first)
        self.assertTrue(self.document.redo())
        self.assertEqual(self.document.objects[3].vertices[0], moved.vertices[0])

    def test_scale_rotate_and_vertex_edit_prism(self) -> None:
        scene = self.document.objects[3]
        original_size = scene.size
        self.document.scale_object(scene.id, (2.0, 1.0, 1.0))
        scaled = self.document.objects[3]
        self.assertAlmostEqual(scaled.size.x, original_size.x * 2.0)
        center_before_rotation = scaled.floating_center
        self.document.rotate_object(scaled.id, 90, axis="z")
        rotated = self.document.objects[3]
        self.assertAlmostEqual(rotated.size.x, scaled.size.y, places=8)
        self.assertAlmostEqual(rotated.size.y, scaled.size.x, places=8)
        self.assertEqual(rotated.floating_center, center_before_rotation)
        target = rotated.vertices[0] + Vec3(0.01, 0.0, 0.0)
        self.document.set_vertex(rotated.id, 0, target)
        assert_vec_almost(self, self.document.objects[3].vertices[0], target)

    def test_box_translation_scaling_and_quarter_turn(self) -> None:
        box = self.document.objects[1]
        self.document.translate_object(box.id, 0.1, 0.0, 0.0)
        moved = self.document.objects[1]
        self.assertAlmostEqual(moved.position.x, box.position.x + 0.1)
        self.document.scale_object(moved.id, (0.5, 1.0, 1.0))
        scaled = self.document.objects[1]
        self.assertAlmostEqual(scaled.size.x, box.size.x * 0.5)
        self.document.rotate_object(scaled.id, 90, axis="z")
        turned = self.document.objects[1]
        self.assertAlmostEqual(turned.size.x, scaled.size.y, places=8)
        self.assertAlmostEqual(turned.size.y, scaled.size.x, places=8)
        with self.assertRaises(GeometryEditError):
            self.document.rotate_object(turned.id, 12.5, axis="z")

    def test_boxfill_to_prism_mode_and_hidden_face_mapping(self) -> None:
        self.assertEqual(_boxfill_prism_spec("solid"), ("full", "0"))
        self.assertEqual(_boxfill_prism_spec("all"), ("face", "0"))
        self.assertEqual(
            _boxfill_prism_spec("bottom | left | right | front | back"),
            ("face", "2"),
        )
        self.assertEqual(
            _boxfill_prism_spec("bottom | left"),
            ("face", "2 | 3 | 4 | 5"),
        )
        self.assertEqual(
            _boxfill_prism_spec("all^top^front"),
            ("face", "2 | 3"),
        )
        with self.assertRaises(GeometryEditError):
            _boxfill_prism_spec("solid | top")

    def test_box_vertex_edit_converts_to_prism_without_moving_other_vertices(self) -> None:
        box = self.document.objects[1]
        original_text = self.document.text
        original_vertices = box.vertices
        target = original_vertices[7] + Vec3(0.07, -0.03, 0.05)

        self.document.set_vertex(box.id, 7, target)

        converted = self.document.get_object(box.id)
        self.assertEqual(converted.geometry_type, GeometryType.DRAWPRISM)
        self.assertEqual(converted.id, box.id)
        self.assertEqual(converted.marker, box.marker)
        self.assertEqual(converted.shapeout, box.shapeout)
        for index, original in enumerate(original_vertices):
            expected = target if index == 7 else original
            assert_vec_almost(self, converted.vertices[index], expected)
        self.assertIn("<!-- DualSPHysics Studio 原始 drawbox（顶点编辑前）:", self.document.text)
        self.assertIn("<drawbox>", self.document.text)
        self.assertIn('<setdrawmode mode="face" />', self.document.text)
        self.assertIn('<drawprism mask="2">', self.document.text)
        prism_end = self.document.text.index("</drawprism>")
        shapeout = self.document.text.index('<shapeout file="Tank"', prism_end)
        self.assertIn(
            '<setdrawmode mode="full" />',
            self.document.text[prism_end:shapeout],
        )

        converted_text = self.document.text
        self.assertTrue(self.document.undo())
        self.assertEqual(self.document.text, original_text)
        self.assertFalse(self.document.undo())
        self.assertTrue(self.document.redo())
        self.assertEqual(self.document.text, converted_text)

    def test_box_vertex_conversion_updates_floating_center_in_one_undo(self) -> None:
        source = """<case><casedef><geometry><definition dp="0.1"><pointmin x="-2" y="-2" z="-2"/><pointmax x="3" y="3" z="3"/></definition><commands><mainlist>
<setdrawmode mode="full"/><setmkbound mk="2"/>
<drawbox><boxfill>solid</boxfill><point x="0" y="0" z="0"/><size x="1" y="1" z="1"/></drawbox><shapeout file="FloatBox"/>
</mainlist></commands></geometry><floatings><floating mkbound="2" rhopbody="1000"><center x="0.5" y="0.5" z="0.5"/></floating></floatings></casedef></case>"""
        document = CaseDocument.from_text(source)
        box = document.objects[0]
        old_center = box.floating_center
        old_vertex = box.vertices[6]
        target = old_vertex + Vec3(0.8, -0.4, 0.16)

        document.set_vertex(box.id, 6, target)

        converted = document.get_object(box.id)
        new_vertices = list(box.vertices)
        new_vertices[6] = target
        _, old_geometry_center = prism_mass_properties(box.vertices)
        _, new_geometry_center = prism_mass_properties(new_vertices)
        assert_vec_almost(
            self,
            converted.floating_center,
            old_center + new_geometry_center - old_geometry_center,
        )
        self.assertNotEqual(
            converted.floating_center,
            old_center + (target - old_vertex) * (1.0 / 8.0),
        )
        self.assertEqual(document.text.count('<setdrawmode mode="full" />'), 2)
        self.assertIn('<drawprism mask="0">', document.text)
        self.assertTrue(document.undo())
        self.assertEqual(document.text, source)
        self.assertFalse(document.undo())

    def test_prism_mass_properties_and_invalid_vertex_geometry(self) -> None:
        cube = (
            Vec3(0, 0, 0), Vec3(1, 0, 0), Vec3(1, 1, 0), Vec3(0, 1, 0),
            Vec3(0, 0, 1), Vec3(1, 0, 1), Vec3(1, 1, 1), Vec3(0, 1, 1),
        )
        volume, center = prism_mass_properties(cube)
        self.assertAlmostEqual(volume, 1.0)
        assert_vec_almost(self, center, Vec3(0.5, 0.5, 0.5))

        translated_box = (
            Vec3(10, 20, 30), Vec3(12, 20, 30), Vec3(12, 23, 30), Vec3(10, 23, 30),
            Vec3(10, 20, 34), Vec3(12, 20, 34), Vec3(12, 23, 34), Vec3(10, 23, 34),
        )
        volume, center = prism_mass_properties(translated_box)
        self.assertAlmostEqual(volume, 24.0)
        assert_vec_almost(self, center, Vec3(11.0, 21.5, 32.0))
        reversed_box = (
            translated_box[3], translated_box[2], translated_box[1], translated_box[0],
            translated_box[7], translated_box[6], translated_box[5], translated_box[4],
        )
        reversed_volume, reversed_center = prism_mass_properties(reversed_box)
        self.assertAlmostEqual(reversed_volume, -24.0)
        assert_vec_almost(self, reversed_center, center)

        source = """<case><casedef><geometry><definition dp="0.1"><pointmin x="-4" y="-4" z="-4"/><pointmax x="4" y="4" z="4"/></definition><commands><mainlist>
<setdrawmode mode="full"/><setmkbound mk="0"/><drawbox><boxfill>solid</boxfill><point x="0" y="0" z="0"/><size x="1" y="1" z="1"/></drawbox>
</mainlist></commands></geometry></casedef></case>"""
        document = CaseDocument.from_text(source)
        with self.assertRaisesRegex(GeometryEditError, "零体积"):
            document.set_vertex(0, 6, (1.0, 1.0, -2.0))
        self.assertEqual(document.text, source)

        origin_source = source.replace(
            '<point x="0" y="0" z="0"/><size x="1" y="1" z="1"/>',
            '<point x="0.45" y="0.45" z="1.3"/><size x="0.1" y="0.1" z="0.1"/>',
        )
        origin_document = CaseDocument.from_text(origin_source)
        original_vertices = origin_document.objects[0].vertices
        origin_document.set_vertex(0, 7, (0.0, 0.0, 0.0))
        converted_vertices = origin_document.objects[0].vertices
        assert_vec_almost(self, converted_vertices[7], Vec3(0.0, 0.0, 0.0))
        for index in range(7):
            assert_vec_almost(self, converted_vertices[index], original_vertices[index])

    def test_box_vertex_rejects_duplicate_nonfinite_and_unknown_draw_mode(self) -> None:
        box = self.document.objects[1]
        original_text = self.document.text
        with self.assertRaises(GeometryEditError):
            self.document.set_vertex(box.id, 0, box.vertices[1])
        with self.assertRaises(GeometryEditError):
            self.document.set_vertex(box.id, 0, (float("inf"), 0.0, 0.0))
        with self.assertRaises(GeometryEditError):
            self.document.set_vertex(box.id, 0, (float("nan"), 0.0, 0.0))
        with self.assertRaisesRegex(GeometryEditError, "网格吸附"):
            self.document.set_vertex(
                box.id,
                0,
                (1e308, 0.0, 0.0),
                snap_to_grid=True,
            )
        self.assertEqual(self.document.text, original_text)

        no_mode = """<case><casedef><geometry><definition dp="0.1"><pointmin x="0" y="0" z="0"/><pointmax x="2" y="2" z="2"/></definition><commands><mainlist>
<setmkbound mk="0"/><drawbox><boxfill>all</boxfill><point x="0" y="0" z="0"/><size x="1" y="1" z="1"/></drawbox>
</mainlist></commands></geometry></casedef></case>"""
        document = CaseDocument.from_text(no_mode)
        with self.assertRaisesRegex(GeometryEditError, "setdrawmode"):
            document.set_vertex(0, 0, (-0.1, 0.0, 0.0))
        self.assertEqual(document.text, no_mode)

    def test_box_source_with_internal_comment_remains_legal_and_recoverable(self) -> None:
        source = """<case><casedef><geometry><definition dp="0.1"><pointmin x="0" y="0" z="0"/><pointmax x="2" y="2" z="2"/></definition><commands><mainlist>
<setdrawmode mode="full"/><setmkbound mk="0"/>
<drawbox><!-- keep me --><boxfill>all</boxfill><point x="0" y="0" z="0"/><size x="1" y="1" z="1"/></drawbox>
</mainlist></commands></geometry></casedef></case>"""
        document = CaseDocument.from_text(source)
        document.set_vertex(0, 0, (-0.1, 0.0, 0.0))
        self.assertIn("UTF-8 Base64", document.text)
        self.assertEqual(document.objects[0].geometry_type, GeometryType.DRAWPRISM)
        self.assertTrue(document.undo())
        self.assertEqual(document.text, source)

    def test_box_face_subset_conversion_remains_vertex_editable(self) -> None:
        source = """<case><casedef><geometry><definition dp="0.1"><pointmin x="-2" y="-2" z="-2"/><pointmax x="3" y="3" z="3"/></definition><commands><mainlist>
<setdrawmode mode="full"/><setmkbound mk="0"/>
<drawbox><boxfill>bottom | left</boxfill><point x="0" y="0" z="0"/><size x="1" y="1" z="1"/></drawbox>
</mainlist></commands></geometry></casedef></case>"""
        document = CaseDocument.from_text(source)

        document.set_vertex(0, 7, (0.0, 1.0, 1.2))

        converted = document.objects[0]
        self.assertTrue(converted.editable)
        self.assertIn('mask="2 | 3 | 4 | 5"', document.text)
        document.set_vertex(converted.id, 6, (1.1, 1.0, 1.0))
        assert_vec_almost(self, document.objects[0].vertices[6], Vec3(1.1, 1.0, 1.0))

    def test_marker_and_shapeout_updates_include_floating_association(self) -> None:
        scene = self.document.objects[3]
        self.document.set_object_marker(scene.id, 20)
        updated = self.document.objects[3]
        self.assertEqual(updated.marker, 20)
        self.assertEqual(updated.role, ObjectRole.FLOATING)
        self.document.rename_shapeout(updated.id, "Block_A")
        self.assertEqual(self.document.objects[3].shapeout, "Block_A")

    def test_grid_snap_uses_dp(self) -> None:
        scene = self.document.objects[3]
        self.document.translate_object(scene.id, (0.006, 0.0, 0.0), snap_to_grid=True)
        self.assertAlmostEqual(self.document.objects[3].position.x % 0.01, 0.0, places=8)

    def test_add_box_and_floating_prism_then_undo(self) -> None:
        fluid_box = self.document.add_box({
            "point": (1.0, 0.02, 0.40),
            "size": (0.10, 0.10, 0.10),
            "category": "fluid",
            "marker": 1,
            "shapeout": "ExtraWater",
        })
        self.assertEqual(len(self.document.objects), 10)
        self.assertEqual(fluid_box.marker, 1)
        self.assertEqual(fluid_box.role, ObjectRole.FLUID)
        self.assertIn('<shapeout file="ExtraWater" />', self.document.text)

        points = (
            (0.30, 0.02, 0.30), (0.30, 0.02, 0.36),
            (0.36, 0.02, 0.36), (0.36, 0.02, 0.30),
            (0.30, 0.08, 0.30), (0.30, 0.08, 0.36),
            (0.36, 0.08, 0.36), (0.36, 0.08, 0.30),
        )
        floating = self.document.add_prism(
            points,
            role="floating",
            shapeout="ExtraBlock",
            floating_density=2500,
        )
        self.assertEqual(len(self.document.objects), 11)
        self.assertEqual(floating.marker, 2)
        self.assertEqual(floating.role, ObjectRole.FLOATING)
        self.assertIsNotNone(floating.floating_center)
        self.assertIn('mkbound="2" rhopbody="2500"', self.document.text)
        self.assertTrue(self.document.undo())
        self.assertEqual(len(self.document.objects), 10)
        self.assertTrue(self.document.undo())
        self.assertEqual(len(self.document.objects), 9)

    def test_delete_object_removes_unique_associations_and_can_undo(self) -> None:
        scene = self.document.objects[3]
        self.document.delete_object(scene.id)
        self.assertEqual(len(self.document.objects), 8)
        self.assertEqual(self.document.facts.floating_count, 5)
        self.assertNotIn('file="SlideBlock_mk10"', self.document.text)
        self.assertNotIn('<floating mkbound="10"', self.document.text)
        self.assertTrue(self.document.undo())
        self.assertEqual(len(self.document.objects), 9)
        self.assertEqual(self.document.facts.floating_count, 6)

    def test_delete_blocks_shared_marker_and_shapeout(self) -> None:
        source = """<case><casedef><mkconfig boundcount="10" fluidcount="10"/><geometry><definition dp="0.1"><pointmin x="0" y="0" z="0"/><pointmax x="3" y="3" z="3"/></definition><commands><mainlist>
<setmkbound mk="0"/>
<drawbox><boxfill>solid</boxfill><point x="0" y="0" z="0"/><size x="1" y="1" z="1"/></drawbox>
<drawbox><boxfill>solid</boxfill><point x="1" y="0" z="0"/><size x="1" y="1" z="1"/></drawbox>
<shapeout file="Together"/>
</mainlist></commands></geometry></casedef></case>"""
        document = CaseDocument.from_text(source)
        with self.assertRaises(GeometryEditError):
            document.delete_object(0)


class CaseDocumentSaveTests(unittest.TestCase):
    def test_save_creates_timestamped_backup_before_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Case_Def.xml"
            shutil.copy2(FIXTURE, target)
            original_bytes = target.read_bytes()
            document = CaseDocument.from_file(target)
            document.translate_object(3, (0.01, 0.0, 0.0))
            result = document.save()

            self.assertEqual(result.path, target.resolve())
            self.assertIsNotNone(result.backup_path)
            self.assertTrue(result.backup_path.is_file())
            self.assertEqual(result.backup_path.read_bytes(), original_bytes)
            self.assertIn(".dualsphysics-studio", result.backup_path.parts)
            self.assertFalse(document.dirty)
            reloaded = CaseDocument.from_file(target)
            self.assertEqual(reloaded.objects[3].vertices[0], document.objects[3].vertices[0])

    def test_unchanged_document_round_trips_byte_for_byte(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Copy.xml"
            document = CaseDocument.from_file(FIXTURE)
            document.save(target)
            self.assertEqual(target.read_bytes(), FIXTURE.read_bytes())

    def test_invalid_draft_blocks_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "Case.xml"
            document = CaseDocument.from_file(FIXTURE)
            document.stage_source("<case>")
            with self.assertRaises(XmlSyntaxError):
                document.save(target)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
